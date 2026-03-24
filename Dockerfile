FROM litestream/litestream:0.3.13 AS litestream

FROM python:3.12-slim

WORKDIR /app

# Build deps for native extensions (gcc needed by some indirect deps)
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy package metadata first so the pip layer is cached independently of
# source changes.
COPY --from=litestream /usr/local/bin/litestream /usr/local/bin/litestream
COPY litestream.yml /etc/litestream.yml

# ── Layer 1: dependencies (cached until requirements.txt changes) ─────────────
# Copy only the dependency manifests — no source files — so this layer is not
# invalidated by code changes.
COPY requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.txt

# Set HOME/USER before model download so the cache lands in /app/.cache.
ENV HOME=/app \
    USER=appuser \
    LOGNAME=appuser

# ── Layer 2: ML models (cached until requirements.txt changes) ────────────────
# This expensive step only re-runs when dependencies change, not on source edits.
# - all-MiniLM-L6-v2: primary sentence embedding model (~90MB)
# - gte-reranker-modernbert-base: cross-encoder reranker for retrieval quality (~570MB)
# - msmarco-t5-small-v1: doc2query synthetic query generation at write time (~80MB)
RUN python -c "from sentence_transformers import SentenceTransformer; \
               SentenceTransformer('all-MiniLM-L6-v2')" \
 && python -c "from sentence_transformers import CrossEncoder; \
               CrossEncoder('Alibaba-NLP/gte-reranker-modernbert-base')" \
 && python -c "from transformers import T5ForConditionalGeneration, AutoTokenizer; \
               T5ForConditionalGeneration.from_pretrained('doc2query/msmarco-t5-small-v1'); \
               AutoTokenizer.from_pretrained('doc2query/msmarco-t5-small-v1')"

# Fix ownership: models downloaded as root; runtime UID is 1000.
RUN chown -R 1000:1000 /app/.cache

# ── Layer 3: package source (invalidated on code changes, fast to rebuild) ────
COPY README.md LICENSE ./
COPY vaire/ vaire/

# Install the package itself without re-downloading dependencies.
RUN pip install --no-cache-dir --no-deps .

# Prevent HuggingFace from contacting the Hub at startup — all models are
# already in the image.  This eliminates the unauthenticated-request warning
# and removes the network round-trip on every container start.
ENV TRANSFORMERS_OFFLINE=1 \
    HF_HUB_OFFLINE=1

# /data is bind-mounted from the host's ~/.vaire at runtime.
# Both the SQLite database and the Unix domain socket land here so the
# host-side thin client can reach the socket at ~/.vaire/vaire.sock.
RUN mkdir -p /data

ENV VAIRE_DB_PATH=/data/memory.db
ENV VAIRE_SOCKET_PATH=/data/vaire.sock
ENV VAIRE_PID_FILE=/data/vaire.pid

RUN mkdir -p /data/replicas

# litestream wraps the server process: streams WAL changes to /data/replicas/
# then forwards all signals to the child so graceful shutdown still works.
ENTRYPOINT ["litestream", "replicate", "-config", "/etc/litestream.yml", "-exec", "python -m vaire server"]
