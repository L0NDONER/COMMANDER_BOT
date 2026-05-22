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
