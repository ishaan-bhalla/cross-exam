import argparse
import asyncio
import json
import wave
from urllib.parse import urlencode
import websockets
from crossexam.config import get_settings, PROJECT_ROOT


async def try_type(msg_type, text, voice, s, out_dir):
    url = f"{s.tts_ws_url}?{urlencode({'voice': voice})}"
    print(f"\n=== type={msg_type!r} ===")
    audio = bytearray()
    try:
        async with websockets.connect(url, additional_headers={"Authorization": s.assemblyai_api_key},
                                      open_timeout=s.tts_timeout_seconds) as ws:
            await ws.send(json.dumps({"type": msg_type, "text": text}))
            while True:
                msg = await asyncio.wait_for(ws.recv(), timeout=s.tts_timeout_seconds)
                if isinstance(msg, bytes):
                    audio.extend(msg)
                    continue
                data = json.loads(msg)
                summary = {k: v for k, v in data.items() if k not in ("audio", "data")}
                if "audio" in data or "data" in data:
                    summary["audio_field_chars"] = len(data.get("audio") or data.get("data") or "")
                print("text:", json.dumps(summary)[:400])
    except Exception as e:
        print("ended:", type(e).__name__, str(e)[:300])
    if audio:
        out = out_dir / f"probe_{msg_type}.wav"
        with wave.open(str(out), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(24000)
            w.writeframes(bytes(audio))
        print(f"saved {len(audio)} bytes of audio to {out}")


async def main():
    p = argparse.ArgumentParser(description="Probe the streaming TTS WebSocket protocol.")
    p.add_argument("--text", required=True)
    p.add_argument("--voice")
    p.add_argument("--types", nargs="+",
                   default=["Text", "Speak", "Synthesize", "Input", "Generate", "Flush"])
    a = p.parse_args()
    s = get_settings()
    out_dir = PROJECT_ROOT / "outputs"
    out_dir.mkdir(exist_ok=True)
    for t in a.types:
        await try_type(t, a.text, a.voice or s.tts_default_voice, s, out_dir)


if __name__ == "__main__":
    asyncio.run(main())
