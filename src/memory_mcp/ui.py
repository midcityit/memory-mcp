import os
from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
import httpx

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

MCP_URL = os.environ.get("MCP_URL", "http://localhost:8000")
API_TOKEN = os.environ.get("API_TOKEN", "")
_headers = {"Authorization": f"Bearer {API_TOKEN}"}

app = FastAPI(title="memory-ui")


def _get(path: str, **params) -> dict:
    with httpx.Client() as client:
        resp = client.get(f"{MCP_URL}{path}", headers=_headers, params={k: v for k, v in params.items() if v})
        resp.raise_for_status()
        return resp.json()


def _post(path: str, json: dict) -> dict:
    with httpx.Client() as client:
        resp = client.post(f"{MCP_URL}{path}", headers=_headers, json=json)
        resp.raise_for_status()
        return resp.json()


@app.get("/", response_class=HTMLResponse)
def index(request: Request, q: str = "", type: str = "", source_repo: str = ""):
    if q:
        data = _post("/memories/search", {"query": q, "limit": 50,
                                           "filter_type": type or None,
                                           "filter_source_repo": source_repo or None})
    else:
        data = _get("/memories", type=type, source_repo=source_repo)
    memories = data.get("memories", [])
    all_data = _get("/memories")
    repos = sorted({m["source_repo"] for m in all_data.get("memories", [])})
    return templates.TemplateResponse("index.html", {
        "request": request, "memories": memories,
        "q": q, "filter_type": type, "filter_source_repo": source_repo,
        "repos": repos,
    })


@app.get("/memories/{memory_id}", response_class=HTMLResponse)
def memory_detail(request: Request, memory_id: str):
    data = _get("/memories")
    memories = data.get("memories", [])
    memory = next((m for m in memories if m["id"] == memory_id), None)
    if not memory:
        return HTMLResponse("Not found", status_code=404)
    return templates.TemplateResponse("memory.html", {"request": request, "memory": memory})
