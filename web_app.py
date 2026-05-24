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

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, JSONResponse

import database
from services.ebay.scout_async import evaluate_with_consensus_saas

LOGGER = logging.getLogger(__name__)

WEB_DIR = Path(__file__).parent / "web"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await database.init_db()
    yield


app = FastAPI(title="Flaz", lifespan=lifespan)


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
