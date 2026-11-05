# syntax=docker/dockerfile:1
#
# pdf-mcp (Kaperkunde fork) as a Streamable HTTP MCP server.
#
# Pinned: upstream nfsarch33/pdf-mcp-server v2.0.0
#   (tag object 007d6be3dba0a84aa94c50c70db61b3916431ff4,
#    commit f5f3cee3b17f24ea8221058702c4163370ce2c90), plus this fork's
#   commits (see KAPERKUNDE.md). Python packages come from uv.lock only
#   (`uv sync --frozen`); no setup script, nothing piped into a shell.
#
# Run (one data volume, loopback only):
#   docker run -d --name pdf-mcp -p 127.0.0.1:8000:8000 -v pdf-mcp-data:/data \
#     ghcr.io/kaperkunde/pdf-mcp-server:<tag>
# Every tool path must be under /data (relative paths are taken from /data).
#
# The image bundles PyMuPDF (AGPL-3.0); see NOTICE before redistributing it.

ARG PYTHON_IMAGE=docker.io/library/python:3.12-slim-trixie@sha256:e695ab189fa2d082b1c61c69a6b888a5adeca61ceeccad107b1ed2a125721a9e
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.11.32@sha256:df4cae8f3a96d175e2e5f992e597550000edbe78fdc2594d5cd8de1a217f504c

FROM ${UV_IMAGE} AS uv

# ---- build: the virtual environment from uv.lock ------------------------------
FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3 \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
# Dependencies first (cached layer), exactly as locked: the ocr extra only,
# no dev tools, no llm extra (openai).
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --extra ocr
COPY README.md LICENSE NOTICE ./
COPY pdf_mcp ./pdf_mcp
RUN uv sync --frozen --no-editable --extra ocr \
    && /opt/venv/bin/python -c "import pdf_mcp.kk_http, pymupdf, pytesseract"

# ---- runtime -----------------------------------------------------------------
FROM ${PYTHON_IMAGE}
LABEL org.opencontainers.image.source="https://github.com/kaperkunde/pdf-mcp-server" \
      org.opencontainers.image.description="pdf-mcp (reviewed Kaperkunde fork) over Streamable HTTP, tools confined to /data" \
      org.opencontainers.image.licenses="Apache-2.0 AND AGPL-3.0-only"

# poppler-utils (pdf2image, used by fillpdf), Tesseract with English and Dutch.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        poppler-utils tesseract-ocr tesseract-ocr-eng tesseract-ocr-nld \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system --gid 10001 pdfmcp \
    && useradd --system --uid 10001 --gid pdfmcp --no-create-home \
        --home-dir /nonexistent --shell /usr/sbin/nologin pdfmcp \
    && mkdir /data && chown pdfmcp:pdfmcp /data

COPY --from=build /opt/venv /opt/venv

# PDF_MCP_ENABLE_REMOTE_LLM, OPENAI_API_KEY and PDF_MCP_ENABLE_AI_TOOLS are
# deliberately left unset; openai is not installed.
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOME=/tmp \
    PDF_MCP_DATA_DIR=/data \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8000 \
    MCP_PATH=/mcp

VOLUME ["/data"]
WORKDIR /data
USER 10001:10001
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ.get('MCP_PORT', '8000'), timeout=4)"]

ENTRYPOINT ["python", "-m", "pdf_mcp.kk_http"]
