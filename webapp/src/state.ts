/* App state: a thin store the renderers subscribe to. The journal stream is
   the source of truth; this is its in-browser projection. */

import type { ModelEntry, Role, TaskSpec, TaskState } from "./types";

export interface TaskView {
  spec: TaskSpec;
  state: TaskState;
  model?: string;
}

export interface AppState {
  tasks: Map<string, TaskView>;
  order: string[];
  architecture: string;
  models: ModelEntry[];
  roster: Partial<Record<Role, string>>;
  runFinished: { ok: boolean; summary: string } | null;
}

export const state: AppState = {
  tasks: new Map(),
  order: [],
  architecture: "",
  models: [],
  roster: {},
  runFinished: null,
};

type Listener = () => void;
const listeners: Record<string, Listener[]> = {};

/** Subscribe to a named change channel ("plan", "task", "models", "roster"). */
export function on(channel: string, fn: Listener): void {
  (listeners[channel] ??= []).push(fn);
}

export function emit(channel: string): void {
  for (const fn of listeners[channel] ?? []) fn();
}

export function resetRun(): void {
  state.tasks.clear();
  state.order = [];
  state.architecture = "";
  state.runFinished = null;
  emit("plan");
}
