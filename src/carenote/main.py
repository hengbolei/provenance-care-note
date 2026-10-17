from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from carenote.api.routes import router
from carenote.core.config import settings
from carenote.core.security import safe_request_logging

settings.validate_production()
app = FastAPI(title=settings.app_name, version="0.1.0")
app.middleware("http")(safe_request_logging)
app.include_router(router)

static_dir = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(static_dir / "index.html")
