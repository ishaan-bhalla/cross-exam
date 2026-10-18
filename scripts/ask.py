import argparse
import asyncio
import wave
from pathlib import Path
from crossexam.config import get_settings
from crossexam.statements.parser import load_statement
from crossexam.retrieval.bm25 import BM25Index
from crossexam.witness.answer import answer
from crossexam.clients.tts import synthesize


def main():
    p = argparse.ArgumentParser(description="Ask a witness a question, answered from their statement.")
    p.add_argument("--pdf", required=True, type=Path)
    p.add_argument("--witness", required=True)
    p.add_argument("--question", required=True)
    p.add_argument("--speak", action="store_true")
    p.add_argument("--voice")
    p.add_argument("--out", default="answer.wav")
    a = p.parse_args()
    s = get_settings()

    hits = BM25Index(load_statement(a.pdf)).search(a.question)
    print("Retrieved paragraphs:")
    for h in hits:
        print(f"  [{h['para_no']}] score={h['score']:.2f} (page {h['page']}) {h['text'][:160]}")

    result = answer(a.question, hits, a.witness)
    print("\nLLM raw reply:\n" + result["raw"])
    print(f"\nAnswer (para {result['para_no']}): {result['spoken']}")
    print(f"verified: {result['verified']}")

    if a.speak:
        speech = asyncio.run(synthesize(result["spoken"], a.voice))
        out = s.resolve(s.outputs_dir) / a.out
        out.parent.mkdir(exist_ok=True)
        with wave.open(str(out), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(speech.sample_rate)
            w.writeframes(speech.audio)
        print(f"saved {out} ({speech.duration_ms} ms, first audio after {speech.time_to_first_audio_ms:.0f} ms)")


if __name__ == "__main__":
    main()
