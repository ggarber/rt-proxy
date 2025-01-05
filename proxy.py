import argparse
import asyncio
import fractions
import logging
import ssl
import uuid

from aiohttp import web
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription

from av import AudioFrame, AudioResampler

from google import genai


AUDIO_PTIME = 0.02
MODEL = "gemini-2.0-flash-exp"

client = genai.Client(http_options={'api_version': 'v1alpha'})

logger = logging.getLogger("proxy")
connections = set()


class SendingTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.queue = asyncio.Queue()

    async def recv(self):
        return await self.queue.get()
    

class RTCConnection:
    recv_track = None
    send_track = None
    pc = None

    async def handle_offer(self, request):
        content = await request.text()
        offer = RTCSessionDescription(sdp=content, type="offer")

        self.pc = RTCPeerConnection()
        
        asyncio.ensure_future(self._run())

        await self.pc.setRemoteDescription(offer)

        answer = await self.pc.createAnswer()
        await self.pc.setLocalDescription(answer)

        return web.Response(
            content_type="application/sdp",
            text=self.pc.localDescription.sdp,
        )

    async def _run(self):
        pc_id = str(uuid.uuid4())

        def log_info(msg, *args):
            logger.info(pc_id + " " + msg, *args)

        log_info("Connection started")

        @self.pc.on("datachannel")
        def on_datachannel(channel):
            @channel.on("message")
            async def on_message(message):
                if self.genai_session:
                    await self.genai_session.send(input=message, end_of_turn=True)

        @self.pc.on("connectionstatechange")
        async def on_connectionstatechange():
            log_info("Connection state is %s", self.pc.connectionState)
            if self.pc.connectionState == "failed" or self.pc.connectionState == "closed":
                await self.close()

        @self.pc.on("track")
        def on_track(track):
            log_info("Track %s received", track.kind)

            # Only accept the first track received for now
            if self.recv_track:
                return

            if track.kind == "audio":
                self.recv_track = track
                self.send_track = SendingTrack()
                self.pc.addTrack(self.send_track)
                asyncio.ensure_future(run_recv_track())
        
            @track.on("ended")
            async def on_ended():
                log_info("Track %s ended", track.kind)

        async def run_recv_track():
            sample_rate = 16000
            resampler = AudioResampler(
                format="s16", 
                layout="mono",
                rate=sample_rate,
                frame_size=int(sample_rate * AUDIO_PTIME),
            )

            while True:
                try:
                    frame = await self.recv_track.recv()
                    for frame in resampler.resample(frame):
                        blob = genai.types.BlobDict(
                            data=frame.to_ndarray().tobytes(), 
                            mime_type=f"audio/pcm;rate={sample_rate}"
                        )
                        await self.genai_session.send(blob)

                except Exception as e:
                    log_info("Error receiving frame: %s", e)
                    break

        async def run_send_track():
            timestamp = 0
            buffer = b''
            while True:
                turn = self.genai_session.receive()
                async for response in turn:
                    if response.data is None:
                        log_info(f'Server Message - {response}')
                        continue

                    mime_type = response.server_content.model_turn.parts[0].inline_data.mime_type
                    sample_rate = int(mime_type.split('rate=')[1])
                    samples = int(sample_rate * AUDIO_PTIME)

                    buffer += response.data
                    
                    while len(buffer) / 2 >= samples:
                        frame = AudioFrame(format="s16", layout="mono", samples=samples)
                        frame.sample_rate = sample_rate
                        frame.planes[0].update(buffer[:samples*2])
                        buffer = buffer[samples*2:]

                        timestamp += sample_rate * AUDIO_PTIME
                        frame.pts = timestamp
                        frame.time_base = fractions.Fraction(1, sample_rate)
                        await self.send_track.queue.put(frame)
                        await asyncio.sleep(AUDIO_PTIME)


        try:
            async with client.aio.live.connect(model=MODEL, config={
                "generation_config": { "response_modalities": ["AUDIO"] },
            }) as session:
                log_info("Connected to GenAI session")
                self.genai_session = session
                # await session.send(input="Sing a song", end_of_turn=True)

                await run_send_track()

        except Exception as e:
            log_info("Error sending frame: %s", e)

        await self.close()
        connections.discard(self)
        log_info(f"Connection stopped. Connections {len(connections)}")

    async def close(self):
        if self.pc:
            await self.pc.close()
        if self.genai_session:
            await self.genai_session.close()


async def offer(request):
    connection = RTCConnection()
    connections.add(connection)
    return await connection.handle_offer(request)


async def on_shutdown(app):
    # close peer connections
    coros = [conn.close() for conn in connections]
    await asyncio.gather(*coros)
    connections.clear()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Real Time LLM Proxy")
    parser.add_argument("--cert-file")
    parser.add_argument("--key-file")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    logging.getLogger('aioice').setLevel(level=logging.WARN)

    if args.cert_file:
        ssl_context = ssl.SSLContext()
        ssl_context.load_cert_chain(args.cert_file, args.key_file)
    else:
        ssl_context = None

    app = web.Application()
    app.on_shutdown.append(on_shutdown)
    app.router.add_post("/", offer)
    app.router.add_static("/assets", "assets")
    web.run_app(
        app, access_log=None, host=args.host, port=args.port, ssl_context=ssl_context
    )