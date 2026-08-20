from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

from app.api.agent import router as agent_router
from app.api.documents import router as documents_router
from app.auth import configure_basic_auth
from app.db.database import Base, engine

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="InvoiceOps AI", version="0.1.0", lifespan=lifespan)

# Gate the whole app behind a shared username/password when configured
# (BASIC_AUTH_USERNAME + BASIC_AUTH_PASSWORD). No-op locally when unset.
configure_basic_auth(app)

app.include_router(documents_router)
app.include_router(agent_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/ui/")


# Serve the single-page UI. Mounted last so it never shadows API routes.
app.mount("/ui", StaticFiles(directory=STATIC_DIR, html=True), name="ui")
