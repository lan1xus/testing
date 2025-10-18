import asyncio
import logging
import time
from pathlib import Path
from typing import Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from bot.fortnite_bot import create_bot, start_bot, WebCtx, Bot

BASE_DIR = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

logger = logging.getLogger("fortnite_web_server")
logger.setLevel(logging.INFO)
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s"))
logger.addHandler(_handler)

app = FastAPI(title="Fortnite Bot Web UI")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


class BroadcastManager:
    def __init__(self) -> None:
        self._clients: Set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._clients.add(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            if ws in self._clients:
                self._clients.remove(ws)

    async def broadcast(self, message: str) -> None:
        async with self._lock:
            clients = list(self._clients)
        to_remove: Set[WebSocket] = set()
        for ws in clients:
            try:
                await ws.send_text(message)
            except Exception:
                to_remove.add(ws)
        if to_remove:
            async with self._lock:
                for ws in to_remove:
                    if ws in self._clients:
                        self._clients.remove(ws)


broadcaster = BroadcastManager()
bot: Bot = create_bot()
_bot_task: asyncio.Task | None = None


@app.on_event("startup")
async def on_startup() -> None:
    global _bot_task
    logger.info("Starting bot background task...")
    _bot_task = asyncio.create_task(start_bot(bot))


@app.on_event("shutdown")
async def on_shutdown() -> None:
    global _bot_task
    logger.info("Shutting down bot...")
    await bot.stop()
    if _bot_task:
        try:
            await asyncio.wait_for(_bot_task, timeout=5)
        except asyncio.TimeoutError:
            logger.warning("Bot task did not stop in time.")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse("index.html", {"request": request})


RATE_LIMIT_SECONDS = 0.3
_last_message_at: dict[int, float] = {}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await broadcaster.connect(ws)
    await ws.send_text("Connected to Fortnite Bot Web UI. Type !hello to test.")
    try:
        while True:
            text = await ws.receive_text()
            now = time.monotonic()
            key = id(ws)
            last = _last_message_at.get(key, 0)
            if now - last < RATE_LIMIT_SECONDS:
                await ws.send_text("Slow down! You're sending messages too quickly.")
                continue
            _last_message_at[key] = now

            # Build a context that broadcasts to all clients
            async def _send(msg: str) -> None:
                await broadcaster.broadcast(msg)

            ctx = WebCtx(send_func=_send, author={"id": str(key), "display_name": "WebUser"}, bot=bot)

            # Echo the user's message to all first
            await broadcaster.broadcast(f"You: {text}")

            # Dispatch to the bot
            await bot.dispatch_line(text, ctx)
    except WebSocketDisconnect:
        await broadcaster.disconnect(ws)
    except Exception as e:
        logger.exception("WebSocket error: %s", e)
        await broadcaster.disconnect(ws)
