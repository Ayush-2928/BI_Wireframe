import json
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

from database import create_tables
from routers import auth, wireframes, upload, charts, chat, export

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bi-agent")


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_tables()
    yield


app = FastAPI(title="BI Wireframe Agent", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()

    # Log incoming request
    body_bytes = await request.body()
    try:
        body_str = body_bytes.decode("utf-8")
        # Try to pretty-print JSON, fall back to raw string
        try:
            body_display = json.dumps(json.loads(body_str), separators=(",", ":"))
        except Exception:
            # For form-data (file uploads) just show content type
            body_display = request.headers.get("content-type", "")
    except Exception:
        body_display = ""

    log.info(f">>> {request.method} {request.url.path}  {body_display}")

    # Re-inject body so the route handler can still read it.
    # Important for multipart/form-data: return body only once.
    body_sent = False

    async def receive():
        nonlocal body_sent
        if body_sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        body_sent = True
        return {"type": "http.request", "body": body_bytes, "more_body": False}

    request._receive = receive

    # Call the actual route
    response: Response = await call_next(request)

    # Capture response body
    resp_body = b""
    async for chunk in response.body_iterator:
        resp_body += chunk

    elapsed = round((time.time() - start) * 1000)
    try:
        resp_display = json.dumps(json.loads(resp_body.decode()), separators=(",", ":"))
        # Truncate very long responses (e.g. large schema dumps)
        if len(resp_display) > 300:
            resp_display = resp_display[:300] + "..."
    except Exception:
        resp_display = f"[{response.status_code}]"

    log.info(f"<<< {response.status_code}  {resp_display}  ({elapsed}ms)\n")

    return Response(
        content=resp_body,
        status_code=response.status_code,
        headers=dict(response.headers),
        media_type=response.media_type,
    )


app.include_router(auth.router, prefix="/auth", tags=["auth"])
app.include_router(wireframes.router, prefix="/wireframes", tags=["wireframes"])
app.include_router(upload.router, prefix="/upload", tags=["upload"])
app.include_router(charts.router, prefix="/charts", tags=["charts"])
app.include_router(chat.router, prefix="/chat", tags=["chat"])
app.include_router(export.router, prefix="/export", tags=["export"])


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
