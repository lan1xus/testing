from typing import Any, Awaitable, Callable, Dict, Optional

# Re-export WebCtx from the bot module to keep a single implementation
try:
    from bot.fortnite_bot import WebCtx as _WebCtx
    from bot.fortnite_bot import Bot as _Bot
except Exception:  # pragma: no cover - fallback if bot module is not available
    _WebCtx = object  # type: ignore
    _Bot = object  # type: ignore


class WebCtx(_WebCtx):  # type: ignore[misc]
    def __init__(self, send_func: Callable[[str], Awaitable[None]], author: Optional[Dict[str, str]] = None, bot: Optional[_Bot] = None) -> None:
        super().__init__(send_func, author=author, bot=bot)  # type: ignore[arg-type]
