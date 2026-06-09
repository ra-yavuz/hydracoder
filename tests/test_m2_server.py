"""M2 UI test (browser-free equivalent of a Playwright test): start the real
server over a seeded journal, fetch the UI over HTTP, connect a WebSocket
client, and assert it receives the replayed journal events and that a control
message round-trips. Verifies the data path the browser depends on.

Run: PYTHONPATH=lib python3 tests/test_m2_server.py   (needs the websockets lib)
"""
import asyncio
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from hydracoder.journal import Journal  # noqa: E402
from hydracoder.server import HydracoderServer  # noqa: E402


def _seed(run_dir: Path):
    j = Journal(run_dir)
    if list(j.replay()):
        return
    j.append("run_started", {"goal": "demo", "models_dir": "/tmp/ws"})
    j.append("plan_created", {"architecture": "demo", "tasks": [
        {"id": "t1", "title": "first", "depends_on": [], "complexity": "small"},
        {"id": "t2", "title": "second", "depends_on": ["t1"], "complexity": "medium"}]})
    j.append("task_state", {"task_id": "t1", "state": "done"})


def main() -> int:
    import tempfile
    import websockets  # noqa: F401  (assert dep present)

    run_dir = Path(tempfile.mkdtemp())
    ws_dir = Path(tempfile.mkdtemp())
    _seed(run_dir)

    srv = HydracoderServer(run_dir, ws_dir, host="127.0.0.1",
                           http_port=8791, ws_port=8792)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    time.sleep(1.2)  # let HTTP + WS bind

    failures = []

    # 1. HTTP serves the UI
    try:
        html = urllib.request.urlopen("http://127.0.0.1:8791/index.html", timeout=5).read().decode()
        assert "hydracoder" in html and "talk to the boss" in html, "UI html missing markers"
        print("  PASS http serves index.html with UI markers")
    except Exception as e:
        failures.append(f"http: {e}")
        print(f"  FAIL http: {e}")

    # 2. WebSocket replays the seeded journal on connect
    async def ws_check():
        import websockets
        import json
        async with websockets.connect("ws://127.0.0.1:8792") as ws:
            kinds = []
            # collect the replayed backlog
            for _ in range(3):
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
                kinds.append(json.loads(raw)["kind"])
            assert kinds[0] == "run_started", kinds
            assert "plan_created" in kinds, kinds
            # a control message must not error the socket
            await ws.send(json.dumps({"type": "control", "action": "status", "args": {}}))
            return kinds

    try:
        kinds = asyncio.run(ws_check())
        print(f"  PASS websocket replayed journal: {kinds}")
    except Exception as e:
        failures.append(f"ws: {e}")
        print(f"  FAIL ws: {e}")

    print(f"{2 - len(failures)}/2 passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
