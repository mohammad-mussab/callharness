import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .analysis.worker import run_worker
from .config import settings
from .db import init_db
from .routes import alerts, analytics, calls, config, evaluators, gaps, testcalls
from .schemas import HealthOut

VERSION = "0.3.0"

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    stop_event = asyncio.Event()
    worker_task = asyncio.create_task(run_worker(stop_event))
    yield
    stop_event.set()
    await worker_task


app = FastAPI(
    title="CallHarness",
    description="Open-source call analytics for voice AI agents",
    version=VERSION,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(calls.router)
app.include_router(analytics.router)
app.include_router(gaps.router)
app.include_router(config.router)
app.include_router(alerts.router)
app.include_router(evaluators.router)
app.include_router(testcalls.router)


@app.get("/api/v1/health", response_model=HealthOut, tags=["health"])
async def health():
    return HealthOut(
        status="ok",
        version=VERSION,
        llm_provider=settings.resolved_provider,
        analysis_enabled=settings.analysis_enabled,
    )
