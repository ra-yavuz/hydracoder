/* Shared types for the websocket protocol and app state. The protocol is
   defined by lib/hydracoder/journal.py (journal events are forwarded 1:1)
   plus the server-level kinds: models, roster, chat_reply, notice. */

export type TaskState = "queued" | "running" | "review" | "done" | "failed" | "blocked";

export interface TaskSpec {
  id: string;
  title?: string;
  description?: string;
  depends_on?: string[];
  interface?: string;
  acceptance?: string;
  complexity?: string;
}

export interface WorkerEvent {
  kind: "token" | "tool_call" | "tool_result" | "turn_end";
  text?: string;
  name?: string;
  args?: Record<string, unknown>;
  ok?: boolean;
  result?: { error?: string; [k: string]: unknown };
  stopped_reason?: string | null;
}

export interface ModelEntry {
  id: string;
  name?: string;
  size_gb?: number;
  downloaded?: boolean;
  fit?: string;
  fit_reason?: string;
}

export type Role = "small" | "worker" | "reviewer" | "boss";
export const ROLES: Role[] = ["small", "worker", "reviewer", "boss"];

export interface ServerMessage {
  kind: string;
  seq?: number;
  ts?: number;
  data: Record<string, any>;
}

export type Outbound =
  | { type: "goal"; goal: string; models_dir?: string }
  | { type: "chat"; text: string }
  | { type: "control"; action: string; args: Record<string, unknown> };
