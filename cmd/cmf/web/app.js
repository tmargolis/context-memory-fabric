// CMF management page: Status, Features, Review changes (plan section 3.3).
// Everything shown comes from the cmf engine's API; text is always set with
// textContent, never parsed as HTML.
"use strict";

const $ = (id) => document.getElementById(id);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v === true) node.setAttribute(k, "");
    else if (v !== false && v != null) node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

async function api(path, options = {}) {
  const res = await fetch(path, { credentials: "same-origin", ...options });
  const data = await res.json().catch(() => ({ error: res.statusText }));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

// Runs a streaming action, showing its log in the dialog; resolves to the result line.
async function stream(path, body, title) {
  openDialog(title, "");
  const pre = $("dialog-body");
  const res = await fetch(path, {
    method: "POST", credentials: "same-origin",
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    pre.textContent = await res.text();
    return { ok: false, error: pre.textContent };
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let text = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    text += decoder.decode(value, { stream: true });
    pre.textContent = text.split("\n@@result ")[0];
    pre.scrollTop = pre.scrollHeight;
  }
  const marker = text.lastIndexOf("@@result ");
  const result = marker >= 0 ? JSON.parse(text.slice(marker + 9).trim()) : { ok: false, error: "no result" };
  const foot = $("dialog-foot");
  foot.replaceChildren(el("span", { class: result.ok ? "" : "muted" }, result.ok ? "Done." : "Failed: " + result.error));
  if (result.stamp) foot.append(el("span", { class: "muted" }, ` Backup ${result.stamp} (undo from Features → Backups).`));
  return result;
}

function openDialog(title, body, tools = [], foot = []) {
  $("dialog-title").textContent = title;
  $("dialog-body").textContent = body;
  $("dialog-tools").replaceChildren(...tools);
  $("dialog-foot").replaceChildren(...foot);
  if (!$("dialog").open) $("dialog").showModal();
}

// --- tabs -----------------------------------------------------------------------------------

// Each tab is a history entry (#status, #features, #review), so the browser's
// Back button returns to the previous tab where it was scrolled to. Status
// keeps its last results; it re-checks on load, after a change, and on
// "Check again".
const tabs = ["status", "features", "review"];
const scrollY = {}; // tab -> where it was scrolled to
let current = null;
history.scrollRestoration = "manual"; // the panels share one page, so restore per tab here

function show(tab, { push = true } = {}) {
  if (!tabs.includes(tab) || (tab === "review" && !pending)) tab = "status";
  if (current) scrollY[current] = window.scrollY;
  if (push && tab !== current) history.pushState({ tab }, "", "#" + tab);
  current = tab;
  for (const b of document.querySelectorAll(".tabs button")) b.setAttribute("aria-selected", String(b.dataset.tab === tab));
  for (const p of document.querySelectorAll(".panel")) p.hidden = p.id !== tab;
  if (tab === "status" && !statusShown) loadStatus();
  if (tab === "features" && !features) featuresLoading = loadFeatures();
  const y = scrollY[tab] || 0;
  requestAnimationFrame(() => window.scrollTo(0, y));
}
for (const b of document.querySelectorAll(".tabs button")) b.addEventListener("click", () => show(b.dataset.tab));
window.addEventListener("popstate", (ev) => show((ev.state && ev.state.tab) || location.hash.slice(1), { push: false }));

// --- status ---------------------------------------------------------------------------------

const levelWords = { ok: "All checks pass.", warn: "Working, with warnings below.", fail: "Something needs fixing." };

let statusShown = false; // Status has results on screen

// "10:42:07", with the date too when it isn't today.
function checkedAt(iso) {
  const t = new Date(iso);
  if (isNaN(t)) return "";
  const time = t.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  return t.toDateString() === new Date().toDateString() ? time : t.toLocaleDateString() + " " + time;
}

async function loadStatus() {
  statusShown = true;
  $("summary").textContent = "Checking this install…";
  $("summary").className = "summary";
  let s;
  try {
    s = await api("/api/status");
  } catch (e) {
    $("summary").textContent = "Couldn't check: " + e.message;
    statusShown = false;
    return;
  }
  const when = checkedAt(s.checked_at);
  $("summary").replaceChildren(levelWords[s.overall] || s.overall,
    when ? el("span", { class: "checked-at", title: s.checked_at }, "checked " + when) : null);
  $("summary").classList.add(s.overall);
  // One number when the cmf program and the server agree; both when they don't.
  const ver = s.versions_match ? `CMF ${s.version}` : `cmf ${s.version} · server ${s.server_version}`;
  $("meta").textContent = ver + (s.graph ? ` · graph ${s.graph}` + (s.nodes >= 0 ? `, ${s.nodes} nodes` : "") : "");
  $("browser-link").href = s.browser_url;
  $("browser-link").title = s.browser_url + (s.graph ? ` · pick graph “${s.graph}” there` : "");
  $("checks").replaceChildren(...s.checks.map((c) => el("li", {},
    el("span", { class: "dot " + (c.level === "n/a" ? "na" : c.level), title: c.level }),
    el("div", {}, checkTitle(c),
      c.about ? el("div", { class: "check-about" }, ...withCode(c.about)) : null,
      el("div", { class: "check-detail" }, c.detail)),
    c.fix && c.level !== "ok"
      ? el("button", { class: "small fix", title: c.fix.steps.map((st) => st.command).join("\n"), onclick: () => runFix(c, c.fix) }, "Fix: " + c.fix.label)
      : c.action
      ? el("button", { class: "small fix", title: c.action.steps.map((st) => st.command).join("\n"), onclick: () => runFix(c, c.action) }, c.action.label)
      : el("span"),
  )));
}

// Text with `backticks` as code spans (still text nodes, never HTML).
function withCode(text) {
  return text.split("`").map((part, i) => (i % 2 ? el("code", {}, part) : part));
}

// A row's title links to where its setting is changed: a card or group on
// Features, or the setup guide for things set outside the app.
function checkTitle(c) {
  if (c.feature) {
    return el("a", { class: "check-title", href: "#features", title: "Change this in Features",
      onclick: (ev) => { ev.preventDefault(); goToFeature(c.feature); } }, c.title);
  }
  if (c.doc && /^https:\/\//.test(c.doc)) {
    return el("a", { class: "check-title", href: c.doc, target: "_blank", rel: "noopener", title: "Opens the setup guide" }, c.title, " ↗");
  }
  return el("div", { class: "check-title" }, c.title);
}

async function goToFeature(target) {
  show("features");
  await featuresLoading;
  const [kind, id] = target.includes(":") ? target.split(":") : ["feature", target];
  const node = $(kind + "-" + id);
  if (!node) return;
  node.scrollIntoView({ behavior: "smooth", block: "start" });
  node.classList.remove("flash");
  void node.offsetWidth; // restart the highlight
  node.classList.add("flash");
}

async function runFix(c, fix) {
  const steps = fix.steps.map((st) => "  $ " + st.command).join("\n");
  if (!confirm(`${fix.label}\n\nThis runs:\n${steps}`)) return;
  await stream(`/api/fix/${encodeURIComponent(c.id)}`, null, fix.label);
  loadStatus();
}

$("refresh").addEventListener("click", loadStatus);

$("diag-open").addEventListener("click", async () => {
  openDialog("Diagnostics (shown before copying)", "Building the report…");
  try {
    const { report } = await api("/api/diagnostics");
    const copy = el("button", { class: "primary", onclick: async () => {
      await navigator.clipboard.writeText(report);
      copy.textContent = "Copied";
    } }, "Copy to clipboard");
    openDialog("Diagnostics (shown before copying)", report, [
      el("span", { class: "muted" }, "Keys, tokens, names, paths and memory text are removed. Read it, then copy it to send."),
    ], [copy]);
  } catch (e) {
    $("dialog-body").textContent = "Couldn't build the report: " + e.message;
  }
});

$("logs-open").addEventListener("click", () => {
  const service = el("select", {}, ...["", "cmf-server", "falkordb", "cmf-poller"].map((v) => el("option", { value: v }, v || "all services")));
  const errorsOnly = el("input", { type: "checkbox" });
  const load = async () => {
    $("dialog-body").textContent = "Loading…";
    try {
      // The server marks error lines (the rule diagnostics uses); they're
      // shown in red, or alone with "errors only".
      const { text, errors = [] } = await api(`/api/logs?tail=400&service=${encodeURIComponent(service.value)}`);
      const bad = new Set(errors);
      const lines = text.split("\n").map((l, i) => ({ l, err: bad.has(i) })).filter((x) => !errorsOnly.checked || x.err);
      $("dialog-body").replaceChildren(...(lines.length ? lines.map((x) => el("span", { class: "line" + (x.err ? " err" : "") }, x.l))
        : [errorsOnly.checked ? "No error lines in the last 400." : "No log lines."]));
      $("dialog-body").scrollTop = $("dialog-body").scrollHeight;
    } catch (e) {
      $("dialog-body").textContent = e.message;
    }
  };
  service.addEventListener("change", load);
  errorsOnly.addEventListener("change", load);
  openDialog("Logs", "", [service, el("label", {}, errorsOnly, " errors only"), el("button", { class: "small", onclick: load }, "Reload")]);
  load();
});

// --- features -------------------------------------------------------------------------------

let features = null; // the machine's state as loaded
let featuresLoading = null; // the load in progress, if any
let edits = {};      // feature id -> {enabled?, <input id>: value}
let clientEdits = {}; // app id -> bool

// The transcript source the poller reads for each AI app (always on).
const sourceFor = { "claude-code": "claude_code", "codex": "codex", "antigravity": "antigravity" };
// Claude Desktop is covered by the claude.ai connector card, not an entry.
const connectorApps = new Set(["claude-code", "claude-desktop"]);
const connectorDoc = "https://github.com/tmargolis/context-memory-fabric/blob/main/docs/SETUP.md#the-claudeai-connector-claude-desktop-cowork-claude-code";

const groupNames = { core: "Core", models: "Models", knowledge: "Knowledge", sources: "Sources", apps: "AI apps" };

async function loadFeatures() {
  $("groups").replaceChildren(el("p", { class: "muted" }, "Reading this machine…"));
  try {
    features = await api("/api/features");
  } catch (e) {
    $("groups").replaceChildren(el("p", {}, "Couldn't read the features: " + e.message));
    return;
  }
  edits = {};
  clientEdits = {};
  renderFeatures();
  loadBackups();
}

function edit(id, key, value) {
  (edits[id] ||= {})[key] = value;
  renderFeatures();
}

function featureEnabled(f) {
  const e = edits[f.id];
  return e && "enabled" in e ? e.enabled : f.enabled;
}

function inputControl(f, input) {
  const e = edits[f.id] || {};
  const current = input.id in e ? e[input.id] : input.value;
  if (input.type === "choice") {
    return el("select", { onchange: (ev) => edit(f.id, input.id, ev.target.value) },
      ...input.choices.map((c) => el("option", { value: c, selected: c === current }, c)));
  }
  if (input.type === "ranked") {
    const list = Array.isArray(current) ? current : String(current || "").split(",").map((x) => x.trim()).filter(Boolean);
    const set = (next) => edit(f.id, input.id, next);
    const rows = list.map((name, i) => el("div", { class: "row" },
      el("span", { class: "name" }, `${i + 1}. ${name}`),
      el("button", { type: "button", class: "small", disabled: i === 0, title: "Move up", onclick: () => { const n = [...list]; [n[i - 1], n[i]] = [n[i], n[i - 1]]; set(n); } }, "↑"),
      el("button", { type: "button", class: "small", disabled: i === list.length - 1, title: "Move down", onclick: () => { const n = [...list]; [n[i + 1], n[i]] = [n[i], n[i + 1]]; set(n); } }, "↓"),
      el("button", { type: "button", class: "small", disabled: list.length === 1, title: "Remove", onclick: () => set(list.filter((_, j) => j !== i)) }, "✕"),
    ));
    const rest = input.choices.filter((c) => !list.includes(c));
    const pick = el("select", {}, ...rest.map((c) => el("option", { value: c }, c)));
    return el("div", { class: "ranked" }, ...rows, rest.length ? el("div", { class: "add" }, pick,
      el("button", { type: "button", class: "small", onclick: () => set([...list, pick.value]) }, "Add as fallback")) : null,
      el("span", { class: "muted" }, "The first is used; the rest take over on rate-limit, quota or key errors."));
  }
  const secret = input.type === "secret";
  return el("input", {
    type: secret ? "password" : "text",
    autocomplete: "off",
    spellcheck: "false",
    value: secret ? (input.id in e ? e[input.id] : "") : current || "",
    placeholder: secret ? (input.set ? `set (${input.value}); type to replace` : "not set") : input.type === "url" ? "https://…" : "",
    onchange: (ev) => edit(f.id, input.id, ev.target.value),
  });
}

function featureCard(f) {
  const on = featureEnabled(f);
  const changed = !!edits[f.id];
  let control;
  if (f.coming_soon) control = el("span", { class: "badge soon" }, "coming soon");
  else if (f.always_on) control = el("span", { class: "badge" }, "always on");
  else if (f.derived) control = el("span", { class: "badge" }, f.enabled ? "on" : "off");
  else control = el("label", { class: "switch", title: on ? "On" : "Off" },
    el("input", { type: "checkbox", checked: on, "aria-label": f.label, onchange: (ev) => edit(f.id, "enabled", ev.target.checked) }), el("span"));
  const showInputs = (on || f.always_on) && f.inputs && f.inputs.length;
  return el("div", { class: "card" + (changed ? " changed" : "") + (f.coming_soon ? " soon" : ""), id: "feature-" + f.id },
    el("div", { class: "card-head" },
      el("div", { class: "text" }, el("div", { class: "card-title" }, f.label), el("div", { class: "card-desc" }, f.description)),
      control),
    showInputs ? el("div", { class: "inputs" }, ...f.inputs.map((i) => el("label", {}, i.label, inputControl(f, i)))) : null,
    on && !f.enabled && f.cost_note ? el("div", { class: "cost" }, "Cost: " + f.cost_note) : null);
}

function captureState(source) {
  return (features.capture || []).find((s) => s.id === source);
}

// The recommended way for Claude Desktop (Chat, Cowork, Code tab) and Claude
// Code to reach CMF: one connector on the claude.ai account. No switch: it's
// added in claude.ai, and the poller always reads those apps' transcripts.
function connectorCard() {
  const url = features.public_url ? features.public_url.replace(/\/$/, "") + "/mcp" : "";
  const state = features.connector ? "active: Claude calls came through it in the last day"
    : url ? "not seen yet: remote access is on; add the connector in claude.ai" : "not set up: turn on remote access first";
  const cowork = captureState("claude_cowork");
  return el("div", { class: "card", id: "client-connector" },
    el("div", { class: "card-head" },
      el("div", { class: "text" }, el("div", { class: "card-title" }, "claude.ai connector (recommended for Claude)"),
        el("div", { class: "card-desc" }, state)),
      el("span", { class: "badge" + (features.connector ? "" : " soon") }, features.connector ? "active" : "not active")),
    el("div", { class: "card-desc note" },
      "One connector on your claude.ai account covers Claude Desktop (Chat, Cowork and the Code tab), Claude Code and claude.ai. ",
      url ? null : el("span", {}, "First turn on remote access in a terminal: ", el("code", {}, "cmf remote enable tailscale"), ". "),
      "Then in claude.ai: Settings → Connectors → Add custom connector, URL ", el("code", {}, url || "https://<your address>/mcp"),
      ". CMF's consent page asks for ", el("code", {}, "CMF_MCP_OAUTH_PASSWORD"), " from ~/.cmf/.env. ",
      el("a", { href: connectorDoc, target: "_blank", rel: "noopener" }, "Step by step ↗")),
    cowork && cowork.present ? el("div", { class: "card-desc note" }, "Cowork's sessions: the poller reads " + (cowork.folder || cowork.dirs[0]) + ".") : null);
}

function clientCard(c) {
  const has = (c.entries || []).some((e) => e.cmf);
  const healthy = has && c.entries.filter((e) => e.cmf).length === 1 && c.entries.find((e) => e.cmf).correct;
  const want = c.id in clientEdits ? clientEdits[c.id] : has;
  const viaConnector = !has && features.connector && connectorApps.has(c.id);
  const state = !c.installed ? "not installed" : c.error ? "can't read its config" : viaConnector ? "connected through the claude.ai connector; no entry needed"
    : !has ? "no CMF entry" : healthy ? "connected" : "needs fixing (duplicates or a stale entry)";
  const src = sourceFor[c.id] && captureState(sourceFor[c.id]);
  return el("div", { class: "card" + (c.id in clientEdits ? " changed" : ""), id: "client-" + c.id },
    el("div", { class: "card-head" },
      el("div", { class: "text" }, el("div", { class: "card-title" }, c.label), el("div", { class: "card-desc" }, state + (c.exists ? " · " + c.path : ""))),
      el("label", { class: "switch", title: "A CMF entry in this app's own config" },
        el("input", { type: "checkbox", checked: want, disabled: !c.installed || !!c.error, "aria-label": c.label + ": CMF entry",
          onchange: (ev) => { if (ev.target.checked === has && healthy) delete clientEdits[c.id]; else clientEdits[c.id] = ev.target.checked; renderFeatures(); } }),
        el("span"))),
    connectorApps.has(c.id) ? el("div", { class: "card-desc note" },
      "The switch writes a local entry, for when you don't use the claude.ai connector above. With the connector, leave it off, or CMF's tools appear twice.") : null,
    src && src.present ? el("div", { class: "card-desc note" }, "Transcripts: the poller reads " + (src.folder || src.dirs[0]) + ".") : null);
}

function request() {
  const req = {};
  const fs = {};
  for (const [id, e] of Object.entries(edits)) {
    const f = features.features.find((x) => x.id === id);
    const out = {};
    for (const [k, v] of Object.entries(e)) {
      if (k === "enabled") { if (v !== f.enabled) out.enabled = v; continue; }
      const input = f.inputs.find((i) => i.id === k);
      if (input.type === "secret" && v === "") continue; // blank: keep what's set
      out[k] = v;
    }
    if ((out.enabled ?? featureEnabled(f)) && Object.keys(out).some((k) => k !== "enabled")) out.enabled = true;
    if (Object.keys(out).length) fs[id] = out;
  }
  if (Object.keys(fs).length) req.features = fs;
  if (Object.keys(clientEdits).length) req.clients = clientEdits;
  return req;
}

function renderFeatures() {
  const cards = (g) => features.features.filter((f) => f.group === g && !f.hidden).map(featureCard);
  const order = features.groups.filter((g) => g !== "sources");
  const groups = [...order.map((g) => [g, cards(g)]), ["apps", [connectorCard(), ...features.clients.filter((c) => c.id !== "claude-desktop").map(clientCard)]], ["sources", cards("sources")]];
  $("groups").replaceChildren(...groups.filter(([, cards]) => cards.length).map(([g, cards]) =>
    el("div", { class: "group", id: "group-" + g }, el("h3", {}, groupNames[g] || g), ...cards)));
  const req = request();
  const n = Object.keys(req.features || {}).length + Object.keys(req.clients || {}).length;
  $("dirty-note").textContent = n ? `${n} change${n > 1 ? "s" : ""} not applied yet.` : "No changes yet.";
  $("review-btn").disabled = $("discard").disabled = n === 0;
}

$("discard").addEventListener("click", () => { edits = {}; clientEdits = {}; renderFeatures(); });

async function loadBackups() {
  try {
    const list = await api("/api/backups");
    $("backups").replaceChildren(...(list || []).slice().reverse().slice(0, 15).map((b) => el("li", {},
      el("span", {}, b.Stamp), el("span", { class: "what" }, b.Note || b.Kind),
      b.Kind === "cmf" ? el("button", { class: "small", onclick: () => restore(b.Stamp) }, "Restore") : el("span", { class: "muted" }, "restore from the CLI"))));
  } catch (e) {
    $("backups").replaceChildren(el("li", {}, e.message));
  }
}

async function restore(stamp) {
  if (!confirm(`Put back the files from backup ${stamp}? The current ones are backed up first, so this can be undone too.`)) return;
  await stream(`/api/restore/${encodeURIComponent(stamp)}`, null, "Restoring " + stamp);
  features = null;
  featuresLoading = loadFeatures();
  statusShown = false;
}

// --- review ---------------------------------------------------------------------------------

let pending = null;

$("review-btn").addEventListener("click", async () => {
  pending = request();
  $("review-tab").hidden = false;
  show("review");
  const body = $("review-body");
  body.replaceChildren(el("p", { class: "muted" }, "Working out the changes…"));
  let plan;
  try {
    plan = await api("/api/plan", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(pending) });
  } catch (e) {
    body.replaceChildren(el("div", { class: "errors" }, e.message));
    $("apply-btn").disabled = true;
    return;
  }
  const parts = [];
  if (plan.errors && plan.errors.length) parts.push(el("div", { class: "errors" }, "These have to change before anything can be applied:", el("ul", {}, ...plan.errors.map((x) => el("li", {}, x)))));
  if (plan.warnings && plan.warnings.length) parts.push(el("div", { class: "warnings" }, "Notes:", el("ul", {}, ...plan.warnings.map((x) => el("li", {}, x)))));
  if (plan.blocked) parts.push(el("div", { class: "errors" }, plan.blocked));
  if (plan.ok && plan.empty) parts.push(el("p", {}, "Nothing to change: the machine already matches."));
  if (plan.review) parts.push(el("div", { class: "review-list" }, plan.review.map((l) => l.replace(/^ {2}/, "")).join("\n")));
  body.replaceChildren(...parts);
  $("apply-btn").disabled = !plan.ok || plan.empty || !!plan.blocked;
});

$("back").addEventListener("click", () => show("features"));

$("apply-btn").addEventListener("click", async () => {
  $("apply-btn").disabled = true;
  const result = await stream("/api/apply", pending, "Applying");
  if (result.ok) {
    $("review-tab").hidden = true;
    features = null;
    pending = null;
    statusShown = false; // re-check what changed
    show("status");
  } else {
    $("apply-btn").disabled = false;
  }
});

// --- lifetime -------------------------------------------------------------------------------

// The server stops when this tab closes, or after 30 minutes without a
// request. The open /api/alive request is how it knows the tab is still
// there, even if the browser quits without sending the "bye".
fetch("/api/alive", { credentials: "same-origin" }).catch(() => {});
window.addEventListener("pagehide", () => navigator.sendBeacon("/api/bye"));

history.replaceState({ tab: "status" }, "", "#status");
show("status", { push: false });
