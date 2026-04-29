FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml .
RUN pip install --no-cache-dir -e .
# Pre-download the embedding model so it's baked into the image
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"
COPY src/ src/
EXPOSE 8000
CMD ["uvicorn", "memory_mcp.server:app", "--host", "0.0.0.0", "--port", "8000"]
