"""Boss model: the chat box's brain and the conversational control surface.

When you type into the chat box mid-run, the boss model either answers or calls
a control tool to change how the run behaves. This is the "talk to the boss to
change settings" capability: there are no commands to memorize; the chat box is
the control surface, and the same actions exist as UI buttons.

Control actions (the closed set the boss can invoke, mirrored by UI buttons):
  set_model_for_role   {role: small|worker|reviewer|boss, model_id}
  set_models_folder    {path}
  pause                {}            stop scheduling new tasks (current finishes)
  resume               {}
  set_concurrency_mode {mode: routing|swarm|multi_gpu}
  status               {}           report current run state

The boss decides via a small tool schema. For a plain question it just answers.
"""
from __future__ import annotations

import json

from . import scheduler as sched
from hydra_llm import api as hydra


CONTROL_TOOLS = [
    {"type": "function", "function": {
        "name": "set_model_for_role",
        "description": "Pin a model id to a role (small/worker/reviewer/boss).",
        "parameters": {"type": "object", "properties": {
            "role": {"type": "string", "enum": ["small", "worker", "reviewer", "boss"]},
            "model_id": {"type": "string"}}, "required": ["role", "model_id"]}}},
    {"type": "function", "function": {
        "name": "set_concurrency_mode",
        "description": "Set how tasks run: routing (default), swarm, or multi_gpu.",
        "parameters": {"type": "object", "properties": {
            "mode": {"type": "string", "enum": ["routing", "swarm", "multi_gpu"]}},
            "required": ["mode"]}}},
    {"type": "function", "function": {
        "name": "pause", "description": "Pause scheduling of new tasks.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "resume", "description": "Resume scheduling.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "status", "description": "Report the current run state.",
        "parameters": {"type": "object", "properties": {}}}},
]


def apply_control(server, action: str, args: dict) -> str:
    """Apply a control action to the live server/orchestrator. Returns a short
    human-readable result string. Every action is journalled."""
    orch = server._orch
    if orch is not None:
        orch.journal.append("control", {"action": action, "args": args})

    if action == "set_model_for_role":
        role, mid = args.get("role"), args.get("model_id")
        if server.roster is None:
            server.roster = sched.choose_roster(hydra.list_models())
        if role in ("small", "worker", "reviewer", "boss") and mid:
            setattr(server.roster, role, mid)
            if orch is not None:
                orch._roster = server.roster
            return f"role {role} -> {mid}"
        return "bad role or model_id"

    if action == "set_concurrency_mode":
        mode = args.get("mode", "routing")
        server._mode = mode
        return f"concurrency mode -> {mode} (routing is the only mode with effect on a single GPU)"

    if action == "pause":
        server._paused = True
        return "paused: no new tasks will be scheduled"

    if action == "resume":
        server._paused = False
        return "resumed"

    if action == "status":
        if orch is None:
            return "no run in progress"
        st = orch.journal.reconstruct()
        tasks = st.get("tasks", {})
        by_state: dict[str, int] = {}
        for t in tasks.values():
            by_state[t.get("state", "?")] = by_state.get(t.get("state", "?"), 0) + 1
        return "tasks: " + ", ".join(f"{k}={v}" for k, v in sorted(by_state.items()))

    return f"unknown action: {action}"


def handle_chat(server, text: str) -> str:
    """Send the chat message to the boss model with control tools available. If
    the model calls a control tool, apply it and report; otherwise return its
    answer. Best-effort: if the boss model is not reachable, fall back to a
    direct keyword parse so basic control still works offline."""
    roster = server.roster or sched.choose_roster(hydra.list_models())
    server.roster = roster
    try:
        base = server._orch._base_url(roster.boss) if server._orch else _start_boss(roster.boss)
    except Exception as e:
        return _keyword_fallback(server, text, reason=f"(boss model unavailable: {e})")

    sys_prompt = ("You are the orchestrator's boss. The user is watching local "
                  "models build their project and may ask you to change settings "
                  "or answer questions. If they ask to change a model, mode, or "
                  "to pause/resume/report status, CALL the matching control tool. "
                  "Otherwise answer briefly.")
    payload_messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": text},
    ]
    # Use a tool-enabled completion via the raw endpoint.
    import urllib.request
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=json.dumps({"model": roster.boss, "messages": payload_messages,
                         "tools": CONTROL_TOOLS, "tool_choice": "auto",
                         "max_tokens": 400, "temperature": 0.2}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.loads(r.read().decode())
    msg = d["choices"][0]["message"]
    tcs = msg.get("tool_calls") or []
    if tcs:
        results = []
        for tc in tcs:
            fn = tc.get("function", {})
            try:
                a = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                a = {}
            results.append(apply_control(server, fn.get("name", ""), a))
        return " ; ".join(results)
    return msg.get("content") or "(no reply)"


def _start_boss(model_id: str) -> str:
    st = hydra.model_status(model_id)
    if st and st.get("ready") and st.get("port"):
        return f"http://127.0.0.1:{st['port']}/v1"
    info = hydra.start(model_id, wait=True, wait_timeout=180)
    return info["base_url"]


def _keyword_fallback(server, text: str, reason: str = "") -> str:
    t = text.lower()
    if "pause" in t:
        return apply_control(server, "pause", {}) + " " + reason
    if "resume" in t:
        return apply_control(server, "resume", {}) + " " + reason
    if "status" in t:
        return apply_control(server, "status", {}) + " " + reason
    return f"could not reach the boss model {reason}".strip()
