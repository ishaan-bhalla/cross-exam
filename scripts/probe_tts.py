import argparse
import asyncio
import base64
import json
import wave
from urllib.parse import urlencode
import websockets
from crossexam.config import get_settings


async def main():
    p = argparse.ArgumentParser(description="Generate speech via the streaming TTS WebSocket and log every server message.")
    p.add_argument("--text", required=True)
    p.add_argument("--voice")
    p.add_argument("--out", default="probe.wav")
    a = p.parse_args()
    s = get_settings()

    url = f"{s.tts_ws_url}?{urlencode({'voice': a.voice or s.tts_default_voice})}"
    audio = bytearray()
    seen_types = {}

    async with websockets.connect(url, additional_headers={"Authorization": s.assemblyai_api_key},
                                  open_timeout=s.tts_timeout_seconds) as ws:
        await ws.send(json.dumps({"type": "Generate", "text": a.text}))
        await ws.send(json.dumps({"type": "Flush"}))
        while True:
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=s.tts_timeout_seconds)
            except asyncio.TimeoutError:
                print("timeout waiting for more messages")
                break
            if isinstance(msg, bytes):
                audio.extend(msg)
                seen_types["<binary>"] = seen_types.get("<binary>", 0) + 1
                continue
            data = json.loads(msg)
            t = data.get("type", "?")
            seen_types[t] = seen_types.get(t, 0) + 1
            for field in ("audio", "data", "chunk"):
                if isinstance(data.get(field), str):
                    try:
                        audio.extend(base64.b64decode(data[field], validate=True))
                    except ValueError:
                        print(f"{field} is not base64: {data[field][:100]}")
                        continue
                    data[field] = f"<{len(data[field])} base64 chars>"
            if seen_types[t] <= 3:
                print(json.dumps(data)[:600])
            if t == "FlushDone":
                await ws.send(json.dumps({"type": "Terminate"}))
            if t in ("Termination", "Terminated", "Error"):
                break

    print("\nmessage counts:", seen_types)
    if audio:
        out = s.resolve(s.outputs_dir) / a.out
        out.parent.mkdir(exist_ok=True)
        with wave.open(str(out), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(s.tts_sample_rate)
            w.writeframes(bytes(audio))
        print(f"saved {len(audio)} bytes ({len(audio) / (s.tts_sample_rate * 2):.1f}s) to {out}")


if __name__ == "__main__":
    asyncio.run(main())
