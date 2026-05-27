"""FastAPI app: polls exchanges in the background, broadcasts reports over WS."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets
import sys
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

# Make local sibling modules importable regardless of how uvicorn is invoked.
# Works for both:
#   uvicorn app:app --app-dir backend       (legacy style)
#   uvicorn backend.app:app                 (run from project root)
# Also defeats accidental shadowing if the user pip-installed an unrelated
# package named "arbitrage" or "exchanges".
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from arbitrage import build_report
from exchanges import fetch_all  # also runs _load_dotenv() as a side-effect

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("app")

POLL_INTERVAL_S = 8  # exchanges throttle hard if we hammer them
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"


class Broker:
    """Fan-out the latest report to all connected websockets."""

    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self.latest: dict | None = None
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self.clients.add(ws)
        if self.latest is not None:
            try:
                await ws.send_text(json.dumps(self.latest))
            except Exception:  # noqa: BLE001
                pass

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self.clients.discard(ws)

    async def broadcast(self, payload: dict) -> None:
        self.latest = payload
        msg = json.dumps(payload)
        dead: list[WebSocket] = []
        for ws in list(self.clients):
            try:
                await ws.send_text(msg)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self.clients.discard(ws)


broker = Broker()


def _report_to_dict(report) -> dict:
    return {
        "funding": [asdict(r) for r in report.funding],
        "divergence": [asdict(r) for r in report.divergence],
        "basis": [asdict(r) for r in report.basis],
        "cross_spot": [asdict(r) for r in report.cross_spot],
        "cross_perp": [asdict(r) for r in report.cross_perp],
        "errors": report.errors,
        "stats": report.stats,
        "generated_at_ms": report.generated_at_ms,
    }


async def poll_loop() -> None:
    while True:
        started = asyncio.get_event_loop().time()
        try:
            snaps = await fetch_all()
            report = build_report(snaps)
            payload = _report_to_dict(report)
            log.info(
                "snapshot: %d tickers | funding=%d div=%d basis=%d cross_spot=%d cross_perp=%d errors=%s",
                report.stats.get("tickers_total", 0),
                len(report.funding), len(report.divergence), len(report.basis),
                len(report.cross_spot), len(report.cross_perp),
                list(report.errors.keys()),
            )
            await broker.broadcast(payload)
        except Exception:  # noqa: BLE001
            log.exception("poll iteration failed")
        elapsed = asyncio.get_event_loop().time() - started
        await asyncio.sleep(max(1.0, POLL_INTERVAL_S - elapsed))


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(poll_loop())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


class BasicAuthMiddleware:
    """Raw-ASGI middleware that enforces HTTP Basic Auth on every request,
    including WebSocket upgrades. Activates only when AUTH_USER + AUTH_PASS
    are set in the environment — otherwise it's a no-op."""

    def __init__(self, app, user: str, password: str) -> None:
        self.app = app
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self._expected = f"Basic {token}"

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        header_val = ""
        for name, value in scope.get("headers", []):
            if name == b"authorization":
                header_val = value.decode("latin-1")
                break
        if not header_val or not secrets.compare_digest(header_val, self._expected):
            if scope["type"] == "http":
                await send({
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"text/plain; charset=utf-8"),
                        (b"www-authenticate", b'Basic realm="crypto-arb"'),
                    ],
                })
                await send({"type": "http.response.body", "body": b"Unauthorized"})
            else:
                # ws — close with policy violation
                await send({"type": "websocket.close", "code": 1008})
            return
        await self.app(scope, receive, send)


app = FastAPI(lifespan=lifespan, title="Crypto Arbitrage Dashboard")

_auth_user = os.environ.get("AUTH_USER")
_auth_pass = os.environ.get("AUTH_PASS")
if _auth_user and _auth_pass:
    app.add_middleware(BasicAuthMiddleware, user=_auth_user, password=_auth_pass)
    log.info("Basic Auth enabled (user=%r)", _auth_user)
else:
    log.warning("AUTH_USER/AUTH_PASS not set — dashboard is unauthenticated")


@app.get("/", response_class=HTMLResponse)
async def root() -> HTMLResponse:
    html = (FRONTEND_DIR / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/api/snapshot")
async def snapshot() -> dict:
    if broker.latest is None:
        snaps = await fetch_all()
        report = build_report(snaps)
        broker.latest = _report_to_dict(report)
    return broker.latest


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    await broker.connect(ws)
    try:
        while True:
            # we don't expect inbound messages; just keep the socket alive
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await broker.disconnect(ws)


# Static assets (CSS/JS) live under /static
app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")
