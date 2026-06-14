/* The workspace stage: one terminal window per task (plus a synthetic
   "final verification" window), streaming tokens and tool calls live.
   Click a window header to focus it (maximize over a backdrop); Escape or
   clicking the backdrop restores it. */

import { state } from "../state";
import type { TaskSpec, TaskState, WorkerEvent } from "../types";
import { byId, el } from "./dom";

interface WindowParts {
  card: HTMLElement;
  body: HTMLElement;
  badge: HTMLElement;
  model: HTMLElement;
}

const windows = new Map<string, WindowParts>();
let focused: string | null = null;
let placeholder: HTMLElement | null = null;
// Held as a reference: innerHTML="" detaches it from the DOM, after which a
// byId lookup would fail.
let emptyHint: HTMLElement;

const FINAL_ID = "(final)";

export function clearWindows(): void {
  unfocus();
  windows.clear();
  const stage = byId("terminals");
  stage.innerHTML = "";
  stage.appendChild(emptyHint);
  emptyHint.style.display = "none";
}

export function createWindow(spec: TaskSpec, index: number): void {
  emptyHint.style.display = "none";
  const card = el("article", "term");
  card.dataset.state = "queued";
  card.dataset.id = spec.id;
  card.style.animationDelay = `${Math.min(index * 60, 480)}ms`;

  const head = el("header", "term-head");
  const id = el("span", "term-id", spec.id);
  const title = el("span", "term-title", spec.title || spec.id);
  title.title = spec.description || "";
  const model = el("span", "term-model");
  const badge = el("span", "badge", "queued");
  badge.dataset.state = "queued";
  head.append(id, title, model, badge);
  card.appendChild(head);

  if (spec.depends_on?.length) {
    const deps = el("div", "term-deps");
    deps.append(el("b", "", "deps: "), spec.depends_on.join(", "));
    card.appendChild(deps);
  }

  const body = el("div", "term-body");
  card.appendChild(body);

  head.addEventListener("click", () => toggleFocus(spec.id));
  byId("terminals").appendChild(card);
  windows.set(spec.id, { card, body, badge, model });
}

/** The final deterministic verification gets its own window the first time a
    "(final)" review event arrives. It is the run's authoritative gate. */
function ensureFinalWindow(): WindowParts {
  let w = windows.get(FINAL_ID);
  if (w) return w;
  createWindow(
    { id: FINAL_ID, title: "final verification", description: "deterministic test gate" },
    windows.size,
  );
  w = windows.get(FINAL_ID)!;
  w.card.classList.add("term-final");
  return w;
}

export function setTaskState(id: string, taskState: TaskState, detail?: string, model?: string): void {
  const w = windows.get(id);
  if (!w) return;
  w.badge.dataset.state = taskState;
  w.badge.textContent = taskState;
  w.card.dataset.state = taskState;
  if (model) w.model.textContent = model;
  if (detail && /attempt [23]/.test(detail)) {
    append(w, `\n[retry: ${detail}]\n`, "tok");
  }
}

export function workerEvent(id: string, ev: WorkerEvent): void {
  const w = windows.get(id);
  if (!w) return;
  switch (ev.kind) {
    case "token":
      append(w, ev.text ?? "", "tok");
      break;
    case "tool_call":
      append(w, `\n⏳ ${ev.name}${argsLine(ev.args)}\n`, "tool-call");
      break;
    case "tool_result": {
      const mark = ev.ok ? "✓ " : "✗ ";
      const extra = !ev.ok && ev.result?.error ? `: ${ev.result.error}` : "";
      append(w, `${mark}${ev.name}${extra}\n`, ev.ok ? "tool-ok" : "tool-bad");
      break;
    }
    case "turn_end":
      append(w, "\n", "tok");
      break;
  }
}

export function reviewLine(id: string, passed: boolean, notes: string, attempt?: number): void {
  const w = id === FINAL_ID ? ensureFinalWindow() : windows.get(id);
  if (!w) return;
  if (id === FINAL_ID) {
    setTaskState(FINAL_ID, passed ? "done" : attempt ? "running" : "failed");
  }
  const line = el("div", `review-line ${passed ? "pass" : "fail"}`);
  const label = id === FINAL_ID ? `round ${attempt ?? "?"}` : "review";
  line.textContent = `${passed ? "✓" : "✗"} ${label}${notes ? `: ${notes}` : ""}`;
  w.body.appendChild(line);
  w.body.scrollTop = w.body.scrollHeight;
}

export function finalGateResult(ok: boolean): void {
  const w = windows.get(FINAL_ID);
  if (w) setTaskState(FINAL_ID, ok ? "done" : "failed");
}

export function scrollToWindow(id: string): void {
  const w = windows.get(id);
  if (!w) return;
  w.card.scrollIntoView({ behavior: "smooth", block: "nearest" });
  w.card.classList.add("term-ping");
  setTimeout(() => w.card.classList.remove("term-ping"), 900);
}

// ---- focus (maximize one window over a backdrop) --------------------------

function toggleFocus(id: string): void {
  if (focused === id) unfocus();
  else focus(id);
}

function focus(id: string): void {
  unfocus();
  const w = windows.get(id);
  if (!w) return;
  focused = id;
  placeholder = el("div", "term-placeholder");
  const rect = w.card.getBoundingClientRect();
  placeholder.style.height = `${rect.height}px`;
  w.card.parentElement?.insertBefore(placeholder, w.card);
  document.body.appendChild(w.card);
  w.card.classList.add("term-focused");
  byId("focus-backdrop").hidden = false;
  w.body.scrollTop = w.body.scrollHeight;
}

export function unfocus(): void {
  if (!focused) return;
  const w = windows.get(focused);
  focused = null;
  byId("focus-backdrop").hidden = true;
  if (!w) return;
  w.card.classList.remove("term-focused");
  if (placeholder?.parentElement) {
    placeholder.parentElement.replaceChild(w.card, placeholder);
  } else {
    byId("terminals").appendChild(w.card);
  }
  placeholder = null;
}

export function initWindows(): void {
  emptyHint = byId("empty-hint");
  byId("focus-backdrop").addEventListener("click", unfocus);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") unfocus();
  });
}

// ---- internals -------------------------------------------------------------

function argsLine(args?: Record<string, unknown>): string {
  if (!args) return "";
  const parts = Object.entries(args).map(([k, v]) => {
    let s = typeof v === "string" ? v : JSON.stringify(v);
    if (s.length > 40) s = `${s.slice(0, 40)}...`;
    return `${k}=${s}`;
  });
  const joined = parts.join(" ").slice(0, 120);
  return joined ? ` (${joined})` : "";
}

function append(w: WindowParts, text: string, cls: string): void {
  const span = el("span", cls, text);
  w.body.appendChild(span);
  // Cap DOM growth on very long runs: drop the oldest chunks.
  while (w.body.childNodes.length > 3000) w.body.removeChild(w.body.firstChild!);
  w.body.scrollTop = w.body.scrollHeight;
}

export function knownWindow(id: string): boolean {
  return windows.has(id) || id === FINAL_ID;
}

export function taskCount(): number {
  return state.tasks.size;
}
