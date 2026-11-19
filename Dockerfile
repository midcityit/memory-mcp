FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b
WORKDIR /app

RUN pip install --no-cache-dir --upgrade pip

COPY pyproject.toml .
COPY src/ src/
RUN pip install --no-cache-dir ".[postgres]"

ENV HF_HOME=/app/.cache/huggingface
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

RUN addgroup --system app && adduser --system --ingroup app --home /app app \
    && chown -R app:app /app
USER app

EXPOSE 8000
CMD ["uvicorn", "memory_mcp.server:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
