/* System Components page: list / test / edit integrations. */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const KIND_LABELS = {
  prometheus: "Prometheus",
  kubernetes: "Kubernetes",
  splunk: "Splunk",
  github: "GitHub",
  ollama: "Ollama",
  custom: "Custom HTTP",
};

const STATUS_LABELS = {
  reachable: "reachable",
  unreachable: "unreachable",
  unconfigured: "unconfigured",
  unknown: "unknown",
};

let integrations = [];
let editing = null;            // name currently being edited inline
let testingNames = new Set();  // names currently being tested

/* ---- activity bar tabs (placeholder for audit log) ---- */
function wireTabs() {
  $$(".act-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      $$(".act-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      const tab = btn.dataset.tab;
      const title = btn.getAttribute("title") || "";
      $("#sys-h").textContent = title;
      if (tab === "audit") {
        $("#components").innerHTML =
          '<div class="empty">Audit log view lands in a later step.</div>';
        $("#sys-summary").textContent = "";
      } else {
        loadIntegrations();
      }
    });
  });
}

/* ---- load + render ---- */
async function loadIntegrations() {
  try {
    const r = await fetch("/api/integrations");
    const data = await r.json();
    integrations = data.integrations || [];
    renderCards();
  } catch (err) {
    $("#components").innerHTML =
      `<div class="empty">Failed to load integrations: ${err.message}</div>`;
  }
}

function renderCards() {
  const grid = $("#components");
  grid.innerHTML = "";
  if (!integrations.length) {
    grid.innerHTML = '<div class="empty">No integrations configured.</div>';
    return;
  }
  const reachable = integrations.filter((i) => i.status === "reachable").length;
  $("#sys-summary").textContent =
    `${reachable}/${integrations.length} reachable`;
  integrations.forEach((i) => grid.appendChild(renderCard(i)));
}

function renderCard(card) {
  const el = document.createElement("div");
  el.className = "comp-card";
  el.dataset.name = card.name;

  const head = document.createElement("div");
  head.className = "comp-head";
  head.innerHTML = `
    <span class="comp-title">${KIND_LABELS[card.kind] || card.kind}</span>
    <span class="comp-name">${card.name}</span>
    <span class="comp-spacer"></span>
    <span class="comp-status" data-status="${card.status}">${STATUS_LABELS[card.status] || card.status}</span>
  `;
  el.appendChild(head);

  const body = document.createElement("div");
  body.className = "comp-body";
  body.appendChild(renderDetail(card));
  el.appendChild(body);

  if (card.last_error) {
    const err = document.createElement("div");
    err.className = "comp-error";
    err.textContent = card.last_error;
    el.appendChild(err);
  }

  const foot = document.createElement("div");
  foot.className = "comp-foot";
  const tested = card.last_tested_at
    ? `tested ${formatTime(card.last_tested_at)}`
    : "never tested";
  foot.innerHTML = `<span class="comp-tested">${tested}</span><span class="comp-spacer"></span>`;

  const testBtn = btn("Test", "comp-btn", () => doTest(card.name));
  if (testingNames.has(card.name)) {
    testBtn.disabled = true;
    testBtn.textContent = "Testing…";
  }
  foot.appendChild(testBtn);
  if (card.editable_fields && card.editable_fields.length) {
    foot.appendChild(btn(
      editing === card.name ? "Cancel" : "Edit",
      "comp-btn",
      () => {
        editing = editing === card.name ? null : card.name;
        renderCards();
      },
    ));
  }
  el.appendChild(foot);

  if (editing === card.name) {
    el.appendChild(renderEditor(card));
  }
  return el;
}

function renderDetail(card) {
  const wrap = document.createElement("div");
  wrap.className = "comp-detail";
  if (card.summary) {
    const s = document.createElement("div");
    s.className = "comp-summary";
    s.textContent = card.summary;
    wrap.appendChild(s);
  }
  const detail = card.detail || {};
  Object.entries(detail).forEach(([k, v]) => {
    const row = document.createElement("div");
    row.className = "comp-detail-row";
    row.innerHTML = `<span class="comp-k">${k}</span><span class="comp-v">${formatValue(v)}</span>`;
    wrap.appendChild(row);
  });
  return wrap;
}

function renderEditor(card) {
  const form = document.createElement("form");
  form.className = "comp-editor";
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const fields = {};
    card.editable_fields.forEach((k) => {
      const input = form.querySelector(`[name="${k}"]`);
      if (input && input.value.trim()) fields[k] = input.value.trim();
    });
    doUpdate(card.name, fields);
  });
  card.editable_fields.forEach((k) => {
    const row = document.createElement("label");
    row.className = "comp-edit-row";
    const current = (card.detail || {})[k];
    const placeholder = current != null ? String(current) : "";
    row.innerHTML = `
      <span class="comp-edit-k">${k}</span>
      <input name="${k}" type="text" value="${escapeAttr(placeholder)}" autocomplete="off" />
    `;
    form.appendChild(row);
  });
  const actions = document.createElement("div");
  actions.className = "comp-edit-actions";
  const save = document.createElement("button");
  save.type = "submit";
  save.className = "comp-btn comp-btn-primary";
  save.textContent = "Save";
  actions.appendChild(save);
  form.appendChild(actions);
  return form;
}

/* ---- actions ---- */
async function doTest(name) {
  testingNames.add(name);
  renderCards();
  try {
    const r = await fetch(`/api/integrations/${encodeURIComponent(name)}/test`, {
      method: "POST",
    });
    if (r.ok) {
      const updated = await r.json();
      replaceCard(updated);
    }
  } catch (err) {
    /* ignore — card keeps last-error */
  } finally {
    testingNames.delete(name);
    renderCards();
  }
}

async function doTestAll() {
  const btnEl = $("#btn-refresh-all");
  btnEl.disabled = true;
  btnEl.textContent = "Testing…";
  integrations.forEach((i) => testingNames.add(i.name));
  renderCards();
  try {
    const r = await fetch("/api/integrations/test-all", { method: "POST" });
    if (r.ok) {
      const data = await r.json();
      integrations = data.integrations || integrations;
    }
  } finally {
    testingNames.clear();
    btnEl.disabled = false;
    btnEl.innerHTML = `
      <svg viewBox="0 0 16 16" width="12" height="12" fill="currentColor" aria-hidden="true"><path d="M1.705 8.005a.75.75 0 0 1 .834.656 5.5 5.5 0 0 0 9.592 2.97l-1.204-1.204a.25.25 0 0 1 .177-.427h3.646a.25.25 0 0 1 .25.25v3.646a.25.25 0 0 1-.427.177l-1.38-1.38A7.001 7.001 0 0 1 1.05 8.84a.75.75 0 0 1 .656-.834ZM8 1a6.99 6.99 0 0 1 5.927 3.06l1.422-1.422A.25.25 0 0 1 15.776 2.815v3.646a.25.25 0 0 1-.25.25H11.88a.25.25 0 0 1-.177-.427l1.225-1.224a5.5 5.5 0 0 0-9.59 1.929.75.75 0 0 1-1.444-.402A7.002 7.002 0 0 1 8 1Z"/></svg>
      Re-test all`;
    renderCards();
  }
}

async function doUpdate(name, fields) {
  try {
    const r = await fetch(`/api/integrations/${encodeURIComponent(name)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fields }),
    });
    if (!r.ok) return;
    const updated = await r.json();
    replaceCard(updated);
    editing = null;
    renderCards();
    // Auto-retest after save so the operator sees the result immediately.
    doTest(name);
  } catch (err) {
    /* ignore */
  }
}

async function doAddCustom() {
  const name = prompt("Integration name (no spaces):");
  if (!name) return;
  const url = prompt("URL to probe (e.g. http://host:port/health):");
  if (!url) return;
  try {
    const r = await fetch("/api/integrations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, url }),
    });
    if (!r.ok) {
      const detail = await r.json().catch(() => ({}));
      alert(detail.detail || "failed to add integration");
      return;
    }
    const created = await r.json();
    integrations.push(created);
    renderCards();
    doTest(created.name);
  } catch (err) {
    alert(`failed: ${err.message}`);
  }
}

/* ---- helpers ---- */
function replaceCard(updated) {
  const idx = integrations.findIndex((i) => i.name === updated.name);
  if (idx >= 0) integrations[idx] = updated;
}

function btn(label, cls, onClick) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = cls;
  b.textContent = label;
  b.addEventListener("click", onClick);
  return b;
}

function formatValue(v) {
  if (v === null || v === undefined) return "—";
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (Array.isArray(v)) return v.length ? v.join(", ") : "—";
  return String(v);
}

function formatTime(iso) {
  try {
    const d = new Date(iso);
    const now = Date.now();
    const delta = Math.max(0, (now - d.getTime()) / 1000);
    if (delta < 60) return `${Math.round(delta)}s ago`;
    if (delta < 3600) return `${Math.round(delta / 60)}m ago`;
    return d.toLocaleTimeString();
  } catch {
    return iso;
  }
}

function escapeAttr(s) {
  return String(s).replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/* ---- Copilot ---- */
const copilotHistory = []; // [{role, text}] — last N turns sent on each request

function appendChat(role, html, opts = {}) {
  const body = $("#sys-chat-body");
  const bubble = document.createElement("div");
  bubble.className = `sys-chat-bubble ${role}`;
  if (opts.dataset) Object.entries(opts.dataset).forEach(([k, v]) => bubble.dataset[k] = v);
  if (typeof html === "string") bubble.innerHTML = html;
  else bubble.appendChild(html);
  body.appendChild(bubble);
  body.scrollTop = body.scrollHeight;
  return bubble;
}

function appendTypingIndicator() {
  return appendChat("assistant typing",
    `<span class="sys-typing"><span></span><span></span><span></span></span>`);
}

function formatAssistantText(text) {
  // Render light markdown: `code`, **bold**, bullet lines. Preserve linebreaks.
  let html = escapeAttr(text);
  html = html.replace(/`([^`]+)`/g, (_, c) => `<code>${c}</code>`);
  html = html.replace(/\*\*([^*]+)\*\*/g, (_, c) => `<b>${c}</b>`);
  html = html.replace(/\n/g, "<br>");
  return html;
}

function parseCommand(raw) {
  const text = raw.trim();
  const low = text.toLowerCase();

  // help
  if (low === "help" || low === "?") return { op: "help" };

  // test all / re-test all
  if (/^(re-?test|test)\s+all$/.test(low)) return { op: "test_all" };

  // test <name>  /  is <name> reachable?  /  check <name>
  let m = low.match(/^(?:test|check)\s+([\w.-]+)/);
  if (m) return { op: "test", name: m[1] };
  m = low.match(/^(?:is|are)\s+([\w.-]+)\s+(?:reachable|up|alive|working)/);
  if (m) return { op: "test", name: m[1] };
  m = low.match(/^why\s+is\s+(?:the\s+)?([\w.-]+)\s+/);
  if (m) return { op: "test", name: m[1] };

  // add <name> <url>
  m = text.match(/^add\s+([\w.-]+)\s+(\S+)/i);
  if (m) return { op: "add", name: m[1], url: m[2] };

  // set <name> <key>=<value> [<key>=<value> ...]
  m = text.match(/^set\s+([\w.-]+)\s+(.+)$/i);
  if (m) {
    const fields = {};
    for (const kv of m[2].matchAll(/(\w+)\s*=\s*("([^"]+)"|(\S+))/g)) {
      fields[kv[1]] = kv[3] || kv[4];
    }
    if (Object.keys(fields).length) return { op: "set", name: m[1], fields };
  }

  // point <name> at <url>   /   point <name> token <token>
  m = text.match(/^point\s+([\w.-]+)\s+(?:at\s+)?(\S+)(?:\s+(?:with\s+)?(?:token\s+)?(\S+))?/i);
  if (m) {
    const fields = {};
    if (/^https?:/i.test(m[2])) fields.url = m[2];
    if (m[3]) fields.token = m[3];
    if (Object.keys(fields).length) return { op: "set", name: m[1], fields };
  }

  return { op: "unknown", text };
}

function statusBadge(card) {
  return `<span class="comp-status" data-status="${card.status}">${STATUS_LABELS[card.status] || card.status}</span>`;
}

function findCardCaseInsensitive(name) {
  const lower = name.toLowerCase();
  return integrations.find((i) => i.name.toLowerCase() === lower);
}

async function handleCopilot(raw) {
  const userMsg = raw.trim();
  if (!userMsg) return;
  appendChat("user", escapeAttr(userMsg).replace(/\n/g, "<br>"));
  copilotHistory.push({ role: "user", text: userMsg });

  const typing = appendTypingIndicator();
  try {
    const r = await fetch("/api/system/copilot", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: userMsg, history: copilotHistory.slice(-10) }),
    });
    if (!r.ok) {
      const detail = await r.json().catch(() => ({}));
      typing.remove();
      appendChat("assistant",
        `<b>Copilot error.</b> ${escapeAttr(detail.detail || `HTTP ${r.status}`)}.<br>` +
        `<span class='sys-chat-note'>Is Ollama running at <code>${escapeAttr(detail.ollama_url || "configured URL")}</code>? You can also use direct controls in the cards on the left.</span>`);
      return;
    }
    const data = await r.json();
    typing.remove();
    const text = (data.text || "").trim() || "(no response)";
    copilotHistory.push({ role: "assistant", text });
    appendChat("assistant", formatAssistantText(text));
    // Refresh card grid so any updates the LLM made show up.
    loadIntegrations();
  } catch (err) {
    typing.remove();
    appendChat("assistant", `<b>Copilot error.</b> ${escapeAttr(String(err.message || err))}`);
  }
}

function wireComposer() {
  const input = $("#sys-input");
  const send = $("#sys-send");
  if (!input || !send) return;
  input.disabled = false;
  send.disabled = false;
  const submit = () => {
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    input.style.height = "";
    handleCopilot(text);
  };
  send.addEventListener("click", submit);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  });
  // auto-grow
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = Math.min(160, input.scrollHeight) + "px";
  });
}

/* ---- boot ---- */
function init() {
  // Tag the h2 so we can rewrite it on tab switch.
  const h = $(".sys-h");
  if (h) h.id = "sys-h";
  wireTabs();
  $("#btn-refresh-all").addEventListener("click", doTestAll);
  $("#btn-add-custom").addEventListener("click", doAddCustom);
  $("#btn-clear-chat").addEventListener("click", () => {
    const body = $("#sys-chat-body");
    // Keep only the first (intro) bubble.
    const intro = body.querySelector(".sys-chat-bubble.assistant");
    body.innerHTML = "";
    if (intro) body.appendChild(intro);
  });
  wireComposer();
  loadIntegrations();
}

document.addEventListener("DOMContentLoaded", init);
