// SRE Swarm web UI — CLI-style. Minimal chrome, fixed pipeline strip, full-width
// collapsible reasoning rows, real HITL buttons, single omnibox composer.

// Codenames are display-only; pipeline keys stay (triage / rca / ...) so
// the server, audit log, and HITL gates keep working unchanged.
const CODENAMES = {
  observer:     "SENTINEL",
  triage:       "SCOUT.Δ",
  rca:          "CAUSALITY.Σ",
  predict:      "ORACLE.Π",
  rca_confirm:  "GATE-α",
  chaos_replay: "MIRAGE.Χ",
  heal_plan:    "ARCHITECT.Ψ",
  heal:         "GATE-β",
  heal_execute: "EXEC.Ω",
};
function codename(k) { return CODENAMES[k] || k; }

const AGENTS = [
  { key: "triage",       label: CODENAMES.triage },
  { key: "rca",          label: CODENAMES.rca },
  { key: "chaos_replay", label: CODENAMES.chaos_replay },
  { key: "predict",      label: CODENAMES.predict },
  { key: "heal",         label: CODENAMES.heal },
  { key: "heal_execute", label: CODENAMES.heal_execute },
];
const AGENT_KEYS = new Set(AGENTS.map(a => a.key));
// Agents that render a live terminal pane (streaming command + output)
// instead of compact tool chips. chaos_replay touches the sandbox; heal_execute
// touches the real cluster — both benefit from seeing the actual invocations.
const TERMINAL_AGENTS = new Set(["chaos_replay", "heal_execute"]);

// ---- state ----------------------------------------------------------------

let activeIncident = null;          // { id, event, urgency, status }
let pendingApproval = null;         // { incident_id, kind, plan, summary }
let wsConnected = false;

// agent -> { phase, startedAt, summary, detail }
const agentState = new Map();
// agent -> { row, body, summaryEl, phaseEl, elapsedEl, toolsEl, tickHandle, startedAt, tools }
const agentRows = new Map();
// agent -> detail payload from the last phase=done event (used to build the
// end-of-pipeline synthesis card).
const pipelineResults = new Map();
// tool_id -> { agent, name, input, node, startedAt }
const toolCalls = new Map();
// epoch ms when the current incident's pipeline began (first running event).
let pipelineStartMs = null;

// side-panel observability: which service is currently being watched + refresh handle
let sidebarService = null;
let sidebarRefreshHandle = null;

// queued observer events (kept off the chat feed while another investigation is active)
const sidebarInbox = new Map(); // incidentId -> event

// ---- helpers --------------------------------------------------------------

const $ = id => document.getElementById(id);

function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function timestamp() { return new Date().toTimeString().slice(0, 8); }
function fmtDur(secs) {
  if (secs < 1) return `${Math.round(secs * 1000)}ms`;
  if (secs < 60) return `${secs.toFixed(1)}s`;
  const m = Math.floor(secs / 60), s = Math.round(secs - m * 60);
  return `${m}m${s ? ` ${s}s` : ""}`;
}
function formatJson(v) {
  if (v == null) return "";
  if (typeof v === "string") return v;
  try { return JSON.stringify(v, null, 2); } catch { return String(v); }
}
function prettyToolName(name) { return String(name || "").split("__").pop(); }

function clearEmpty() {
  const e = $("feed").querySelector(".empty");
  if (e) e.remove();
}
function scrollFeed() {
  const f = $("feed");
  const near = f.scrollHeight - f.scrollTop - f.clientHeight < 240;
  if (near) f.scrollTop = f.scrollHeight;
}

// ---- header status --------------------------------------------------------

function setStatus() {
  const node = $("status");
  if (!wsConnected) { node.textContent = "//flock offline"; node.dataset.kind = "error"; return; }
  if (activeIncident && activeIncident.status !== "done") {
    node.textContent = `▶ swarm in flight :: ${activeIncident.id}`;
    node.dataset.kind = "running";
  } else {
    node.textContent = "//roosting";
    node.dataset.kind = "idle";
  }
}

// Fleet meter — count of agents currently running / how many total in the strip.
function setFleetMeter() {
  const node = document.getElementById("fleet-meter");
  if (!node) return;
  const total = AGENTS.length;
  let running = 0, done = 0;
  for (const a of AGENTS) {
    const st = agentState.get(a.key);
    if (!st) continue;
    if (st.phase === "running" || st.phase === "waiting") running++;
    else if (st.phase === "done") done++;
  }
  if (running > 0) {
    node.textContent = `fleet · ${running}/${total} in flight`;
    node.dataset.kind = "running";
  } else if (done > 0) {
    node.textContent = `fleet · ${done}/${total} settled`;
    node.dataset.kind = "done";
  } else {
    node.textContent = `fleet · ${total} roosting`;
    node.dataset.kind = "idle";
  }
}

// ---- pipeline strip (exactly one chip per agent, fixed order) -------------

function renderPipeline() {
  const root = $("pipeline");
  if (!root.children.length) {
    root.innerHTML = AGENTS.map(a => `
      <div class="chip" data-agent="${a.key}" data-phase="pending">
        <span class="chip-dot"></span>
        <span class="chip-name">${a.label}</span>
        <span class="chip-phase">pending</span>
      </div>
    `).join("");
  }
  for (const a of AGENTS) {
    const st = agentState.get(a.key) || { phase: "pending" };
    const chip = root.querySelector(`.chip[data-agent="${a.key}"]`);
    if (!chip) continue;
    chip.dataset.phase = st.phase;
    chip.querySelector(".chip-phase").textContent = st.phase;
  }
  setFleetMeter();
}

function resetPipeline() {
  agentState.clear();
  pipelineResults.clear();
  pipelineStartMs = null;
  for (const a of AGENTS) agentState.set(a.key, { phase: "pending" });
  renderPipeline();
}

// ---- collapsible agent rows (full-width, click to toggle) -----------------

function makeAgentRow(agent) {
  clearEmpty();
  const feed = $("feed");
  const row = document.createElement("div");
  row.className = "row agent-row";
  row.dataset.agent = agent;
  row.dataset.phase = "running";
  row.innerHTML = `
    <div class="row-head" role="button" tabindex="0">
      <span class="chev"></span>
      <span class="ts">${timestamp()}</span>
      <span class="row-avatar" data-agent="${agent}">${avatarLetter(agent)}</span>
      <span class="row-agent" title="${agent}">${codename(agent)}</span>
      <span class="row-phase"><span class="phase-dot"></span>running</span>
      <span class="row-summary"></span>
      <span class="row-meta"><span class="row-tools"></span><span class="row-elapsed"></span></span>
    </div>
    <div class="row-body" hidden>
      ${TERMINAL_AGENTS.has(agent)
        ? `<div class="terminal-pane" aria-label="live command output"></div>`
        : `<div class="tool-chips"></div>`}
      <div class="summary-slot"></div>
    </div>
  `;
  const head = row.querySelector(".row-head");
  const body = row.querySelector(".row-body");
  head.addEventListener("click", () => toggleRow(row));
  head.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleRow(row); }
  });
  feed.appendChild(row);
  scrollFeed();
  const entry = {
    row, body,
    head,
    summaryEl: row.querySelector(".row-summary"),
    phaseEl: row.querySelector(".row-phase"),
    elapsedEl: row.querySelector(".row-elapsed"),
    toolsEl: row.querySelector(".row-tools"),
    startedAt: performance.now(),
    tools: 0,
  };
  entry.tickHandle = setInterval(() => {
    if (!agentRows.has(agent)) return;
    const secs = (performance.now() - entry.startedAt) / 1000;
    entry.elapsedEl.textContent = fmtDur(secs);
  }, 250);
  agentRows.set(agent, entry);
  return entry;
}

function toggleRow(row) {
  const body = row.querySelector(".row-body");
  const expanded = !row.classList.contains("open");
  row.classList.toggle("open", expanded);
  body.hidden = !expanded;
}

function closeAgentRow(agent, phase, summary, detail) {
  const e = agentRows.get(agent);
  if (!e) return false;
  if (e.tickHandle) clearInterval(e.tickHandle);
  const secs = (performance.now() - e.startedAt) / 1000;
  e.row.dataset.phase = phase || "done";
  // Replace phase pill content (drop the running dot).
  e.phaseEl.innerHTML = "";
  e.phaseEl.textContent = phase || "done";
  e.elapsedEl.textContent = fmtDur(secs);
  if (summary) e.summaryEl.textContent = summary;
  // Render a digestible summary card in the body; expand row to reveal it.
  if (detail) {
    const slot = e.body.querySelector(".summary-slot");
    if (slot) {
      slot.innerHTML = "";
      const card = renderAgentSummary(agent, detail);
      if (card) slot.appendChild(card);
      slot.appendChild(renderRawDisclosure(detail));
    }
    // auto-expand so user sees the summary
    e.row.classList.add("open");
    e.body.hidden = false;
  }
  // Hide empty terminal pane (e.g. chaos_replay produced JSON directly with
  // no chaos_replay_apply calls) so the summary card dominates instead of
  // the "Waiting for agent to start…" placeholder.
  if (TERMINAL_AGENTS.has(agent)) {
    const pane = e.body.querySelector(".terminal-pane");
    if (pane && pane.childElementCount === 0) pane.remove();
  }
  // Stash detail for the end-of-pipeline synthesis card.
  if (phase === "done" && detail) pipelineResults.set(agent, detail);
  agentRows.delete(agent);
  // Chat-style narration bubble in the feed (the row stays the dense card).
  if (phase === "done" && detail) {
    pushAgentNarration(agent, narrateAgentStep(agent, detail));
  } else if (phase === "done" && summary) {
    mirrorAgentSummary(agent, summary);
  }
  return true;
}

function avatarLetter(agent) {
  const m = {
    triage: "Δ", rca: "Σ", chaos_replay: "Χ", predict: "Π", heal: "β",
    heal_execute: "Ω", rca_confirm: "α", observer: "⊙",
  };
  return m[agent] || (agent || "?")[0].toUpperCase();
}

// ---- digestible summary cards per agent ----------------------------------

function renderAgentSummary(agent, detail) {
  if (!detail || typeof detail !== "object") return null;
  const card = document.createElement("div");
  card.className = "summary-card";
  card.dataset.agent = agent;
  if (agent === "triage")        renderTriageCard(card, detail);
  else if (agent === "rca")      renderRcaCard(card, detail);
  else if (agent === "chaos_replay") renderChaosCard(card, detail);
  else if (agent === "predict")  renderPredictCard(card, detail);
  else if (agent === "heal")     renderHealCard(card, detail);
  else return null;
  return card;
}

function kv(label, value, cls) {
  const row = document.createElement("div");
  row.className = "kv" + (cls ? " " + cls : "");
  row.innerHTML = `<span class="kv-label">${escapeHtml(label)}</span><span class="kv-value">${value}</span>`;
  return row;
}

function bulletList(items) {
  if (!items || !items.length) return "";
  return `<ul class="bullets">${items.map(x => `<li>${escapeHtml(String(x))}</li>`).join("")}</ul>`;
}

function renderTriageCard(card, d) {
  card.appendChild(kv("category", escapeHtml(d.category || "—")));
  const urg = String(d.urgency || "").toLowerCase();
  card.appendChild(kv("urgency", `<span class="pill pill-${escapeHtml(urg)}">${escapeHtml(urg || "—")}</span>`));
  card.appendChild(kv("confidence", `${((d.confidence ?? 0) * 100).toFixed(0)}%`));
  if ((d.affected_services || []).length) {
    card.appendChild(kv("services", (d.affected_services || []).map(s => `<code>${escapeHtml(s)}</code>`).join(" ")));
  }
  if ((d.evidence || []).length) {
    const ev = document.createElement("div");
    ev.className = "kv kv-block";
    ev.innerHTML = `<span class="kv-label">evidence</span><div class="kv-value">${bulletList(d.evidence)}</div>`;
    card.appendChild(ev);
  }
}

function renderRcaCard(card, d) {
  card.appendChild(kv("top cause", `<strong>${escapeHtml(d.top_cause || "—")}</strong>`));
  const top = (d.hypotheses || [])[0];
  if (top) {
    card.appendChild(kv("confidence", `${((top.confidence ?? 0) * 100).toFixed(0)}%`));
    if ((top.evidence || []).length) {
      const ev = document.createElement("div");
      ev.className = "kv kv-block";
      ev.innerHTML = `<span class="kv-label">evidence</span><div class="kv-value">${bulletList(top.evidence)}</div>`;
      card.appendChild(ev);
    }
  }
  if ((d.hypotheses || []).length > 1) {
    const others = (d.hypotheses || []).slice(1, 4).map(h => `${escapeHtml(h.cause || "")} (${((h.confidence ?? 0) * 100).toFixed(0)}%)`);
    card.appendChild(kv("alt hypotheses", others.join(" · ")));
  }
  if ((d.affected_pods || []).length) {
    card.appendChild(kv("pods", (d.affected_pods || []).map(p => `<code>${escapeHtml(p)}</code>`).join(" ")));
  }
}

function renderChaosCard(card, d) {
  card.appendChild(kv("scenario", `<code>${escapeHtml(d.scenario_applied || "—")}</code>`));
  card.appendChild(kv("validation", escapeHtml(d.validation || "—")));
  card.appendChild(kv("similarity", `${((d.similarity_score ?? 0) * 100).toFixed(0)}%`));
  if (d.sandbox_namespace) card.appendChild(kv("sandbox", `<code>${escapeHtml(d.sandbox_namespace)}</code>`));
  if (d.duration_s) card.appendChild(kv("duration", `${d.duration_s}s`));
  if (d.rejection_reason) card.appendChild(kv("rejection", escapeHtml(d.rejection_reason)));
}

function renderPredictCard(card, d) {
  const heal = !!d.will_self_heal;
  card.appendChild(kv("self-heal", `<span class="pill ${heal ? "pill-ok" : "pill-warn"}">${heal ? "yes" : "no"}</span>`));
  if (d.self_heal_reason) card.appendChild(kv("reason", escapeHtml(d.self_heal_reason)));
  card.appendChild(kv("cascade risk", `<span class="pill pill-${escapeHtml(String(d.cascade_risk || "").toLowerCase())}">${escapeHtml(d.cascade_risk || "—")}</span>`));
  card.appendChild(kv("risk level", `<span class="pill pill-${escapeHtml(String(d.risk_level || "").toLowerCase())}">${escapeHtml(d.risk_level || "—")}</span>`));
  if (d.mttr_without_min != null) card.appendChild(kv("mttr (no action)", `${d.mttr_without_min} min`));
}

function renderHealCard(card, d) {
  // detail shape from orchestrator: { plan, approved, approved_steps, rejected, rejection_reason }
  const plan = d.plan || d;
  const approved = d.approved;
  if (approved !== undefined) {
    card.appendChild(kv("decision", `<span class="pill ${approved ? "pill-ok" : "pill-warn"}">${approved ? "approved" : "rejected"}</span>`));
  }
  if (d.rejection_reason) card.appendChild(kv("reason", escapeHtml(d.rejection_reason)));
  const steps = plan && plan.steps;
  if (Array.isArray(steps) && steps.length) {
    card.appendChild(kv("recovery", `~${plan.estimated_recovery_min ?? "?"} min · conf ${((plan.confidence ?? 0) * 100).toFixed(0)}%`));
    const table = document.createElement("div");
    table.className = "kv kv-block";
    const rows = steps.map(s => `
      <div class="heal-step">
        <span class="heal-num">${s.step_number}</span>
        <span class="pill pill-blast-${escapeHtml(s.blast_radius || "read_only")}">${escapeHtml(s.blast_radius || "read_only")}</span>
        <span class="heal-action">${escapeHtml(s.action || "")}</span>
        ${s.kubectl_command ? `<code class="heal-cmd">${escapeHtml(s.kubectl_command)}</code>` : ""}
      </div>
    `).join("");
    table.innerHTML = `<span class="kv-label">plan</span><div class="kv-value heal-steps">${rows}</div>`;
    card.appendChild(table);
  }
}

function renderRawDisclosure(detail) {
  const wrap = document.createElement("details");
  wrap.className = "raw-json";
  wrap.innerHTML = `<summary>raw json</summary><pre class="row-json"></pre>`;
  wrap.querySelector("pre").textContent = formatJson(detail);
  return wrap;
}

function rowTarget(agent) {
  const e = agentRows.get(agent);
  return e ? e.body : null;
}

function bumpTools(agent) {
  const e = agentRows.get(agent);
  if (!e) return;
  e.tools += 1;
  e.toolsEl.textContent = `${e.tools} tool${e.tools === 1 ? "" : "s"}`;
}

// ---- system rows ----------------------------------------------------------

function pushSystem(text, kind) {
  clearEmpty();
  const row = document.createElement("div");
  row.className = `row sys ${kind || ""}`;
  row.innerHTML = `<span class="ts">${timestamp()}</span><span class="sys-body">${escapeHtml(text)}</span>`;
  $("feed").appendChild(row);
  scrollFeed();
  mirrorConversation("system", text, kind);
}

function pushUserMessage(text) {
  clearEmpty();
  const row = document.createElement("div");
  row.className = "row user";
  row.innerHTML = `<span class="ts">${timestamp()}</span><span class="user-tag">you</span><span class="user-body">${escapeHtml(text)}</span>`;
  $("feed").appendChild(row);
  scrollFeed();
  mirrorConversation("user", text);
}

// ---- right-column conversation mirror -------------------------------------

function mirrorConversation(role, text, kind) {
  const list = $("conv-list");
  if (!list) return;
  const empty = list.querySelector(".empty");
  if (empty) empty.remove();
  const bubble = document.createElement("div");
  bubble.className = `conv-bubble conv-${role}`;
  if (kind) bubble.dataset.kind = kind;
  bubble.innerHTML = `
    <div class="conv-meta">
      <span class="conv-role">${role === "user" ? "you" : role === "agent" ? (text._agent || "agent") : "system"}</span>
      <span class="conv-ts">${timestamp()}</span>
    </div>
    <div class="conv-text">${escapeHtml(typeof text === "string" ? text : text.text)}</div>`;
  list.appendChild(bubble);
  list.scrollTop = list.scrollHeight;
  const upd = $("conv-updated");
  if (upd) upd.textContent = "Δ " + new Date().toTimeString().slice(0, 8);
}

function mirrorAgentSummary(agent, summary) {
  const text = `${agent} · ${summary}`;
  mirrorConversation("agent", text);
}

function resetConversationMirror() {
  const list = $("conv-list");
  if (!list) return;
  list.innerHTML = `<div class="empty side-empty">// console clear<br>stdin&gt; service · symptom · INC-ID</div>`;
  const upd = $("conv-updated");
  if (upd) upd.textContent = "";
}

// ---- agent message / tool call (inside a row body) ------------------------

// Reasoning prose + raw "final json" blocks are intentionally suppressed.
// The summary card (rendered on phase=done) is the headline content; the
// ▸ raw json disclosure below it holds the full structured payload for the
// curious. Dumping the LLM chain-of-thought into the feed was clumsy.
// Exception: TERMINAL_AGENTS (chaos_replay, heal_execute) get progress
// header lines streamed into their terminal pane.
function pushAgentMessage(agent, text) {
  if (!TERMINAL_AGENTS.has(agent)) return;
  // Only render orchestrator-emitted step headers (convention: starts with "# ").
  // LLM freeform commentary between tool calls would otherwise pollute the
  // terminal pane with apology prose like "It seems like the context...".
  const s = String(text || "").trim();
  if (!s.startsWith("# ")) return;
  const e = agentRows.get(agent);
  if (!e) return;
  openRowIfClosed(e);
  appendTerminalLine(e, "header", s);
  scrollFeed();
}

function ensureRowOpen(e) {
  if (!e.row.classList.contains("open")) {
    e.row.classList.add("open");
    e.body.hidden = false;
  }
}
const openRowIfClosed = ensureRowOpen;

function appendTerminalLine(entry, kind, text) {
  const pane = entry.body.querySelector(".terminal-pane");
  if (!pane) return null;
  const line = document.createElement("div");
  line.className = `term-line term-${kind}`;
  line.textContent = text;
  pane.appendChild(line);
  pane.scrollTop = pane.scrollHeight;
  return line;
}

function pushToolCall(agent, toolId, toolName, input) {
  const e = agentRows.get(agent);
  if (!e) return;
  ensureRowOpen(e);
  if (TERMINAL_AGENTS.has(agent)) {
    const args = _shortArgs(input);
    const cmdLine = appendTerminalLine(e, "cmd", `$ ${prettyToolName(toolName)}${args ? " " + args : ""}`);
    const outLine = appendTerminalLine(e, "out pending", "  \u2026");
    toolCalls.set(toolId, { agent, name: toolName, cmdLine, outLine, startedAt: performance.now() });
    bumpTools(agent);
    return;
  }
  const slot = e.body.querySelector(".tool-chips");
  if (!slot) return;
  const chip = document.createElement("span");
  chip.className = "tool-chip running";
  chip.dataset.tool = toolId;
  chip.innerHTML = `
    <span class="tool-chip-dot"></span>
    <span class="tool-chip-name">${escapeHtml(prettyToolName(toolName))}</span>
    <span class="tool-chip-status">…</span>
  `;
  slot.appendChild(chip);
  toolCalls.set(toolId, { agent, name: toolName, node: chip, startedAt: performance.now() });
  bumpTools(agent);
  scrollFeed();
}

function pushToolResult(agent, toolId, content, isError) {
  const entry = toolCalls.get(toolId);
  if (!entry) return;
  const elapsed = (performance.now() - entry.startedAt) / 1000;
  if (entry.outLine) {
    // terminal agent
    entry.outLine.classList.remove("pending");
    entry.outLine.classList.toggle("err", !!isError);
    const text = _truncateOutput(formatJson(content));
    entry.outLine.textContent = `  ${text}  (${fmtDur(elapsed)})`;
    const pane = entry.outLine.parentElement;
    if (pane) pane.scrollTop = pane.scrollHeight;
    scrollFeed();
    return;
  }
  // chip-style agent
  if (!entry.node) return;
  entry.node.classList.remove("running");
  entry.node.classList.add(isError ? "error" : "done");
  const status = entry.node.querySelector(".tool-chip-status");
  if (status) status.textContent = fmtDur(elapsed);
  scrollFeed();
}

function _shortArgs(input) {
  if (input == null) return "";
  if (typeof input === "string") return input.length > 80 ? input.slice(0, 80) + "\u2026" : input;
  try {
    const pairs = Object.entries(input).slice(0, 4).map(([k, v]) => {
      const vs = typeof v === "string" ? v : JSON.stringify(v);
      const trimmed = vs && vs.length > 40 ? vs.slice(0, 40) + "\u2026" : vs;
      return `${k}=${trimmed}`;
    });
    return pairs.join(" ");
  } catch { return ""; }
}

function _truncateOutput(text) {
  if (!text) return "(no output)";
  const s = String(text).replace(/\s+/g, " ").trim();
  return s.length > 200 ? s.slice(0, 200) + "\u2026" : s;
}

// ---- HITL request (real buttons) -----------------------------------------

function pushHitlRequest(req) {
  clearEmpty();
  const kind = req.kind || "heal";
  const incidentId = req.incident_id;
  const plan = req.plan;
  let summary = req.summary || "";

  let bodyHtml = "";
  if (kind === "heal" && plan && Array.isArray(plan.steps)) {
    summary = summary || `${plan.steps.length} steps · ~${plan.estimated_recovery_min ?? "?"}min`;
    bodyHtml = renderHealHitlBody(plan);
  } else if (kind === "chaos_replay") {
    summary = summary || "chaos replay in sandbox";
    bodyHtml = renderChaosHitlBody(req);
  } else if (kind === "rca_confirm") {
    summary = summary || "Confirm root cause to proceed";
    bodyHtml = `
      <div class="hitl-summary">${escapeHtml(summary)}</div>
      <div class="hitl-meta">
        <span class="hitl-meta-item">authorizes <strong>sandbox replay</strong></span>
        <span class="hitl-meta-item">authorizes <strong>live heal execution</strong></span>
      </div>
    `;
  } else {
    bodyHtml = `<div class="hitl-summary">${escapeHtml(summary || "approval required")}</div>`;
  }

  const row = document.createElement("div");
  row.className = "row hitl open";
  row.dataset.kind = kind;
  row.dataset.incident = incidentId || "";
  row.dataset.phase = "waiting";
  row.innerHTML = `
    <div class="row-head" role="button" tabindex="0">
      <span class="chev"></span>
      <span class="ts">${timestamp()}</span>
      <span class="row-avatar" data-agent="${kind}">${avatarLetter(kind)}</span>
      <span class="row-agent" title="${kind}">${codename(kind)}</span>
      <span class="row-incident">${escapeHtml(incidentId || "")}</span>
      <span class="row-phase"><span class="phase-dot"></span>awaiting approval</span>
      <span class="row-summary">${escapeHtml(summary)}</span>
      <span class="row-meta"></span>
    </div>
    <div class="row-body">
      ${bodyHtml}
      <div class="hitl-guidance">
        <textarea class="hitl-guidance-input" rows="1"
          placeholder="Add guidance for the agent (e.g. 'check downstream payments first')&hellip; Enter to send, Shift+Enter for newline"></textarea>
        <button class="hitl-guidance-send">Send guidance</button>
        <span class="hitl-guidance-status" hidden></span>
      </div>
      <div class="hitl-reject-reason" hidden>
        <input type="text" class="hitl-reason-input" placeholder="reason for rejection (optional)&hellip;" />
      </div>
      <div class="hitl-actions">
        ${kind === "heal" && plan && (plan.steps || []).length
          ? `<button class="primary hitl-approve-selected">Approve selected <span class="hitl-count">(${plan.steps.length})</span></button>
             <button class="hitl-approve-all">Approve all</button>`
          : `<button class="primary hitl-approve-all">Approve</button>`}
        <button class="hitl-reject">Reject</button>
        <span class="hitl-resolution" hidden></span>
      </div>
    </div>
  `;
  const head = row.querySelector(".row-head");
  head.addEventListener("click", (ev) => {
    if (ev.target.closest("button") || ev.target.closest("input")) return;
    toggleRow(row);
  });
  head.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleRow(row); }
  });

  // Per-step checkboxes (heal only) — update the live count.
  const boxes = row.querySelectorAll(".hitl-step-check");
  const countEl = row.querySelector(".hitl-count");
  const refreshCount = () => {
    const checked = Array.from(boxes).filter(b => b.checked).length;
    if (countEl) countEl.textContent = `(${checked})`;
    const sel = row.querySelector(".hitl-approve-selected");
    if (sel) sel.disabled = checked === 0;
  };
  boxes.forEach(b => b.addEventListener("change", refreshCount));

  const approveAllBtn  = row.querySelector(".hitl-approve-all");
  const approveSelBtn  = row.querySelector(".hitl-approve-selected");
  const rejectBtn      = row.querySelector(".hitl-reject");
  const reasonWrap     = row.querySelector(".hitl-reject-reason");
  const reasonInput    = row.querySelector(".hitl-reason-input");

  if (approveAllBtn) approveAllBtn.addEventListener("click", () => approve(row, kind, incidentId, plan, null));
  if (approveSelBtn) approveSelBtn.addEventListener("click", () => {
    const chosen = Array.from(boxes).filter(b => b.checked).map(b => Number(b.dataset.step));
    if (!chosen.length) return;
    approve(row, kind, incidentId, plan, chosen);
  });
  if (rejectBtn) rejectBtn.addEventListener("click", () => {
    // Two-stage: first click reveals the reason input + Confirm button.
    if (reasonWrap.hidden) {
      reasonWrap.hidden = false;
      rejectBtn.textContent = "Confirm reject";
      reasonInput.focus();
      return;
    }
    reject(row, kind, incidentId, (reasonInput.value || "").trim());
  });

  const guideInput  = row.querySelector(".hitl-guidance-input");
  const guideBtn    = row.querySelector(".hitl-guidance-send");
  const guideStatus = row.querySelector(".hitl-guidance-status");
  const sendGuidance = async () => {
    const text = (guideInput.value || "").trim();
    if (!text || !incidentId) return;
    guideBtn.disabled = true;
    guideStatus.hidden = false;
    guideStatus.textContent = "// dispatching…";
    try {
      const r = await fetch(`/api/incident/${incidentId}/inject`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      if (!r.ok) throw new Error(`status ${r.status}`);
      guideInput.value = "";
      guideStatus.textContent = "▶ guidance injected · applied next step";
    } catch (e) {
      guideStatus.textContent = `failed: ${e.message || e}`;
    } finally {
      guideBtn.disabled = false;
    }
  };
  if (guideBtn) guideBtn.addEventListener("click", sendGuidance);
  if (guideInput) guideInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendGuidance(); }
  });

  $("feed").appendChild(row);
  scrollFeed();
}

function renderHealHitlBody(plan) {
  const stepsHtml = (plan.steps || []).map(s => `
    <label class="hitl-step">
      <input type="checkbox" class="hitl-step-check" data-step="${s.step_number}" checked />
      <span class="hitl-step-num">#${s.step_number}</span>
      <span class="pill pill-blast-${escapeHtml(s.blast_radius || "read_only")}">${escapeHtml(s.blast_radius || "read_only")}</span>
      <span class="hitl-step-action">${escapeHtml(s.action || "")}</span>
      ${s.kubectl_command ? `<code class="hitl-step-cmd">${escapeHtml(s.kubectl_command)}</code>` : ""}
      ${s.expected_effect ? `<span class="hitl-step-meta">expected: ${escapeHtml(s.expected_effect)}</span>` : ""}
      ${s.rollback_cmd ? `<span class="hitl-step-meta">rollback: <code>${escapeHtml(s.rollback_cmd)}</code></span>` : ""}
    </label>
  `).join("");
  const writes = (plan.steps || []).filter(s => (s.blast_radius || "") !== "read_only").length;
  const reads  = (plan.steps || []).length - writes;
  return `
    <div class="hitl-meta">
      <span class="hitl-meta-item"><strong>${(plan.steps || []).length}</strong> steps</span>
      <span class="hitl-meta-item">${writes} write · ${reads} read</span>
      <span class="hitl-meta-item">recovery ~${plan.estimated_recovery_min ?? "?"} min</span>
      <span class="hitl-meta-item">confidence ${((plan.confidence ?? 0) * 100).toFixed(0)}%</span>
    </div>
    <div class="hitl-steps-list">${stepsHtml}</div>
  `;
}

function renderChaosHitlBody(req) {
  return `
    <div class="hitl-summary">${escapeHtml(req.summary || "Approve replay in sandbox namespace?")}</div>
    <div class="hitl-meta">
      <span class="hitl-meta-item">sandbox: <code>sre-sandbox</code></span>
      <span class="hitl-meta-item">isolation: chaos mesh CRD, auto-cleanup</span>
    </div>
  `;
}

async function approve(row, kind, incidentId, plan, stepFilter) {
  let steps = [];
  if (kind === "heal" && plan && Array.isArray(plan.steps)) {
    steps = stepFilter == null
      ? plan.steps.map(s => s.step_number)
      : stepFilter;
  }
  await fetch(`/api/approval/${incidentId}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decision: "approve_all", approved_steps: steps, kind }),
  });
  const label = (stepFilter && plan && stepFilter.length < (plan.steps || []).length)
    ? `approved (${stepFilter.length}/${plan.steps.length})`
    : "approved";
  resolveHitl(row, label, "done");
}

async function reject(row, kind, incidentId, reason) {
  await fetch(`/api/approval/${incidentId}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decision: "reject", rejection_reason: reason || "rejected via UI", kind }),
  });
  resolveHitl(row, "rejected", "error");
}

function resolveHitl(row, label, phase) {
  row.classList.add("resolved");
  row.dataset.phase = phase || (label === "rejected" ? "error" : "done");
  const phaseEl = row.querySelector(".row-phase");
  phaseEl.innerHTML = "";
  phaseEl.textContent = label;
  const actions = row.querySelector(".hitl-actions");
  actions.querySelectorAll("button").forEach(b => { b.disabled = true; });
  row.querySelectorAll(".hitl-step-check").forEach(b => { b.disabled = true; });
  const reasonWrap = row.querySelector(".hitl-reject-reason");
  if (reasonWrap) reasonWrap.hidden = true;
  const res = actions.querySelector(".hitl-resolution");
  res.textContent = label;
  res.hidden = false;
  if (pendingApproval && pendingApproval.incident_id === row.dataset.incident && pendingApproval.kind === row.dataset.kind) {
    pendingApproval = null;
  }
}

// ---- pending incident inbox (manual gate) ---------------------------------

function pushPendingIncident(event) {
  clearEmpty();
  const feed = $("feed");
  // Avoid duplicate cards for the same incident id.
  if (feed.querySelector(`.row.inbox[data-incident="${event.id}"]`)) return;
  const row = document.createElement("div");
  row.className = "row inbox";
  row.dataset.incident = event.id;
  const val = event.value != null ? Number(event.value).toFixed(2) : "—";
  const thr = event.threshold != null ? Number(event.threshold).toFixed(2) : "—";
  row.innerHTML = `
    <div class="row-head">
      <span class="ts">${timestamp()}</span>
      <span class="row-agent" title="observer">${codename("observer")}</span>
      <span class="row-incident">${escapeHtml(event.id)}</span>
      <span class="row-phase" data-kind="incident">detected</span>
      <span class="row-summary">${escapeHtml(event.trigger || "anomaly")} on ${escapeHtml(event.service || "unknown")} · ${val} (thr ${thr})</span>
      <span class="row-meta">
        <button class="primary inbox-investigate">Investigate</button>
        <button class="inbox-dismiss">Dismiss</button>
      </span>
    </div>
  `;
  row.querySelector(".inbox-investigate").addEventListener("click", () => investigateIncident(row, event));
  row.querySelector(".inbox-dismiss").addEventListener("click", () => dismissIncident(row, event));
  feed.appendChild(row);
  scrollFeed();
  renderSidebarInbox();
}

async function investigateIncident(row, event) {
  row.querySelectorAll("button").forEach(b => { b.disabled = true; });
  row.classList.add("handled");
  renderSidebarInbox();
  // Don't stomp an active pipeline — but if none is running, this becomes
  // the active incident and we clear the feed.
  if (!activeIncident || activeIncident.status === "done") {
    activeIncident = { id: event.id, event, urgency: null, status: "investigating" };
    resetFeed();
    resetPipeline();
    pushSystem(`investigating ${event.trigger || "anomaly"} on ${event.service || "unknown"} · ${event.id}`, "incident");
    setStatus();
    if (event.service) startSidebarObservability(event.service);
  }
  try {
    const r = await fetch(`/api/incidents/${event.id}/investigate`, { method: "POST" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
  } catch (err) {
    pushSystem(`failed to start pipeline · ${err}`, "error");
    row.querySelectorAll("button").forEach(b => { b.disabled = false; });
    row.classList.remove("handled");
    renderSidebarInbox();
  }
}

async function dismissIncident(row, event) {
  row.querySelectorAll("button").forEach(b => { b.disabled = true; });
  try {
    await fetch(`/api/incidents/${event.id}/dismiss`, { method: "POST" });
    row.classList.add("dismissed");
    row.querySelector(".row-phase").textContent = "killed";
  } catch (err) {
    row.querySelectorAll("button").forEach(b => { b.disabled = false; });
  } finally {
    renderSidebarInbox();
  }
}

// ---- summary text from a phase detail -------------------------------------

function summaryFromDetail(agent, detail) {
  if (!detail) return "";
  try {
    if (agent === "triage")
      return `${detail.category} · ${detail.urgency} · conf ${(detail.confidence ?? 0).toFixed(2)}`;
    if (agent === "rca")
      return `${(detail.top_cause || "").slice(0, 120)}`;
    if (agent === "chaos_replay")
      return `${detail.scenario_applied || "no-scenario"} · ${detail.validation} · sim ${(detail.similarity_score ?? 0).toFixed(2)}`;
    if (agent === "predict")
      return `mttr ${detail.mttr_without_min}m · ${detail.risk_level}`;
    if (agent === "heal") {
      // detail may be the bare plan ({steps,...}) or the post-HITL shape
      // ({plan:{steps,...}, approved, approved_steps, ...}). Handle both.
      const plan = detail.plan || detail;
      const steps = plan.steps || [];
      const conf = plan.confidence ?? detail.confidence ?? 0;
      const decision = detail.approved === true ? "approved"
                      : detail.approved === false ? "rejected" : "";
      const base = `${steps.length} step${steps.length === 1 ? "" : "s"} · conf ${conf.toFixed(2)}`;
      return decision ? `${decision} · ${base}` : base;
    }
  } catch { /* noop */ }
  return "";
}

// One-to-two-line natural-language synthesis pushed into the feed as a chat
// bubble right after each agent step closes. Distinct from the dense KV card
// (which stays inside the row body) and from the raw json disclosure.
function narrateAgentStep(agent, detail) {
  if (!detail) return "";
  try {
    if (agent === "triage") {
      const svcs = (detail.affected_services || []).join(", ") || "unknown service";
      const cat = detail.category || "incident";
      const urg = detail.urgency || "medium";
      const conf = ((detail.confidence ?? 0) * 100).toFixed(0);
      return `Triaged as <b>${escapeHtml(cat)}</b> on <code>${escapeHtml(svcs)}</code> — urgency <b>${escapeHtml(urg)}</b> (${conf}% confidence).`;
    }
    if (agent === "rca") {
      const cause = detail.top_cause || "unknown cause";
      const top = (detail.hypotheses || [])[0] || {};
      const conf = ((top.confidence ?? 0) * 100).toFixed(0);
      const ev = (top.evidence || [])[0];
      const tail = ev ? ` Key signal: <i>${escapeHtml(ev.slice(0, 110))}</i>.` : "";
      return `Top cause: <b>${escapeHtml(cause)}</b> (${conf}% confidence).${tail}`;
    }
    if (agent === "predict") {
      const heal = detail.will_self_heal ? "yes" : "no";
      const cascade = detail.cascade_risk || "unknown";
      const risk = detail.risk_level || "unknown";
      const mttr = detail.mttr_without_min;
      const mttrStr = mttr != null ? `~${mttr} min` : "unknown";
      return `Forecast: self-heal <b>${heal}</b>, cascade risk <b>${escapeHtml(cascade)}</b>, MTTR if untouched <b>${mttrStr}</b> — overall risk <b>${escapeHtml(risk)}</b>.`;
    }
    if (agent === "chaos_replay") {
      const scenario = detail.scenario_applied || "no scenario";
      const validation = detail.validation || "unknown";
      const sim = ((detail.similarity_score ?? 0) * 100).toFixed(0);
      const verb = validation === "confirmed" ? "Confirmed" :
                   validation === "rejected" ? "Rejected" : "Partial match for";
      return `${verb} the hypothesis by replaying <code>${escapeHtml(scenario)}</code> in the sandbox — similarity <b>${sim}%</b>.`;
    }
    if (agent === "heal") {
      const plan = detail.plan || detail;
      const steps = plan.steps || [];
      const conf = ((plan.confidence ?? 0) * 100).toFixed(0);
      const eta = plan.estimated_recovery_min;
      const writeCt = steps.filter(s => (s.blast_radius || "").toLowerCase() !== "read_only").length;
      const decision = detail.approved === true ? "approved" :
                       detail.approved === false ? "rejected" : "drafted";
      const etaStr = eta != null ? ` Estimated recovery <b>~${eta} min</b>.` : "";
      return `Plan ${decision}: <b>${steps.length} step${steps.length === 1 ? "" : "s"}</b> (${writeCt} write) at ${conf}% confidence.${etaStr}`;
    }
    if (agent === "heal_execute") {
      // orchestrator emits { executed: [...], count, summary }
      const exec = detail.executed || detail.executed_steps || detail.results || [];
      const okCt = exec.filter(s => s.executed === true && !s.error).length;
      const total = exec.length;
      if (detail.summary && total > 0) {
        return `Executed <b>${okCt}/${total}</b> step${total === 1 ? "" : "s"}: ${escapeHtml(detail.summary)}`;
      }
      return `Executed <b>${okCt}/${total}</b> remediation step${total === 1 ? "" : "s"} against the live cluster.`;
    }
  } catch { /* noop */ }
  return "";
}

function pushAgentNarration(agent, html) {
  if (!html) return;
  clearEmpty();
  const row = document.createElement("div");
  row.className = "row narration";
  row.dataset.agent = agent;
  row.innerHTML = `
    <span class="ts">${timestamp()}</span>
    <span class="narr-avatar" data-agent="${agent}">${avatarLetter(agent)}</span>
    <span class="narr-body">${html}</span>
  `;
  $("feed").appendChild(row);
  scrollFeed();
  mirrorConversation("agent", { _agent: agent, text: stripTags(html) });
}

function stripTags(s) { return String(s).replace(/<[^>]*>/g, ""); }

// ---- websocket handler (preserves existing event types) ------------------

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => { wsConnected = true; setStatus(); };
  ws.onclose = () => { wsConnected = false; setStatus(); setTimeout(connect, 2000); };
  ws.onmessage = e => { try { handle(JSON.parse(e.data)); } catch (err) { console.error(err); } };
}

// Drop events that belong to an incident other than the one the operator is
// currently watching. Concurrent / stale pipelines on the same bus would
// otherwise overwrite the pipeline strip and dump unrelated rows into the feed.
function isForeignIncident(msg) {
  if (!activeIncident) return false;            // no focus yet — accept everything
  if (!msg.incident_id) return false;           // legacy / global event — accept
  return msg.incident_id !== activeIncident.id;
}

function handle(msg) {
  if (!msg || !msg.type) return;
  if (msg.type === "snapshot") return;

  if (msg.type === "observer_incident") {
    // While an investigation is running, queue new detections to the sidebar
    // inbox so they don't disrupt the active chat. Otherwise drop them inline
    // in the chat where the operator can act on them immediately.
    const busy = activeIncident && activeIncident.status !== "done";
    if (busy) {
      enqueueSidebarInbox(msg.event);
    } else {
      pushPendingIncident(msg.event);
    }
    return;
  }

  if (msg.type === "agent_phase") {
    if (isForeignIncident(msg)) return;
    const agent = msg.agent;
    if (!AGENT_KEYS.has(agent)) return;
    const summary = summaryFromDetail(agent, msg.detail);
    agentState.set(agent, { phase: msg.phase, summary });
    renderPipeline();
    if (msg.phase === "running") {
      // close any stale row first
      if (agentRows.has(agent)) closeAgentRow(agent, "interrupted");
      makeAgentRow(agent);
    } else if (msg.phase === "waiting") {
      // "waiting" means an HITL gate is opening — the gate's own card will
      // appear via hitl_request, so skip the duplicate system note here.
      // Leave the pipeline strip showing the waiting state.
    } else {
      if (!closeAgentRow(agent, msg.phase, summary, msg.detail)) {
        // No live row — emit a one-line system note so the user still sees it.
        pushSystem(`${agent} ${msg.phase}${summary ? " · " + summary : ""}`);
      }
    }
    if (agent === "triage" && msg.detail && msg.detail.urgency && activeIncident) {
      activeIncident.urgency = msg.detail.urgency;
    }
    if (agent === "triage" && msg.phase === "done" && msg.detail) {
      const affected = (msg.detail.affected_services || [])[0];
      if (affected && affected !== sidebarService) startSidebarObservability(affected);
    }
    return;
  }

  if (msg.type === "agent_message") {
    if (isForeignIncident(msg)) return;
    return pushAgentMessage(msg.agent, msg.text);
  }
  if (msg.type === "agent_token")   return;
  if (msg.type === "tool_call") {
    if (isForeignIncident(msg)) return;
    recordToolCall(msg.tool_id);
    return pushToolCall(msg.agent, msg.tool_id, msg.tool_name, msg.input);
  }
  if (msg.type === "tool_result") {
    if (isForeignIncident(msg)) return;
    recordToolResult(msg.tool_id, !!msg.is_error);
    return pushToolResult(msg.agent, msg.tool_id, msg.content, msg.is_error);
  }

  if (msg.type === "hitl_request") {
    if (isForeignIncident(msg)) return;
    pendingApproval = { incident_id: msg.incident_id, kind: msg.kind || "heal", plan: msg.plan, summary: msg.summary };
    pushHitlRequest(msg);
    return;
  }

  if (msg.type === "user_message") {
    if (isForeignIncident(msg)) return;
    return pushUserMessage(msg.text);
  }
  if (msg.type === "plan_ready")   return; // delivered via hitl_request

  if (msg.type === "pipeline_done") {
    pushSystem(`pipeline complete · ${msg.incident_id}`, "done");
    if (activeIncident && activeIncident.id === msg.incident_id) {
      activeIncident.status = "done";
      setStatus();
      stopSidebarObservability();
    }
    return;
  }

  if (msg.type === "vulnerability_report") {
    renderVulnerabilities(msg.findings || [], msg.generated_at);
    return;
  }
}

// ---- side panel: vulnerabilities -----------------------------------------

function renderVulnerabilities(findings, generatedAt) {
  const list = $("vulns-list");
  const badge = $("vulns-count");
  const sub = $("vulns-updated");
  if (!list) return;
  list.innerHTML = "";
  if (!findings.length) {
    list.innerHTML = `<div class="empty side-empty">// all clear :: no active anomalies</div>`;
    badge.hidden = true;
    badge.textContent = "0";
  } else {
    // Stable sort: critical > high > medium > low, then by service name.
    const order = { critical: 0, high: 1, medium: 2, low: 3 };
    const sorted = [...findings].sort((a, b) =>
      (order[a.severity] ?? 9) - (order[b.severity] ?? 9)
      || String(a.service).localeCompare(String(b.service))
    );
    for (const f of sorted) {
      list.appendChild(renderVulnCard(f));
    }
    badge.hidden = false;
    badge.textContent = String(findings.length);
  }
  if (generatedAt) {
    // ISO → HH:MM:SS local.
    try { sub.textContent = "Δ " + new Date(generatedAt).toTimeString().slice(0, 8); }
    catch { sub.textContent = ""; }
  }
}

function renderVulnCard(f) {
  const node = document.createElement("div");
  node.className = "vuln";
  const sev = (f.severity || "low").toLowerCase();
  const valStr = f.value == null ? "" : String(f.value);
  const thrStr = f.threshold == null ? "" : ` (thr ${f.threshold})`;
  node.innerHTML = `
    <span class="vuln-sev" data-sev="${escapeHtml(sev)}">${escapeHtml(sev)}</span>
    <span class="vuln-svc">${escapeHtml(f.service || "unknown")}</span>
    <span class="vuln-src">${escapeHtml(f.source || "")}</span>
    <span class="vuln-body">${escapeHtml(f.signal || "")} &middot; <code>${escapeHtml(valStr)}</code>${escapeHtml(thrStr)}${f.note ? " &middot; " + escapeHtml(f.note) : ""}</span>
  `;
  return node;
}

// ---- side panel: queued incident inbox ----------------------------------

function enqueueSidebarInbox(event) {
  if (!event || !event.id) return;
  // Don't re-queue an event that's already been promoted to the chat feed.
  if ($("feed").querySelector(`.row.inbox[data-incident="${event.id}"]`)) return;
  sidebarInbox.set(event.id, event);
  renderSidebarInbox();
}

function dropSidebarInbox(incidentId) {
  if (!sidebarInbox.has(incidentId)) return;
  sidebarInbox.delete(incidentId);
  renderSidebarInbox();
}

function countInlineInbox() {
  // Pending observer cards living inline in the main feed (not yet
  // investigated / dismissed) should also count against the inbox badge.
  return $("feed").querySelectorAll(".row.inbox:not(.dismissed):not(.handled)").length;
}

function renderSidebarInbox() {
  const list = $("inbox-list");
  const badge = $("inbox-count");
  const updated = $("inbox-updated");
  if (!list) return;
  list.innerHTML = "";
  const queued = sidebarInbox.size;
  const inline = countInlineInbox();
  const total = queued + inline;
  if (!queued) {
    list.innerHTML = inline
      ? `<div class="empty side-empty">// ${inline} pending in console — act on them there</div>`
      : `<div class="empty side-empty">// queue empty</div>`;
  } else {
    // Newest first.
    const events = Array.from(sidebarInbox.values()).reverse();
    for (const ev of events) list.appendChild(renderSidebarInboxCard(ev));
  }
  if (badge) {
    if (total > 0) {
      badge.hidden = false;
      badge.textContent = String(total);
    } else {
      badge.hidden = true;
      badge.textContent = "0";
    }
  }
  if (updated) updated.textContent = total ? "updated " + new Date().toTimeString().slice(0, 8) : "";
}

function renderSidebarInboxCard(event) {
  const card = document.createElement("div");
  card.className = "inbox-card";
  card.dataset.incident = event.id;
  const val = event.value != null ? Number(event.value).toFixed(2) : "—";
  const thr = event.threshold != null ? Number(event.threshold).toFixed(2) : "—";
  card.innerHTML = `
    <div class="inbox-card-head">
      <span class="inbox-card-id">${escapeHtml(event.id)}</span>
      <span class="inbox-card-svc">${escapeHtml(event.service || "unknown")}</span>
    </div>
    <div class="inbox-card-body">${escapeHtml(event.trigger || "anomaly")} · <code>${val}</code> (thr ${thr})</div>
    <div class="inbox-card-actions">
      <button class="primary inbox-card-investigate">Investigate</button>
      <button class="inbox-card-dismiss">Dismiss</button>
    </div>
  `;
  card.querySelector(".inbox-card-investigate").addEventListener("click", async () => {
    card.querySelectorAll("button").forEach(b => { b.disabled = true; });
    try {
      // If pipeline is still running, do nothing — leave card queued.
      if (activeIncident && activeIncident.status !== "done") {
        card.querySelectorAll("button").forEach(b => { b.disabled = false; });
        return;
      }
      // Otherwise drop from sidebar and treat as the new active incident.
      dropSidebarInbox(event.id);
      activeIncident = { id: event.id, event, urgency: null, status: "investigating" };
      resetFeed();
      resetPipeline();
      resetScoreboard();
      pushSystem(`investigating ${event.trigger || "anomaly"} on ${event.service || "unknown"} · ${event.id}`, "incident");
      setStatus();
      if (event.service) startSidebarObservability(event.service);
      const r = await fetch(`/api/incidents/${event.id}/investigate`, { method: "POST" });
      if (!r.ok) pushSystem(`failed to start pipeline · HTTP ${r.status}`, "error");
    } catch (err) {
      pushSystem(`failed to start pipeline · ${err}`, "error");
    }
  });
  card.querySelector(".inbox-card-dismiss").addEventListener("click", async () => {
    card.querySelectorAll("button").forEach(b => { b.disabled = true; });
    try { await fetch(`/api/incidents/${event.id}/dismiss`, { method: "POST" }); }
    catch { /* swallow */ }
    dropSidebarInbox(event.id);
  });
  return card;
}

// ---- side panel: tab switching -------------------------------------------

function wireSideTabs() {
  const tabs = document.querySelectorAll(".act-btn, .side-tab");
  const panels = {
    conversation: $("panel-conversation"),
    vulns:   $("panel-vulns"),
    inbox:   $("panel-inbox"),
    metrics: $("panel-metrics"),
    logs:    $("panel-logs"),
    github:  $("panel-github"),
  };
  tabs.forEach(btn => {
    btn.addEventListener("click", () => {
      tabs.forEach(b => { b.classList.toggle("active", b === btn); b.setAttribute("aria-selected", b === btn ? "true" : "false"); });
      const target = btn.dataset.tab;
      for (const [key, el] of Object.entries(panels)) {
        if (!el) continue;
        el.classList.toggle("active", key === target);
        el.hidden = key !== target;
      }
    });
  });
  // Clear-feed icon in chat header
  const clearBtn = $("btn-clear-feed");
  if (clearBtn) clearBtn.addEventListener("click", () => {
    const feed = $("feed");
    if (feed) feed.innerHTML = '<div class="empty">// feed flushed</div>';
  });
}

// ---- side panel: observability (metrics / logs / github) ----------------

function startSidebarObservability(service) {
  if (!service) return;
  if (sidebarService === service && sidebarRefreshHandle) return;  // already running
  sidebarService = service;
  if (sidebarRefreshHandle) clearInterval(sidebarRefreshHandle);
  loadSidebarObservability(service);
  sidebarRefreshHandle = setInterval(() => {
    if (!sidebarService) return;
    loadSidebarObservability(sidebarService);
  }, 15000);
}

function stopSidebarObservability() {
  if (sidebarRefreshHandle) clearInterval(sidebarRefreshHandle);
  sidebarRefreshHandle = null;
}

async function loadSidebarObservability(service) {
  await Promise.all([
    loadSidebarMetrics(service),
    loadSidebarLogs(service),
    loadSidebarGithub(service),
  ]);
}

async function loadSidebarMetrics(service) {
  try {
    const r = await fetch(`/api/topology`);
    if (!r.ok) return;
    const data = await r.json();
    renderMetrics(service, data.nodes || []);
  } catch (e) { /* swallow */ }
}

function renderMetrics(focusService, nodes) {
  const list = $("metrics-list");
  const updated = $("metrics-updated");
  if (!list) return;
  list.innerHTML = "";
  // focused service first, others below
  const sorted = [...nodes].sort((a, b) => {
    if (a.id === focusService) return -1;
    if (b.id === focusService) return 1;
    return 0;
  });
  for (const n of sorted) {
    list.appendChild(renderMetricTile(n, n.id === focusService));
  }
  updated.textContent = "Δ " + new Date().toTimeString().slice(0, 8);
}

function renderMetricTile(node, isFocus) {
  const err = node.metrics?.error_rate_pct;
  const lat = node.metrics?.p99_latency_ms;
  const errState = err == null ? "none" : err > 5 ? "breach" : err > 1 ? "warn" : "ok";
  const latState = lat == null ? "none" : lat > 500 ? "breach" : lat > 250 ? "warn" : "ok";
  const errStr   = err == null ? "—" : `${err.toFixed(2)}%`;
  const latStr   = lat == null ? "—" : `${lat.toFixed(0)}ms`;
  const el = document.createElement("div");
  el.className = "metric";
  el.innerHTML = `
    <span class="metric-svc">${escapeHtml(node.label || node.id)}${isFocus ? " <span class='side-section-sub'>focus</span>" : ""}</span>
    <span class="metric-value" data-state="${errState}">${errStr}</span>
    <span class="metric-label">error rate (1m)</span>
    <span class="metric-value" data-state="${latState}">${latStr}</span>
    <span class="metric-label">p99 latency (5m)</span>
    <span class="metric-sub">health: ${escapeHtml(node.health || "unknown")}</span>
  `;
  return el;
}

async function loadSidebarLogs(service) {
  try {
    const r = await fetch(`/api/sidebar/logs/${encodeURIComponent(service)}?minutes=15&limit=30`);
    if (!r.ok) return;
    const data = await r.json();
    renderLogs(data);
  } catch (e) { /* swallow */ }
}

function renderLogs(data) {
  const list = $("logs-list");
  const updated = $("logs-updated");
  if (!list) return;
  list.innerHTML = "";
  const events = data.events || [];
  if (!events.length) {
    const note = data.note || "no recent ERROR/CRITICAL events";
    list.innerHTML = `<div class="empty side-empty">${escapeHtml(note)}</div>`;
  } else {
    for (const ev of events.slice(0, 30)) {
      list.appendChild(renderLogRow(ev));
    }
  }
  updated.textContent = "Δ " + new Date().toTimeString().slice(0, 8);
}

function renderLogRow(ev) {
  const el = document.createElement("div");
  el.className = "log-row";
  const level = (ev.level || "info").toLowerCase();
  let when = "";
  const ts = ev.ts || ev.timestamp || ev._time;
  if (ts) {
    try { when = new Date(ts).toTimeString().slice(0, 8); } catch {}
  }
  const msg = ev.message || ev.msg || JSON.stringify(ev);
  el.innerHTML = `
    <span class="log-level" data-level="${escapeHtml(level)}">${escapeHtml(level)}</span>
    <span class="log-time">${escapeHtml(when)}</span>
    <span class="log-msg">${escapeHtml(String(msg).slice(0, 320))}</span>
  `;
  return el;
}

async function loadSidebarGithub(service) {
  try {
    const r = await fetch(`/api/sidebar/github/${encodeURIComponent(service)}`);
    if (!r.ok) return;
    const data = await r.json();
    renderGithub(data);
  } catch (e) { /* swallow */ }
}

function renderGithub(data) {
  const list = $("github-list");
  const updated = $("github-updated");
  if (!list) return;
  list.innerHTML = "";
  const commits = data.commits || [];
  const prs = data.prs || [];
  if (!commits.length && !prs.length) {
    const note = data.note || "no recent activity";
    list.innerHTML = `<div class="empty side-empty">${escapeHtml(note)}</div>`;
  } else {
    for (const c of commits) {
      const el = document.createElement("div");
      el.className = "gh-row";
      el.innerHTML = `<div class="gh-kind">commit</div>${escapeHtml(c.sha?.slice(0, 7) || "")} &middot; ${escapeHtml(c.message || "")}`;
      list.appendChild(el);
    }
    for (const p of prs) {
      const el = document.createElement("div");
      el.className = "gh-row";
      el.innerHTML = `<div class="gh-kind">pr #${escapeHtml(String(p.number || ""))}</div>${escapeHtml(p.title || "")}`;
      list.appendChild(el);
    }
  }
  updated.textContent = "Δ " + new Date().toTimeString().slice(0, 8);
}

function resetFeed() {
  $("feed").innerHTML = "";
  toolCalls.clear();
  for (const e of agentRows.values()) if (e.tickHandle) clearInterval(e.tickHandle);
  agentRows.clear();
  resetConversationMirror();
}

// ---- omnibox submit -------------------------------------------------------

const INC_RE = /^INC-[A-Z0-9]+$/i;

async function submitInput() {
  const ta = $("input");
  const text = (ta.value || "").trim();
  if (!text) return;
  ta.value = "";
  autoresize(ta);

  // INC-ID → investigate that incident's service (treat as new investigation target)
  if (INC_RE.test(text)) {
    await investigate(text);
    return;
  }
  // If an active incident is in progress, route as operator guidance.
  if (activeIncident && activeIncident.status !== "done") {
    pushUserMessage(text);
    try {
      const r = await fetch(`/api/incident/${activeIncident.id}/inject`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      if (!r.ok) pushSystem("failed to inject message", "error");
    } catch {
      pushSystem("failed to inject message", "error");
    }
    return;
  }
  // Otherwise start a new investigation.
  await investigate(text);
}

async function investigate(target) {
  try {
    const r = await fetch("/api/investigate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target }),
    });
    if (!r.ok) { pushSystem(`investigate failed (${r.status})`, "error"); return; }
    const data = await r.json();
    pushSystem(`investigation kicked off · ${data.incident_id} (${target})`);
    activeIncident = { id: data.incident_id, event: { service: target, trigger: "manual" }, urgency: null, status: "investigating" };
    setStatus();
    // Best-effort: if target looks like a service name, kick off sidebar fetches.
    const SERVICE_RE = /^[a-z][a-z0-9-]*-service$/;
    if (SERVICE_RE.test(target)) startSidebarObservability(target);
  } catch (e) {
    pushSystem(`investigate failed · ${e.message || e}`, "error");
  }
}

function autoresize(ta) {
  ta.style.height = "auto";
  ta.style.height = Math.min(ta.scrollHeight, 140) + "px";
}

// ---- wiring ---------------------------------------------------------------

function injectBuildHash() {
  const header = document.querySelector("header");
  if (!header || header.querySelector(".build-hash")) return;
  // Deterministic per-session hash so the demo still feels live-ish.
  const r = Math.floor(Math.random() * 0xfffff).toString(16).padStart(5, "0");
  const span = document.createElement("span");
  span.className = "build-hash";
  span.title = "build hash · per-session";
  span.textContent = `#${r}`;
  const ref = header.querySelector(".page-title");
  if (ref) ref.after(span); else header.prepend(span);
}

// ---- scoreboard + status dock --------------------------------------------
// Live counters derived from the same event stream that feeds the feed.
// Nothing here is fabricated — values come straight from observed events.
const sb = { tools: 0, errs: 0, latSum: 0, latN: 0, lastToolAt: null, runStart: null };
const _toolStart = new Map(); // tool_id -> ms

function sbSet(id, v) { const n = document.getElementById(id); if (n) n.textContent = v; }

function recordToolCall(toolId) {
  sb.tools++;
  _toolStart.set(toolId, performance.now());
  sbSet("sb-tools", sb.tools);
  if (!sb.runStart) sb.runStart = performance.now();
}

function recordToolResult(toolId, isError) {
  const t0 = _toolStart.get(toolId);
  if (t0 != null) {
    const dt = performance.now() - t0;
    sb.latSum += dt; sb.latN++;
    _toolStart.delete(toolId);
    sbSet("sb-lat", Math.round(sb.latSum / sb.latN));
  }
  if (isError) { sb.errs++; sbSet("sb-errs", sb.errs); }
}

function resetScoreboard() {
  sb.tools = 0; sb.errs = 0; sb.latSum = 0; sb.latN = 0;
  sb.lastToolAt = null; sb.runStart = null;
  _toolStart.clear();
  sbSet("sb-tools", 0); sbSet("sb-errs", 0);
  sbSet("sb-lat", "—"); sbSet("sb-mttr", "—");
  sbSet("sb-trace", activeIncident ? activeIncident.id.slice(-8) : "—");
}

function tickScoreboard() {
  // queue depth — derived from inbox panel rows currently rendered.
  const queueRows = document.querySelectorAll("#inbox-list .inbox-row").length;
  sbSet("sb-queue", queueRows);
  // mttr ticker = seconds since runStart while running, freezes on completion.
  if (sb.runStart && activeIncident && activeIncident.status !== "done") {
    sbSet("sb-mttr", Math.round((performance.now() - sb.runStart) / 1000));
  }
  // trace id = short tail of active incident id.
  if (activeIncident) sbSet("sb-trace", activeIncident.id.slice(-8));
  // dock clock
  const c = document.getElementById("sd-clock");
  if (c) c.textContent = new Date().toTimeString().slice(0, 8);
  // dock fleet mirrors the header meter.
  const fm = document.getElementById("fleet-meter");
  const sf = document.getElementById("sd-fleet");
  if (fm && sf) sf.textContent = fm.textContent;
  // mode pill: NORMAL when idle, RUN when pipeline live, GATE when HITL pending.
  const mode = document.getElementById("sd-mode");
  if (mode) {
    let next = "NORMAL", kind = "idle";
    if (pendingApproval) { next = "GATE"; kind = "waiting"; }
    else if (activeIncident && activeIncident.status !== "done") { next = "RUN"; kind = "running"; }
    if (mode.textContent !== next) mode.textContent = next;
    mode.dataset.kind = kind;
  }
}

function init() {
  injectBuildHash();
  resetPipeline();
  setStatus();
  setFleetMeter();
  resetScoreboard();
  wireSideTabs();
  const ta = $("input");
  ta.addEventListener("keydown", e => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submitInput(); }
  });
  ta.addEventListener("input", () => autoresize(ta));
  $("send").addEventListener("click", submitInput);
  connect();
  hydratePending();
  setInterval(tickScoreboard, 1000);
  tickScoreboard();
}

// Pull any incidents that were already in the pending queue when the page
// loaded — without this, anything the observer detected before the WebSocket
// connected stays invisible until the next detection.
async function hydratePending() {
  try {
    const r = await fetch("/api/state");
    if (!r.ok) return;
    const data = await r.json();
    const pending = data.pending_incidents || [];
    for (const ev of pending) {
      const busy = activeIncident && activeIncident.status !== "done";
      if (busy) enqueueSidebarInbox(ev);
      else      pushPendingIncident(ev);
    }
    renderSidebarInbox();
  } catch (err) { /* noop */ }
}

init();
