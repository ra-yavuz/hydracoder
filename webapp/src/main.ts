/* hydracoder web UI entry: wires the websocket stream into the workspace.
   The UI is a pure projection of the journal event stream; on (re)connect the
   server replays history, so every render path must be idempotent from a
   fresh resetRun(). */

import "@fontsource/ibm-plex-mono/400.css";
import "@fontsource/ibm-plex-mono/400-italic.css";
import "@fontsource/ibm-plex-mono/600.css";
import "@fontsource/chakra-petch/500.css";
import "@fontsource/chakra-petch/700.css";
import "./style.css";

import { resetRun, state } from "./state";
import type { ServerMessage, TaskState, WorkerEvent } from "./types";
import { bossMsg, sysMsg, youMsg } from "./ui/boss";
import { byId, el } from "./ui/dom";
import { drawGraph, updateNode } from "./ui/graph";
import { initRoster, renderRoster } from "./ui/roster";
import {
  clearWindows, createWindow, finalGateResult, initWindows,
  reviewLine, setTaskState, workerEvent,
} from "./ui/windows";
import { Link } from "./ws";

const link = new Link(handle, setConn);

function setConn(up: boolean): void {
  const conn = byId("conn");
  conn.dataset.state = up ? "on" : "off";
  conn.textContent = up ? "linked" : "no link";
}

let statusTimer: number | undefined;
function status(text: string): void {
  const line = byId("statusline");
  line.textContent = text;
  line.classList.add("is-fresh");
  clearTimeout(statusTimer);
  statusTimer = window.setTimeout(() => line.classList.remove("is-fresh"), 4000);
}

function banner(ok: boolean, summary: string): void {
  byId("run-banner")?.remove();
  const b = el("div", `run-banner ${ok ? "ok" : "bad"}`);
  b.id = "run-banner";
  b.textContent = `${ok ? "✓ run complete" : "✗ run incomplete"}: ${summary}`;
  byId("terminals").prepend(b);
}

// ---- inbound event dispatch -------------------------------------------------

function handle(m: ServerMessage): void {
  const d = m.data ?? {};
  switch (m.kind) {
    case "run_started":
      resetRun();
      clearWindows();
      drawGraph();
      status("run started");
      break;

    case "plan_created": {
      state.architecture = d.architecture ?? "";
      byId("arch").textContent = state.architecture;
      const tasks = d.tasks ?? [];
      tasks.forEach((spec: any, i: number) => {
        state.order.push(spec.id);
        state.tasks.set(spec.id, { spec, state: "queued" });
        createWindow(spec, i);
      });
      drawGraph();
      break;
    }

    case "task_state": {
      const t = state.tasks.get(d.task_id);
      if (t) {
        t.state = d.state as TaskState;
        if (d.model) t.model = d.model;
      }
      setTaskState(d.task_id, d.state, d.detail, d.model);
      updateNode(d.task_id, d.state);
      break;
    }

    case "worker_event":
      workerEvent(d.task_id, (d.event ?? {}) as WorkerEvent);
      break;

    case "review":
      reviewLine(d.task_id, !!d.passed, d.notes ?? "", d.attempt);
      break;

    case "log":
      sysMsg(d.msg);
      status(d.msg);
      break;

    case "decision":
      sysMsg(`decision: ${d.what ?? ""} - ${d.why ?? ""}`);
      if (d.what === "roster") parseRosterDecision(d.why ?? "");
      break;

    case "error":
      sysMsg(`error @ ${d.where ?? "?"}: ${d.message ?? ""}`);
      status(`error @ ${d.where ?? "?"}`);
      break;

    case "control":
      sysMsg(`control: ${d.action ?? ""}`);
      break;

    case "chat_reply":
      bossMsg(d.text);
      break;

    case "run_finished":
      state.runFinished = { ok: !!d.ok, summary: d.summary ?? "" };
      finalGateResult(!!d.ok);
      banner(!!d.ok, d.summary ?? "");
      sysMsg(`run finished: ${d.summary ?? ""}${d.ok ? " [ok]" : " [incomplete]"}`);
      status(d.ok ? "run complete" : "run incomplete");
      break;

    case "notice":
      sysMsg(d.msg);
      status(d.msg);
      break;

    case "models":
      state.models = d.models ?? [];
      renderRoster();
      break;

    case "roster":
      state.roster = { ...state.roster, ...d };
      renderRoster();
      break;
  }
}

/** The roster decision event encodes the chosen models in its why-string
    ("small=X worker=Y reviewer=Z boss=W"); mirror it into the roster panel. */
function parseRosterDecision(why: string): void {
  for (const part of why.split(/\s+/)) {
    const [role, id] = part.split("=");
    if (id && ["small", "worker", "reviewer", "boss"].includes(role)) {
      (state.roster as Record<string, string>)[role] = id;
    }
  }
  renderRoster();
}

// ---- outbound wiring ---------------------------------------------------------

function submitGoal(): void {
  const goalEl = byId<HTMLTextAreaElement>("goal");
  const goal = goalEl.value.trim();
  if (!goal) {
    goalEl.focus();
    return;
  }
  const dir = byId<HTMLInputElement>("models-dir").value.trim();
  if (link.send({ type: "goal", goal, ...(dir ? { models_dir: dir } : {}) })) {
    sysMsg("goal submitted");
  } else {
    sysMsg("no link: goal not sent");
  }
}

function sendChat(): void {
  const input = byId<HTMLInputElement>("chat");
  const text = input.value.trim();
  if (!text) return;
  youMsg(text);
  link.send({ type: "chat", text });
  input.value = "";
}

byId("build").addEventListener("click", submitGoal);
byId<HTMLTextAreaElement>("goal").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) submitGoal();
});
byId("send").addEventListener("click", sendChat);
byId<HTMLInputElement>("chat").addEventListener("keydown", (e) => {
  if (e.key === "Enter") sendChat();
});
document.querySelectorAll<HTMLButtonElement>(".ctl[data-action]").forEach((b) => {
  b.addEventListener("click", () => {
    link.send({ type: "control", action: b.dataset.action!, args: {} });
    sysMsg(`control: ${b.dataset.action}`);
  });
});

initWindows();
initRoster((o) => link.send(o));
link.connect();
