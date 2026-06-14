/* Dependency graph strip: tasks laid out by dependency depth, edges as
   circuit-trace curves, nodes pulsing with live state. Clicking a node
   scrolls to (and pings) its window. */

import { state } from "../state";
import type { TaskState } from "../types";
import { byId } from "./dom";
import { scrollToWindow } from "./windows";

const SVGNS = "http://www.w3.org/2000/svg";

const STATE_COLOR: Record<string, string> = {
  queued: "#5a646e",
  running: "#ffb454",
  review: "#4aa3ff",
  done: "#3ad29f",
  failed: "#ff5c6c",
  blocked: "#b06bff",
};

let nodePos: Record<string, { x: number; y: number }> = {};

export function clearGraph(): void {
  byId("graph").innerHTML = "";
  nodePos = {};
}

export function drawGraph(): void {
  clearGraph();
  const svg = byId<HTMLElement>("graph") as unknown as SVGSVGElement;
  const ids = state.order;
  if (!ids.length) return;

  // Layer by dependency depth so the strip reads left to right.
  const depth: Record<string, number> = {};
  const depthOf = (id: string, seen: Set<string>): number => {
    if (depth[id] !== undefined) return depth[id];
    if (seen.has(id)) return 0;
    seen.add(id);
    const deps = state.tasks.get(id)?.spec.depends_on ?? [];
    let m = 0;
    for (const dep of deps) {
      if (state.tasks.has(dep)) m = Math.max(m, depthOf(dep, seen) + 1);
    }
    depth[id] = m;
    return m;
  };
  for (const id of ids) depthOf(id, new Set());

  const cols: Record<number, string[]> = {};
  for (const id of ids) (cols[depth[id]] ??= []).push(id);
  const colW = 120, rowH = 30, padX = 24, padY = 18, r = 7;
  const maxDepth = Math.max(...Object.keys(cols).map(Number));
  const maxRows = Math.max(...Object.values(cols).map((c) => c.length));
  const w = padX * 2 + (maxDepth + 1) * colW;
  const h = Math.max(110, padY * 2 + maxRows * rowH);
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.style.minWidth = `${Math.max(w, 100)}px`;

  for (const [ck, colIds] of Object.entries(cols)) {
    colIds.forEach((id, i) => {
      nodePos[id] = { x: padX + Number(ck) * colW + 30, y: padY + i * rowH + r };
    });
  }

  // Edges first, behind the nodes.
  for (const id of ids) {
    for (const dep of state.tasks.get(id)?.spec.depends_on ?? []) {
      const a = nodePos[dep];
      const b = nodePos[id];
      if (!a) continue;
      const path = document.createElementNS(SVGNS, "path");
      const mx = (a.x + b.x) / 2;
      path.setAttribute("d", `M${a.x},${a.y} C${mx},${a.y} ${mx},${b.y} ${b.x},${b.y}`);
      path.setAttribute("fill", "none");
      path.setAttribute("stroke", "#222a32");
      path.setAttribute("stroke-width", "1");
      svg.appendChild(path);
    }
  }

  for (const id of ids) {
    const p = nodePos[id];
    const g = document.createElementNS(SVGNS, "g");
    g.setAttribute("data-node", id);
    g.setAttribute("class", "graph-node");
    const c = document.createElementNS(SVGNS, "circle");
    c.setAttribute("cx", String(p.x));
    c.setAttribute("cy", String(p.y));
    c.setAttribute("r", String(r));
    c.setAttribute("fill", "#0b0e12");
    c.setAttribute("stroke", STATE_COLOR[state.tasks.get(id)?.state ?? "queued"]);
    c.setAttribute("stroke-width", "2");
    const label = document.createElementNS(SVGNS, "text");
    label.setAttribute("x", String(p.x + 12));
    label.setAttribute("y", String(p.y + 4));
    label.setAttribute("fill", "#7d8893");
    label.setAttribute("font-size", "10");
    label.textContent = id;
    g.append(c, label);
    g.addEventListener("click", () => scrollToWindow(id));
    svg.appendChild(g);
  }
}

export function updateNode(id: string, taskState: TaskState): void {
  const c = byId<HTMLElement>("graph").querySelector(`[data-node="${id}"] circle`);
  if (c) c.setAttribute("stroke", STATE_COLOR[taskState] ?? STATE_COLOR.queued);
}
