# Cross-examine, for Render's free web service (512 MB). Settings come from deploy/render.env;
# the only secret, ASSEMBLYAI_API_KEY, is set in Render's dashboard.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    CROSSEXAM_ENV_FILE=deploy/render.env \
    # Fewer glibc malloc arenas: less memory held per thread (embedding, uploads, TTS)
    MALLOC_ARENA_MAX=2

RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr \
 && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 app
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Owned by the non-root user, which needs to write under data/ (phrase cache, budget counters).
COPY --chown=app . .
USER app
# The embedding model and the example witness's index are built into the image; nothing is
# synthesised (no API key at build time).
RUN ASSEMBLYAI_API_KEY=unused-at-build python scripts/prepare_image.py

EXPOSE 10000
# Render sets $PORT; the settings file's API_PORT is the fallback.
CMD ["sh", "-c", "API_PORT=${PORT:-10000} exec python scripts/serve.py"]
