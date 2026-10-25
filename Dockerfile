FROM python:3.15-slim@sha256:533b0b2f9b5d46fd097091cf826f27902f1d6c3bca714b6dc74551356aa97a51
WORKDIR /app

RUN pip install --no-cache-dir --upgrade pip

COPY pyproject.toml .
COPY src/ src/
RUN pip install --no-cache-dir .

ENV HF_HOME=/app/.cache/huggingface
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

RUN addgroup --system app && adduser --system --ingroup app --home /app app \
    && chown -R app:app /app
USER app

EXPOSE 8000
CMD ["uvicorn", "memory_mcp.server:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
