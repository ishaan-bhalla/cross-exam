import argparse
import asyncio
import wave
from pathlib import Path
from crossexam.config import get_settings
from crossexam.statements.parser import load_statement
from crossexam.retrieval.retriever import Retriever
from crossexam.witness.answer import answer
from crossexam.clients.tts import synthesize


def main():
    p = argparse.ArgumentParser(description="Ask a witness a question, answered from their statement.")
    p.add_argument("--pdf", required=True, type=Path)
    p.add_argument("--witness", required=True)
    p.add_argument("--question", required=True)
    p.add_argument("--demeanour")
    p.add_argument("--retriever", help="bm25 | dense | hybrid (default: RETRIEVER setting)")
    p.add_argument("--hyde", action=argparse.BooleanOptionalAction, help="override HYDE_ENABLED")
    p.add_argument("--speak", action="store_true")
    p.add_argument("--voice")
    p.add_argument("--out", default="answer.wav")
    a = p.parse_args()
    s = get_settings()

    retriever = Retriever(load_statement(a.pdf), a.pdf.stem, kind=a.retriever, hyde=a.hyde)
    retrieval = retriever.retrieve(a.question)
    if retriever.hyde:
        print(f"HyDE query: {retrieval.query}" + (f"  (failed: {retrieval.hyde_error})" if retrieval.hyde_error else ""))
    print(f"Retrieved paragraphs ({retriever.kind}; gate {retrieval.gate_score:.3f} vs min {retriever.min_score}):")
    for h in retrieval.hits:
        print(f"  [{h['para_no']}] score={h['score']:.3f} (page {h['page']}) {h['text'][:160]}")

    result = answer(a.question, retriever, a.witness, a.demeanour, retrieval=retrieval)
    print("\nLLM raw reply:\n" + (result["raw"] or "(LLM skipped: retrieval below refusal gate)"))
    print(f"\nAnswer (para {result['para_no']}): {result['spoken']}")
    print(f"verified: {result['verified']}  para_corrected: {result['para_corrected']}  "
          f"skipped_llm: {result['skipped_llm']}")

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
