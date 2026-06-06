"""
FastAPI front door for flaz.co.uk.

Single POST endpoint takes a photo + buy price, runs the same consensus
pipeline the Telegram bot uses, and returns the verdict as JSON.
"""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.exceptions import HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

import database
from navigation.router import router as nav_router
from services.ebay.scout_async import evaluate_with_consensus_saas

LOGGER = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await database.init_db()
    yield
    await database.checkpoint()      # truncate WAL into the .db before shutdown


app = FastAPI(title="Flaz", lifespan=lifespan)
app.include_router(nav_router)


@app.exception_handler(404)
async def not_found(_req: Request, _exc: HTTPException) -> HTMLResponse:
    path = _req.url.path
    return HTMLResponse(
        f"""<!DOCTYPE html><html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Not found — flaz</title>
<style>
  body{{margin:0;background:#0e0e0e;color:#f0f0f0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
       display:flex;flex-direction:column;align-items:center;justify-content:center;min-height:100dvh;gap:12px;padding:24px}}
  h1{{font-size:4rem;font-weight:700;color:#4a9eff;margin:0}}
  p{{color:#888;font-size:1rem;margin:0}}
  code{{background:#1a1a1a;border:1px solid #333;border-radius:6px;padding:2px 8px;font-size:0.9rem;color:#f0f0f0}}
  a{{color:#4a9eff;text-decoration:none}}a:hover{{text-decoration:underline}}
</style></head><body>
<h1>404</h1>
<p><code>{path}</code> not found</p>
<p><a href="/">← flaz.co.uk</a></p>
</body></html>""",
        status_code=404,
    )


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.get("/manifest.json")
async def manifest() -> FileResponse:
    return FileResponse(WEB_DIR / "manifest.json", media_type="application/manifest+json")


@app.get("/icons/{name}")
async def icons(name: str) -> FileResponse:
    path = WEB_DIR / "icons" / name
    if not path.is_file() or ".." in name:
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(path, media_type="image/png")


@app.post("/api/log-buy")
async def log_buy(
    query: str = Form(...),
    buy_price: float = Form(...),
    median: float = Form(None),
    verdict: str = Form(None),
) -> JSONResponse:
    try:
        row_id = await database.log_buy(
            chat_id="flaz",
            query=query,
            buy_price=buy_price,
            median=median,
            verdict=verdict,
            raw=f"flaz web buy_price={buy_price}",
        )
        return JSONResponse({"status": "ok", "id": row_id})
    except Exception as exc:
        LOGGER.exception("log_buy failed")
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=500)


@app.post("/api/evaluate")
async def evaluate(
    image: UploadFile = File(...),
    price: float = Form(...),
) -> JSONResponse:
    path = f"/tmp/{uuid4().hex}.jpg"
    try:
        with open(path, "wb") as f:
            f.write(await image.read())
        result = await evaluate_with_consensus_saas(path, str(price))
        return JSONResponse(result)
    except Exception as exc:
        LOGGER.exception("web evaluate failed")
        return JSONResponse(
            {"status": "error", "message": str(exc)},
            status_code=500,
        )
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
