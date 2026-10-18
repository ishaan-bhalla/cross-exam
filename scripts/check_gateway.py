import argparse
from crossexam.config import get_settings
from crossexam.clients.llm_gateway import chat


def main():
    p = argparse.ArgumentParser(description="Check the LLM Gateway responds.")
    p.add_argument("--witness", required=True)
    p.add_argument("--question", required=True)
    a = p.parse_args()
    s = get_settings()
    template = (s.resolve(s.prompts_dir) / "witness_system.txt").read_text()
    system = template.format(witness_name=a.witness)
    print(chat([
        {"role": "system", "content": system},
        {"role": "user", "content": a.question},
    ]))


if __name__ == "__main__":
    main()
