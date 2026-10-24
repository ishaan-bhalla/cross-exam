"""Build-time preparation for the Docker image: download the embedding model into
embedding_cache_dir and build the example witness's dense index (processed_dir), so neither is
fetched or computed when the service starts. Fixed phrases are not synthesised here: that
needs the API key, which only exists at runtime. The Dockerfile runs this with a placeholder
key, since settings require one and nothing here calls AssemblyAI.

usage: ASSEMBLYAI_API_KEY=unused prepare_image.py
"""
from crossexam.api import witnesses
from crossexam.config import get_settings


def main():
    s = get_settings()
    for w in witnesses.list_witnesses():
        r = witnesses.retriever(w["id"])
        print(f"{w['id']}: {len(r.dense.vectors)} units indexed with {s.embedding_model} into {s.processed_dir}")


if __name__ == "__main__":
    main()
