/* Websocket link to the hydracoder server, with auto-reconnect. The server
   replays the whole journal on connect, so reconnecting late never loses
   history (the UI is a pure function of the event stream). */

import type { Outbound, ServerMessage } from "./types";

type Handler = (msg: ServerMessage) => void;
type ConnHandler = (up: boolean) => void;

// The websocket listens one port above HTTP (server default 8765/8766).
function wsUrl(): string {
  const httpPort = location.port ? Number(location.port) : 8765;
  return `ws://${location.hostname}:${httpPort + 1}`;
}

export class Link {
  private ws: WebSocket | null = null;
  private onMsg: Handler;
  private onConn: ConnHandler;

  constructor(onMsg: Handler, onConn: ConnHandler) {
    this.onMsg = onMsg;
    this.onConn = onConn;
  }

  connect(): void {
    this.ws = new WebSocket(wsUrl());
    this.ws.onopen = () => this.onConn(true);
    this.ws.onclose = () => {
      this.onConn(false);
      setTimeout(() => this.connect(), 1500);
    };
    this.ws.onerror = () => this.onConn(false);
    this.ws.onmessage = (e) => {
      let msg: ServerMessage;
      try {
        msg = JSON.parse(e.data);
      } catch {
        return;
      }
      this.onMsg(msg);
    };
  }

  send(obj: Outbound): boolean {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(obj));
      return true;
    }
    return false;
  }
}
