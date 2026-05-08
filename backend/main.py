import logging
import sys
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from database import verify_tables
from routers import auth, logs, messages, rules, settings_router
from services.scheduler import setup_scheduler

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("[STARTUP] WhatsApp AI Agent starting…")
    ok = await verify_tables()
    if not ok:
        logger.warning("[STARTUP] Some Supabase tables are missing — see output above.")
    setup_scheduler()
    logger.info("[STARTUP] Ready.")
    yield
    logger.info("[SHUTDOWN] Shutting down.")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="WhatsApp AI Agent", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000", "*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(messages.router)
app.include_router(auth.router)
app.include_router(rules.router)
app.include_router(settings_router.router)
app.include_router(logs.router)


@app.get("/health")
async def health():
    return {"status": "ok", "time": datetime.now().isoformat()}
