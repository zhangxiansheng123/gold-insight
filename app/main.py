from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import __version__
from app.api import router as api_router
from app.config import settings
from app.db import init_db
from app.services.data_service import collect_live_quotes, ensure_bootstrap

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("gold-insight")

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
scheduler = AsyncIOScheduler()


async def _job_collect_quotes() -> None:
    try:
        items = await collect_live_quotes()
        logger.info("collected %s quotes", len(items))
    except Exception:  # noqa: BLE001
        logger.exception("quote collection failed")


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    logger.info("database ready")
    try:
        boot = await ensure_bootstrap()
        logger.info("bootstrap done: %s", {k: boot.get(k) for k in ("quotes", "history")})
    except Exception:  # noqa: BLE001
        logger.exception("bootstrap failed")

    scheduler.add_job(
        _job_collect_quotes,
        "interval",
        minutes=settings.quote_interval_minutes,
        id="collect_quotes",
        replace_existing=True,
    )
    scheduler.start()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(title=settings.app_name, version=__version__, lifespan=lifespan)
app.include_router(api_router)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "app_name": settings.app_name,
            "version": __version__,
        },
    )


def run() -> None:
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=False,
    )


if __name__ == "__main__":
    run()
