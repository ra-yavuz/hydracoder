/* Roster drawer: map a downloaded local model to each role (small / worker /
   reviewer / boss). Selecting a model sends the same set_model_for_role
   control the boss chat uses; the server journals it. */

import { state } from "../state";
import { ROLES } from "../types";
import type { Outbound, Role } from "../types";
import { byId, el } from "./dom";

const ROLE_HINT: Record<Role, string> = {
  small: "fast leaf tasks",
  worker: "the build workhorse",
  reviewer: "advisory second opinion",
  boss: "planning + chat",
};

let sendFn: ((o: Outbound) => boolean) | null = null;

export function initRoster(send: (o: Outbound) => boolean): void {
  sendFn = send;
  byId("roster-btn").addEventListener("click", () => setOpen(true));
  byId("roster-close").addEventListener("click", () => setOpen(false));
  byId("drawer-backdrop").addEventListener("click", () => setOpen(false));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") setOpen(false);
  });
}

function setOpen(open: boolean): void {
  byId("roster-drawer").hidden = !open;
  byId("drawer-backdrop").hidden = !open;
  if (open) renderRoster();
}

export function renderRoster(): void {
  const rolesBox = byId("roster-roles");
  rolesBox.innerHTML = "";
  const usable = state.models.filter((m) => m.downloaded);

  for (const role of ROLES) {
    const row = el("div", "roster-row");
    const label = el("div", "roster-role");
    label.append(el("span", "roster-role-name", role),
                 el("span", "roster-role-hint", ROLE_HINT[role]));

    const select = el("select", "roster-select");
    const current = state.roster[role];
    if (!current) {
      const opt = el("option", "", "(auto)");
      opt.value = "";
      opt.selected = true;
      select.appendChild(opt);
    }
    for (const m of usable) {
      const opt = el("option", "", `${m.id}  ·  ${m.size_gb ?? "?"} GB`);
      opt.value = m.id;
      if (m.id === current) opt.selected = true;
      select.appendChild(opt);
    }
    if (!usable.length) {
      const opt = el("option", "", "no downloaded models");
      opt.disabled = true;
      select.appendChild(opt);
    }
    select.addEventListener("change", () => {
      if (!select.value || !sendFn) return;
      state.roster[role] = select.value;
      sendFn({ type: "control", action: "set_model_for_role",
               args: { role, model_id: select.value } });
    });

    row.append(label, select);
    rolesBox.appendChild(row);
  }

  const modelsBox = byId("roster-models");
  modelsBox.innerHTML = "";
  modelsBox.appendChild(el("div", "roster-models-head",
    `${usable.length} downloaded model(s) on this machine`));
  for (const m of state.models) {
    const line = el("div", `roster-model${m.downloaded ? "" : " is-remote"}`);
    line.append(
      el("span", "rm-dot", m.downloaded ? "●" : "○"),
      el("span", "rm-id", m.id),
      el("span", "rm-meta", `${m.size_gb ?? "?"} GB · fit ${m.fit ?? "?"}`),
    );
    line.title = m.fit_reason ?? "";
    modelsBox.appendChild(line);
  }
}
