import argparse
import asyncio
import wave
from crossexam.config import get_settings
from crossexam.clients.tts import synthesize


async def main():
    p = argparse.ArgumentParser(description="Synthesize speech and print word timings.")
    p.add_argument("--text", required=True)
    p.add_argument("--voice")
    p.add_argument("--out", default="say.wav")
    a = p.parse_args()
    s = get_settings()

    speech = await synthesize(a.text, a.voice)

    out = s.resolve(s.outputs_dir) / a.out
    out.parent.mkdir(exist_ok=True)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(speech.sample_rate)
        w.writeframes(speech.audio)

    print(f"saved {out}")
    print(f"duration:            {speech.duration_ms} ms")
    print(f"time to first audio: {speech.time_to_first_audio_ms:.0f} ms\n")
    print(f"{'word':<20}{'start':>8}{'end':>8}{'conf':>8}")
    for wd in speech.words:
        print(f"{wd['word']:<20}{wd['start']:>8}{wd['end']:>8}{wd['confidence']:>8.2f}")


if __name__ == "__main__":
    asyncio.run(main())
