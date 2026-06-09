/* hydracoder UI logic: connect to the WebSocket, render the journal as live
   terminal cards + a task graph + a boss chat. Pure vanilla, no deps. */
(function () {
  "use strict";

  var WS_PORT = 8766;
  var wsUrl = "ws://" + location.hostname + ":" + WS_PORT;
  var ws = null;
  var tasks = {};            // id -> {spec, state, el, body}
  var order = [];            // task ids in plan order
  var lastTokenTaskEnded = {};

  var el = {
    conn: document.getElementById("conn"),
    goal: document.getElementById("goal"),
    build: document.getElementById("build"),
    modelsDir: document.getElementById("models-dir"),
    arch: document.getElementById("arch"),
    terminals: document.getElementById("terminals"),
    emptyHint: document.getElementById("empty-hint"),
    graph: document.getElementById("graph"),
    bossLog: document.getElementById("boss-log"),
    chat: document.getElementById("chat"),
    send: document.getElementById("send")
  };

  // ---- websocket ----------------------------------------------------------
  function connect() {
    ws = new WebSocket(wsUrl);
    ws.onopen = function () { setConn("on"); };
    ws.onclose = function () { setConn("off"); setTimeout(connect, 1500); };
    ws.onerror = function () { setConn("off"); };
    ws.onmessage = function (e) {
      var msg; try { msg = JSON.parse(e.data); } catch (_) { return; }
      handle(msg);
    };
  }
  function setConn(s) { el.conn.dataset.state = s; el.conn.textContent = s === "on" ? "linked" : "no link"; }
  function send(obj) { if (ws && ws.readyState === 1) ws.send(JSON.stringify(obj)); }

  // ---- inbound events -----------------------------------------------------
  function handle(m) {
    var d = m.data || {};
    switch (m.kind) {
      case "run_started": resetRun(); break;
      case "plan_created": buildPlan(d); break;
      case "task_state": setTaskState(d.task_id, d.state, d.detail); break;
      case "worker_event": workerEvent(d.task_id, d.event || {}); break;
      case "review": reviewLine(d.task_id, d.passed, d.notes); break;
      case "log": sysMsg(d.msg); break;
      case "decision": sysMsg("decision: " + (d.what || "") + " - " + (d.why || "")); break;
      case "error": sysMsg("error @ " + (d.where || "?") + ": " + (d.message || "")); break;
      case "chat_reply": bossMsg(d.text); break;
      case "run_finished": sysMsg("run finished: " + (d.summary || "") + (d.ok ? " [ok]" : " [incomplete]")); break;
      case "notice": sysMsg(d.msg); break;
    }
  }

  function resetRun() {
    tasks = {}; order = [];
    el.terminals.innerHTML = "";
    el.terminals.appendChild(el.emptyHint);
    el.emptyHint.style.display = "none";
    clearGraph();
  }

  function buildPlan(d) {
    if (d.architecture) el.arch.textContent = d.architecture;
    var list = d.tasks || [];
    el.emptyHint.style.display = "none";
    list.forEach(function (spec) {
      order.push(spec.id);
      tasks[spec.id] = { spec: spec, state: "queued" };
      makeCard(spec);
    });
    drawGraph();
  }

  // ---- terminal cards -----------------------------------------------------
  function makeCard(spec) {
    var card = document.createElement("div");
    card.className = "term"; card.dataset.state = "queued";
    card.dataset.id = spec.id;

    var head = document.createElement("div"); head.className = "term-head";
    var id = document.createElement("span"); id.className = "term-id"; id.textContent = spec.id;
    var title = document.createElement("span"); title.className = "term-title";
    title.textContent = spec.title || spec.id; title.title = spec.description || "";
    var badge = document.createElement("span"); badge.className = "badge";
    badge.dataset.state = "queued"; badge.textContent = "queued";
    head.appendChild(id); head.appendChild(title); head.appendChild(badge);
    card.appendChild(head);

    if (spec.depends_on && spec.depends_on.length) {
      var deps = document.createElement("div"); deps.className = "term-deps";
      deps.innerHTML = "<b>deps:</b> " + spec.depends_on.join(", ");
      card.appendChild(deps);
    }

    var body = document.createElement("div"); body.className = "term-body";
    card.appendChild(body);

    el.terminals.appendChild(card);
    tasks[spec.id].el = card;
    tasks[spec.id].body = body;
    tasks[spec.id].badge = badge;
  }

  function setTaskState(id, state, detail) {
    var t = tasks[id]; if (!t) return;
    t.state = state;
    if (t.badge) { t.badge.dataset.state = state; t.badge.textContent = state; }
    if (t.el) t.el.dataset.state = state;
    if (detail && /attempt 2|attempt 3/.test(detail)) append(t, "\n[retry: " + detail + "]\n", "tok");
    updateNode(id, state);
  }

  function workerEvent(id, ev) {
    var t = tasks[id]; if (!t || !t.body) return;
    if (ev.kind === "token") {
      append(t, ev.text || "", "tok");
    } else if (ev.kind === "tool_call") {
      var a = ev.args ? compactArgs(ev.args) : "";
      append(t, "\n⏳ " + ev.name + (a ? " (" + a + ")" : "") + "\n", "tool-call");
    } else if (ev.kind === "tool_result") {
      var mark = ev.ok ? "✓ " : "✗ ";
      var cls = ev.ok ? "tool-ok" : "tool-bad";
      var extra = (!ev.ok && ev.result && ev.result.error) ? (": " + ev.result.error) : "";
      append(t, mark + ev.name + extra + "\n", cls);
    } else if (ev.kind === "turn_end") {
      append(t, "\n", "tok");
    }
  }

  function compactArgs(args) {
    var parts = [];
    Object.keys(args).forEach(function (k) {
      var v = args[k];
      if (typeof v === "string") v = v.length > 40 ? v.slice(0, 40) + "..." : v;
      else v = JSON.stringify(v);
      parts.push(k + "=" + v);
    });
    return parts.join(" ").slice(0, 120);
  }

  function append(t, text, cls) {
    var span = document.createElement("span");
    span.className = cls; span.textContent = text;
    t.body.appendChild(span);
    t.body.scrollTop = t.body.scrollHeight;
  }

  function reviewLine(id, passed, notes) {
    var t = tasks[id]; if (!t || !t.body) return;
    var div = document.createElement("div");
    div.className = "review-line " + (passed ? "pass" : "fail");
    div.textContent = (passed ? "✓ review passed" : "✗ review rejected") +
      (notes ? ": " + notes : "");
    t.body.appendChild(div);
    t.body.scrollTop = t.body.scrollHeight;
  }

  // ---- svg task graph -----------------------------------------------------
  var SVGNS = "http://www.w3.org/2000/svg";
  var nodePos = {};

  function clearGraph() { el.graph.innerHTML = ""; nodePos = {}; }

  function drawGraph() {
    clearGraph();
    var ids = order.slice();
    if (!ids.length) return;
    // layer by dependency depth so the strip reads left-to-right
    var depth = {};
    function d(id, seen) {
      if (depth[id] != null) return depth[id];
      seen = seen || {};
      if (seen[id]) return 0;
      seen[id] = true;
      var deps = (tasks[id].spec.depends_on) || [];
      var m = 0;
      deps.forEach(function (dep) { if (tasks[dep]) m = Math.max(m, d(dep, seen) + 1); });
      depth[id] = m; return m;
    }
    ids.forEach(function (id) { d(id); });

    var cols = {};
    ids.forEach(function (id) { (cols[depth[id]] = cols[depth[id]] || []).push(id); });
    var colW = 120, rowH = 30, padX = 24, padY = 18, r = 7;
    var maxDepth = Math.max.apply(null, Object.keys(cols).map(Number));
    var maxRows = Math.max.apply(null, Object.keys(cols).map(function (k) { return cols[k].length; }));
    var w = padX * 2 + (maxDepth + 1) * colW;
    var h = Math.max(110, padY * 2 + maxRows * rowH);
    el.graph.setAttribute("viewBox", "0 0 " + w + " " + h);
    el.graph.style.minWidth = Math.max(w, 100) + "px";

    Object.keys(cols).forEach(function (ck) {
      cols[ck].forEach(function (id, i) {
        nodePos[id] = { x: padX + Number(ck) * colW + 30, y: padY + i * rowH + r };
      });
    });

    // edges first (behind nodes)
    ids.forEach(function (id) {
      ((tasks[id].spec.depends_on) || []).forEach(function (dep) {
        if (!nodePos[dep]) return;
        var a = nodePos[dep], b = nodePos[id];
        var path = document.createElementNS(SVGNS, "path");
        var mx = (a.x + b.x) / 2;
        path.setAttribute("d", "M" + a.x + "," + a.y + " C" + mx + "," + a.y + " " + mx + "," + b.y + " " + b.x + "," + b.y);
        path.setAttribute("fill", "none");
        path.setAttribute("stroke", "#222a32");
        path.setAttribute("stroke-width", "1");
        el.graph.appendChild(path);
      });
    });

    // nodes
    ids.forEach(function (id) {
      var p = nodePos[id];
      var g = document.createElementNS(SVGNS, "g");
      g.setAttribute("data-node", id);
      var c = document.createElementNS(SVGNS, "circle");
      c.setAttribute("cx", p.x); c.setAttribute("cy", p.y); c.setAttribute("r", r);
      c.setAttribute("fill", "#0b0e12");
      c.setAttribute("stroke", stateColor(tasks[id].state));
      c.setAttribute("stroke-width", "2");
      var label = document.createElementNS(SVGNS, "text");
      label.setAttribute("x", p.x + 12); label.setAttribute("y", p.y + 4);
      label.setAttribute("fill", "#7d8893"); label.setAttribute("font-size", "10");
      label.setAttribute("font-family", "monospace");
      label.textContent = id;
      g.appendChild(c); g.appendChild(label);
      el.graph.appendChild(g);
    });
  }

  function updateNode(id, state) {
    var g = el.graph.querySelector('[data-node="' + id + '"] circle');
    if (g) g.setAttribute("stroke", stateColor(state));
  }

  function stateColor(s) {
    return {
      queued: "#5a646e", running: "#ffb454", review: "#4aa3ff",
      done: "#3ad29f", failed: "#ff5c6c", blocked: "#b06bff"
    }[s] || "#5a646e";
  }

  // ---- boss chat ----------------------------------------------------------
  function youMsg(t) { addMsg(t, "you"); }
  function bossMsg(t) { addMsg(t, "boss-msg"); }
  function sysMsg(t) { addMsg(t, "sys"); }
  function addMsg(text, cls) {
    var d = document.createElement("div");
    d.className = "msg " + cls; d.textContent = text;
    el.bossLog.appendChild(d);
    el.bossLog.scrollTop = el.bossLog.scrollHeight;
  }

  // ---- wiring -------------------------------------------------------------
  el.build.addEventListener("click", function () {
    var goal = el.goal.value.trim();
    if (!goal) { el.goal.focus(); return; }
    send({ type: "goal", goal: goal, models_dir: el.modelsDir.value.trim() || undefined });
    sysMsg("goal submitted");
  });

  function sendChat() {
    var t = el.chat.value.trim(); if (!t) return;
    youMsg(t); send({ type: "chat", text: t }); el.chat.value = "";
  }
  el.send.addEventListener("click", sendChat);
  el.chat.addEventListener("keydown", function (e) { if (e.key === "Enter") sendChat(); });

  document.querySelectorAll(".ctl").forEach(function (b) {
    b.addEventListener("click", function () {
      send({ type: "control", action: b.dataset.action, args: {} });
      sysMsg("control: " + b.dataset.action);
    });
  });

  connect();
})();
