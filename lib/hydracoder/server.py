"""hydracoder web server: serves the dark UI and streams journal events live.

Deliberately dependency-light: stdlib http.server for static files + the
`websockets` library (the only third-party piece, already present) for the live
event channel. No FastAPI/uvicorn, so the .deb stays small and installs on any
hardware (the portability goal).

Architecture:
  - HTTP (a background thread) serves lib/hydracoder/web/* (the UI).
  - WebSocket (asyncio) pushes every journal Event to connected browsers and
    receives two message kinds from the browser:
        {"type": "goal", "goal": "...", "models_dir": "..."}  -> start a run
        {"type": "chat", "text": "..."}                        -> boss model
        {"type": "control", "action": "...", "args": {...}}    -> control tool
  - The run executes in a worker thread; its journal is subscribed so every
    event is forwarded to the socket as it is appended (live terminals).

This module is intentionally small and synchronous-friendly; the heavy lifting
is in the orchestrator. The UI is a pure view over the journal.
"""
from __future__ import annotations

import asyncio
import json
import threading
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from typing import Optional

from .orchestrator import Orchestrator
from .scheduler import Roster
from . import boss as boss_mod

WEB_DIR = Path(__file__).resolve().parent / "web"


def _http_thread(host: str, http_port: int) -> HTTPServer:
    handler = partial(SimpleHTTPRequestHandler, directory=str(WEB_DIR))
    httpd = HTTPServer((host, http_port), handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd


class HydracoderServer:
    def __init__(self, run_dir: Path, workspace: Path,
                 host: str = "127.0.0.1", http_port: int = 8765,
                 ws_port: int = 8766, roster: Optional[Roster] = None):
        self.run_dir = Path(run_dir)
        self.workspace = Path(workspace)
        self.host = host
        self.http_port = http_port
        self.ws_port = ws_port
        self.roster = roster
        self._clients: set = set()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._orch: Optional[Orchestrator] = None
        self._run_thread: Optional[threading.Thread] = None

    # --- broadcast ---------------------------------------------------------

    def _broadcast(self, obj: dict) -> None:
        """Thread-safe push to all browsers (called from the run thread)."""
        if not self._loop:
            return
        data = json.dumps(obj, ensure_ascii=False)
        for ws in list(self._clients):
            asyncio.run_coroutine_threadsafe(self._safe_send(ws, data), self._loop)

    async def _safe_send(self, ws, data: str) -> None:
        try:
            await ws.send(data)
        except Exception:
            self._clients.discard(ws)

    # --- run control -------------------------------------------------------

    def _start_run(self, goal: str) -> None:
        if self._run_thread and self._run_thread.is_alive():
            self._broadcast({"kind": "notice", "data": {"msg": "a run is already in progress"}})
            return
        self._orch = Orchestrator(self.run_dir, self.workspace, roster=self.roster,
                                  log=lambda m: self._broadcast({"kind": "log", "data": {"msg": m}}))
        # forward every journal event to browsers as it is appended
        self._orch.journal.subscribe(
            lambda ev: self._broadcast({"kind": ev.kind, "seq": ev.seq,
                                        "ts": ev.ts, "data": ev.data}))

        def _go():
            try:
                self._orch.run(goal)
            except Exception as e:  # surface, never crash the server
                self._broadcast({"kind": "error", "data": {"where": "run", "message": str(e)}})

        self._run_thread = threading.Thread(target=_go, daemon=True)
        self._run_thread.start()

    def _handle_chat(self, text: str) -> None:
        """Route a chat-box message to the boss model; it may answer or call a
        control tool. Runs in a thread so the socket stays responsive."""
        def _go():
            try:
                reply = boss_mod.handle_chat(self, text)
                self._broadcast({"kind": "chat_reply", "data": {"text": reply}})
            except Exception as e:
                self._broadcast({"kind": "error", "data": {"where": "chat", "message": str(e)}})
        threading.Thread(target=_go, daemon=True).start()

    # --- websocket server --------------------------------------------------

    async def _ws_handler(self, ws):
        self._clients.add(ws)
        # On connect, replay the existing journal so a late browser sees history.
        try:
            from .journal import Journal
            j = Journal(self.run_dir)
            for ev in j.replay():
                await ws.send(json.dumps({"kind": ev.kind, "seq": ev.seq,
                                          "ts": ev.ts, "data": ev.data}))
        except Exception:
            pass
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                kind = msg.get("type")
                if kind == "goal" and msg.get("goal"):
                    if msg.get("models_dir"):
                        self.workspace = Path(msg["models_dir"])
                    self._start_run(msg["goal"])
                elif kind == "chat" and msg.get("text"):
                    self._handle_chat(msg["text"])
                elif kind == "control" and msg.get("action"):
                    boss_mod.apply_control(self, msg["action"], msg.get("args", {}))
        finally:
            self._clients.discard(ws)

    def serve_forever(self) -> None:
        import websockets  # local import so the module loads without it for tests
        httpd = _http_thread(self.host, self.http_port)
        print(f"hydracoder UI:  http://{self.host}:{self.http_port}/")
        print(f"hydracoder is provided AS IS, WITHOUT WARRANTY OF ANY KIND. "
              f"Local models will read/write files and run shell commands in "
              f"{self.workspace}. You accept all risk.")

        async def _main():
            self._loop = asyncio.get_running_loop()
            async with websockets.serve(self._ws_handler, self.host, self.ws_port):
                await asyncio.Future()  # run forever

        try:
            asyncio.run(_main())
        except KeyboardInterrupt:
            pass
        finally:
            httpd.shutdown()
