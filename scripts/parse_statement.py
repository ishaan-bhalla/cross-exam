import argparse
from pathlib import Path
from crossexam.statements.parser import load_statement


def main():
    p = argparse.ArgumentParser(description="Parse a witness statement PDF into numbered paragraphs.")
    p.add_argument("--pdf", required=True, type=Path)
    a = p.parse_args()
    paragraphs = load_statement(a.pdf)
    print(f"{len(paragraphs)} paragraphs\n")
    for para in paragraphs[:3]:
        print(f"[{para['para_no']}] (page {para['page']}) {para['text']}\n")


if __name__ == "__main__":
    main()
