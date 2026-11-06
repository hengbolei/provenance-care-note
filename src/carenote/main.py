import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from carenote import __version__
from carenote.api.container import reset_demo_state
from carenote.api.routes import router
from carenote.core.config import settings
from carenote.core.security import safe_request_logging

settings.validate_production()
logger = logging.getLogger("uvicorn.error")  # printed by uvicorn, e.g. in the Space log


async def _reset_demo_periodically(minutes: int) -> None:
    while True:
        await asyncio.sleep(minutes * 60)
        try:
            reset_demo_state()
            logger.info("public demo reset to the synthetic seed")
        except Exception:  # keep serving; the next interval tries again
            logger.exception("public demo reset failed")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    task = None
    if settings.public_demo:
        task = asyncio.create_task(_reset_demo_periodically(settings.demo_reset_minutes))
    try:
        yield
    finally:
        if task is not None:
            task.cancel()


app = FastAPI(title=settings.app_name, version=__version__, lifespan=lifespan)
app.middleware("http")(safe_request_logging)
app.include_router(router)

static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(static_dir / "index.html")
