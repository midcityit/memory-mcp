import os
from fastapi import FastAPI, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse
import httpx

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

MCP_URL = os.environ.get("MCP_URL", "http://localhost:8000")

def _auth_headers() -> dict:
    token = os.environ.get("API_TOKEN", "")
    return {"Authorization": f"Bearer {token}"}

app = FastAPI(title="memory-ui")


def _get(path: str, **params) -> dict:
    try:
        with httpx.Client() as client:
            resp = client.get(
                f"{MCP_URL}{path}",
                headers=_auth_headers(),
                params={k: v for k, v in params.items() if v},
            )
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError:
        return {"memories": []}


def _post(path: str, json: dict) -> dict:
    try:
        with httpx.Client() as client:
            resp = client.post(f"{MCP_URL}{path}", headers=_auth_headers(), json=json)
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError:
        return {"memories": []}


def _get_one(path: str) -> dict | None:
    try:
        with httpx.Client() as client:
            resp = client.get(f"{MCP_URL}{path}", headers=_auth_headers())
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError:
        return None


@app.get("/", response_class=HTMLResponse)
def index(request: Request, q: str = "", type: str = "", source_repo: str = ""):
    if q:
        data = _post("/memories/search", {"query": q, "limit": 50,
                                           "filter_type": type or None,
                                           "filter_source_repo": source_repo or None})
    else:
        data = _get("/memories", type=type, source_repo=source_repo)
    memories = data.get("memories", [])
    if type or source_repo or q:
        all_data = _get("/memories")
        repos = sorted({m["source_repo"] for m in all_data.get("memories", [])})
    else:
        repos = sorted({m["source_repo"] for m in memories})
    return templates.TemplateResponse("index.html", {
        "request": request, "memories": memories,
        "q": q, "filter_type": type, "filter_source_repo": source_repo,
        "repos": repos,
    })


@app.get("/memories/{memory_id}", response_class=HTMLResponse)
def memory_detail(request: Request, memory_id: str):
    memory = _get_one(f"/memories/{memory_id}")
    if memory is None:
        return HTMLResponse("Not found", status_code=404)
    return templates.TemplateResponse("memory.html", {"request": request, "memory": memory})
