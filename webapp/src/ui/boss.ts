/* Boss chat panel: the conversational control surface. System notices and
   run telemetry also land here, styled distinctly. */

import { byId, el } from "./dom";

export function youMsg(text: string): void {
  addMsg(text, "you");
}

export function bossMsg(text: string): void {
  addMsg(text, "boss-msg");
}

export function sysMsg(text: string): void {
  addMsg(text, "sys");
}

function addMsg(text: string, cls: string): void {
  const log = byId("boss-log");
  log.appendChild(el("div", `msg ${cls}`, text));
  while (log.childNodes.length > 500) log.removeChild(log.firstChild!);
  log.scrollTop = log.scrollHeight;
}
