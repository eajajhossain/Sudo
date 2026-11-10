"use strict";
// All page content, some of it from websites and programs, is written with textContent.

const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
const stored = (() => { try { return sessionStorage.getItem("sudo-token"); } catch { return null; } })();
const token = params.get("token") || stored || "";
try { sessionStorage.setItem("sudo-token", token); } catch {}
if (params.has("token")) history.replaceState(null, "", "/");

const PHASES = ["UNDERSTAND", "PLAN", "OBSERVE", "EXECUTE", "VERIFY", "REPORT"];
const FRIENDLY = {
  browser_open: "Opening a page", browser_snapshot: "Reading the page", browser_read: "Reading the page",
  browser_screenshot: "Looking at the page", browser_click: "Clicking", browser_type: "Typing in the browser",
  browser_edit_text: "Editing text", browser_type_focused: "Typing in the browser", browser_type_secret: "Waiting for your password",
  browser_press: "Pressing a key", browser_select: "Choosing an option", browser_check: "Ticking a box", browser_scroll: "Scrolling",
  browser_nav: "Navigating", browser_tabs: "Managing tabs", browser_wait: "Waiting for the page", browser_upload: "Attaching a file",
  browser_download: "Downloading", desktop_screenshot: "Looking at the screen", desktop_click: "Clicking", desktop_move: "Moving the pointer",
  desktop_drag: "Dragging", desktop_scroll: "Scrolling", desktop_type: "Typing", desktop_type_secret: "Waiting for your password",
  desktop_key: "Pressing keys", clipboard_read: "Reading the clipboard", clipboard_write: "Copying", window_list: "Checking windows",
  window_focus: "Switching windows", window_close: "Closing a window", app_open: "Opening an app", terminal_open: "Opening a terminal",
  ui_elements: "Reading the window", ui_click: "Clicking", ui_read_focused: "Checking the field", run_command: "Running a command",
  file_read: "Reading a file", file_write: "Writing a file", file_edit: "Editing a file", file_list: "Listing a folder",
  file_search: "Searching files", file_move: "Moving a file", file_copy: "Copying a file", file_mkdir: "Creating a folder",
  file_delete: "Moving to Recycle Bin", file_find_duplicates: "Finding duplicates", system_overview: "Checking the system",
  process_list: "Listing programs", process_end: "Ending a program", network_diagnose: "Checking the network", services_list: "Checking services",
  system_errors: "Reading system errors", startup_apps: "Checking startup apps", dev_environment: "Checking dev tools",
  web_search: "Searching the web", web_fetch: "Reading a web page", plan_update: "Updating the plan", ask_owner: "Waiting for you",
  memory_remember: "Saving to memory", memory_search: "Checking memory", memory_forget: "Removing a memory",
};

async function api(path, options = {}) {
  const response = await fetch("/api" + path, {
    ...options,
    headers: { "content-type": "application/json", "x-sudo-token": token, ...(options.headers || {}) },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : "Request failed");
  return body;
}
const post = (path, data) => api(path, { method: "POST", body: JSON.stringify(data ?? {}) });

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

// ---- Views -------------------------------------------------------------------
const loaders = { tasks: loadTasks, memory: loadMemory, settings: loadSettings };
for (const tab of document.querySelectorAll(".tab")) {
  tab.addEventListener("click", () => {
    for (const other of document.querySelectorAll(".tab")) other.removeAttribute("aria-current");
    tab.setAttribute("aria-current", "page");
    for (const view of document.querySelectorAll(".view")) view.hidden = view.id !== "view-" + tab.dataset.view;
    loaders[tab.dataset.view]?.();
  });
}

// ---- Conversation ------------------------------------------------------------
const log = $("log");
let streamNode = null;

function addToLog(node) {
  $("empty")?.remove();
  const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 120;
  log.append(node);
  if (atBottom) log.scrollTop = log.scrollHeight;
}
function addMessage(kind, text) { addToLog(el("div", "msg " + kind, text)); }

function streamText(text) {
  if (!streamNode) { streamNode = el("div", "msg assistant", ""); addToLog(streamNode); }
  streamNode.textContent += text;
  log.scrollTop = log.scrollHeight;
}

const BADGE = { verified: "VERIFIED", unverified: "NOT VERIFIED", failed: "FAILED", stopped: "STOPPED", blocked: "BLOCKED" };
function addResult(event) {
  streamNode = null;
  if (event.status === "answered") return;
  const key = event.verdict || event.status;
  const box = el("div", "msg result " + key);
  box.append(el("div", "badge " + key, BADGE[key] || key.toUpperCase()));
  box.append(el("div", "", event.summary));
  if (event.checks?.length) {
    const list = el("ul", "checks");
    for (const check of event.checks) {
      list.append(el("li", "", (check.passed === true ? "pass  " : check.passed === false ? "FAIL  " : "note  ") + check.label));
    }
    box.append(list);
  }
  addToLog(box);
}

// ---- Status ------------------------------------------------------------------
const phaseList = $("phases");
for (const name of PHASES) phaseList.append(el("li", "", name));
function setPhase(name) {
  const index = PHASES.indexOf(name);
  [...phaseList.children].forEach((li, i) => { li.className = i < index ? "past" : i === index ? "on" : ""; });
  if (index >= 0) $("phase-label").textContent = name[0] + name.slice(1).toLowerCase();
}

const AGENTS = ["Supervisor", "Computer", "Browser", "Terminal", "Files", "Research", "Verification"];
const AGENT_OF_GROUP = { supervisor: "Supervisor", computer: "Computer", browser: "Browser", terminal: "Terminal", system: "Terminal", files: "Files", research: "Research", verification: "Verification" };
const agentList = $("agents");
for (const name of AGENTS) agentList.append(el("li", "", name));
function setAgent(name) { [...agentList.children].forEach((li) => { li.className = li.textContent === name ? "on" : ""; }); }

let running = false, paused = false;
function refreshControls() {
  $("stop").disabled = !running;
  $("pause").disabled = !running;
  $("pause").textContent = paused ? "Resume" : "Pause";
  $("mode-box").hidden = !running;
  $("dot").className = "dot" + (running ? (paused ? " wait" : " run") : "");
  $("send").textContent = running ? "Send" : "Run";
}
function setRunning(value, label) {
  running = value;
  if (!value) paused = false;
  $("status").textContent = label;
  refreshControls();
}

function addAction(event) {
  const row = el("li", "");
  row.dataset.tool = event.tool;
  const label = el("span", "label");
  label.append(el("b", "", FRIENDLY[event.tool] || event.tool));
  const summary = event.args && (event.args.url || event.args.command || event.args.path || event.args.name || event.args.keys || event.args.text);
  if (typeof summary === "string") { row.title = summary; label.append(el("small", "", summary)); }
  row.append(el("span", "mark-s", "·"), label, el("span", "ms", ""));
  $("actions").prepend(row);
  while ($("actions").children.length > 120) $("actions").lastChild.remove();
  return row;
}

let openRow = null;
function handle(event) {
  switch (event.type) {
    case "task_started":
      $("actions").replaceChildren(); $("plan").replaceChildren(); $("plan-panel").hidden = true;
      streamNode = null; setRunning(true, event.objective); $("now").textContent = ""; break;
    case "phase": setPhase(event.name); setAgent(event.name === "VERIFY" ? "Verification" : "Supervisor"); break;
    case "text_delta": streamText(event.text); break;
    case "assistant_text": streamNode = null; break;
    case "tool_start":
      setPhase(event.phase); setAgent(AGENT_OF_GROUP[event.group]); $("now").textContent = FRIENDLY[event.tool] || event.tool; openRow = addAction(event); break;
    case "tool_end":
      if (openRow?.dataset.tool === event.tool) {
        openRow.children[0].textContent = event.ok ? "✓" : "✕";
        openRow.children[0].className = event.ok ? "ok" : "no";
        openRow.children[2].textContent = (event.ms / 1000).toFixed(1) + "s";
        if (!event.ok && event.result) openRow.title = event.result;
      }
      break;
    case "screenshot": showScreen(event.image); break;
    case "plan":
      $("plan-panel").hidden = false;
      $("plan").replaceChildren(...event.steps.map((s) => el("li", s.status, s.text)));
      break;
    case "prompt": queuePrompt(event); break;
    case "paused": paused = true; $("now").textContent = "Paused"; refreshControls(); break;
    case "resumed": paused = false; $("now").textContent = ""; refreshControls(); break;
    case "instruction_received": addMessage("system", "SUDO picked up your update."); break;
    case "stop_requested": $("now").textContent = "Stopping…"; dropPrompts(); break;
    case "task_finished":
      if (event.status !== "queued") { setRunning(false, "Idle"); $("now").textContent = ""; setPhase("REPORT"); $("phase-label").textContent = "Finished"; setAgent(""); addResult(event); dropPrompts(); }
      break;
    case "auto_resume_pending": addMessage("system", `SUDO closed unexpectedly. It will carry on with ${event.tasks.length} unfinished task(s) in ${event.seconds} seconds. Type stop to prevent that.`); break;
    case "auto_resume_cancelled": addMessage("system", "It won't carry on by itself. You can resume from Tasks."); break;
    case "hotkey_unavailable": addMessage("system", event.detail); break;
    case "memory_changed": if (!$("view-memory").hidden) loadMemory(); break;
    case "extension_link": if (!$("view-settings").hidden) loadSettings(); break;
  }
}

let restored = false;
async function restoreConversation() {
  if (restored) return;
  restored = true;
  for (const row of await api("/conversation")) {
    addMessage("owner", row.objective);
    if (row.status === "answered") addMessage("assistant", row.summary);
    else if (row.summary && row.status !== "queued") addResult(row);
  }
  log.scrollTop = log.scrollHeight;
}

function showScreen(src) { $("screen").src = src; $("screen").hidden = false; $("screen-empty").hidden = true; }

// ---- Owner prompts -----------------------------------------------------------
const promptQueue = [];
let activePrompt = null;
let priorFocus = null;

function queuePrompt(prompt) { promptQueue.push(prompt); if (!activePrompt) nextPrompt(); }
function dropPrompts() { promptQueue.length = 0; closePrompt(); }
function closePrompt() { activePrompt = null; $("scrim").hidden = true; $("d-input").value = ""; priorFocus?.focus?.(); }

const ARG_LABELS = { path: "File", url: "Address", command: "Command", text: "Text", src: "From", dst: "To", name: "Name", title: "Window", pid: "Program ID", keys: "Keys", key: "Key", cwd: "Folder", content: "Content", mode: "Mode", index: "Tab" };
function describeApproval(payload) {
  const rows = [];
  for (const [key, value] of Object.entries(payload.args || {})) {
    if (key === "ref" || (typeof value === "string" && (payload.reason || "").includes(value))) continue;
    rows.push(el("dt", "", ARG_LABELS[key] || key), el("dd", "", typeof value === "string" ? value : JSON.stringify(value)));
  }
  return rows;
}

function nextPrompt() {
  const prompt = promptQueue.shift();
  if (!prompt) return closePrompt();
  activePrompt = prompt;
  priorFocus = document.activeElement;
  const input = $("d-input"), detail = $("d-detail");
  input.hidden = true; input.required = false; input.type = "text"; detail.hidden = true;
  if (prompt.kind === "approval") {
    $("d-kind").textContent = "APPROVAL NEEDED";
    $("d-title").textContent = FRIENDLY[prompt.payload.tool] || prompt.payload.tool;
    $("d-body").textContent = prompt.payload.reason;
    const rows = describeApproval(prompt.payload);
    detail.replaceChildren(...rows); detail.hidden = !rows.length;
    $("d-no").textContent = "Decline"; $("d-yes").textContent = "Approve";
  } else if (prompt.kind === "secret") {
    $("d-kind").textContent = "ONLY YOU CAN ENTER THIS";
    $("d-title").textContent = prompt.payload.label;
    $("d-body").textContent = "This goes straight into the field. The AI model never sees it and it is not logged.";
    input.hidden = false; input.required = true; input.type = "password";
    $("d-no").textContent = "Cancel"; $("d-yes").textContent = "Enter it";
  } else {
    $("d-kind").textContent = "SUDO NEEDS YOU";
    $("d-title").textContent = prompt.payload.question;
    $("d-body").textContent = "";
    input.hidden = false; input.required = true;
    $("d-no").textContent = "Skip"; $("d-yes").textContent = "Reply";
  }
  $("scrim").hidden = false;
  (input.hidden ? $("d-no") : input).focus();  // Declining is the safe default for Enter.
}

async function answerPrompt(value) {
  const prompt = activePrompt;
  if (!prompt) return;
  try { await post(`/prompts/${prompt.id}`, { value }); } catch {}
  nextPrompt();
}
$("d-form").addEventListener("submit", (e) => {
  e.preventDefault();
  answerPrompt(activePrompt?.kind === "approval" ? true : $("d-input").value);
});
$("d-no").addEventListener("click", () => answerPrompt(activePrompt?.kind === "approval" ? false : ""));
$("scrim").addEventListener("keydown", (e) => {
  if (e.key === "Escape") { e.preventDefault(); $("d-no").click(); return; }
  if (e.key !== "Tab") return;
  const items = [...$("scrim").querySelectorAll("button, input:not([hidden])")];
  const first = items[0], last = items[items.length - 1];
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
});

// ---- Layout helpers ----------------------------------------------------------
function setSide(open) {
  $("side").classList.toggle("open", open);
  $("side-toggle").setAttribute("aria-expanded", String(open));
}
$("side-toggle").addEventListener("click", () => setSide(!$("side").classList.contains("open")));
$("side-close").addEventListener("click", () => setSide(false));
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && $("scrim").hidden) setSide(false); });
$("suggestions").addEventListener("click", (e) => {
  const button = e.target.closest(".suggestion");
  if (!button) return;
  input.value = button.textContent; grow(); input.focus();
});

function setLink(state) { $("link").textContent = state; $("link").dataset.state = state; }

// ---- Commands ----------------------------------------------------------------
const input = $("input");
function grow() { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight + 2, 180) + "px"; }
input.addEventListener("input", grow);
input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $("composer").requestSubmit(); }
});
$("composer").addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  addMessage("owner", text);
  input.value = ""; grow();
  try { await post("/command", { text, mode: running ? $("mode").value : "auto" }); $("mode").value = "auto"; }
  catch (err) { addMessage("system", "Could not send that: " + err.message); }
});
$("stop").addEventListener("click", () => post("/stop").catch(() => {}));
$("pause").addEventListener("click", () => post(paused ? "/resume" : "/pause").catch(() => {}));
$("refresh-screen").addEventListener("click", async () => {
  try { showScreen((await api("/screen")).image); } catch (err) { $("screen-empty").textContent = err.message; }
});
$("voice").addEventListener("click", async () => {
  const button = $("voice");
  if (button.getAttribute("aria-pressed") === "true") return;
  button.setAttribute("aria-pressed", "true"); button.textContent = "Listening…";
  try {
    const { text } = await post("/voice");
    if (text) { input.value = (input.value ? input.value + " " : "") + text; grow(); input.focus(); }
    else addMessage("system", "Didn't catch anything. Try again a little closer to the microphone.");
  } catch (err) { addMessage("system", err.message); }
  button.setAttribute("aria-pressed", "false"); button.textContent = "Voice";
});

// ---- Task history ------------------------------------------------------------
async function loadTasks() {
  const rows = await api("/tasks");
  const list = $("task-list");
  list.replaceChildren();
  if (!rows.length) list.append(el("p", "sub", "No tasks yet."));
  for (const row of rows) {
    const item = el("div", "item"), info = el("div");
    const state = row.verdict || row.status;
    info.append(el("span", "pill " + state, state[0].toUpperCase() + state.slice(1)));
    info.append(el("div", "item-title", row.objective));
    info.append(el("small", "", new Date(row.created * 1000).toLocaleString()));
    if (row.summary) info.append(el("small", "", row.summary));
    const actions = el("div", "item-actions");
    const steps = el("button", "link", "Steps");
    steps.addEventListener("click", async () => {
      const task = await api(`/tasks/${row.id}`);
      info.querySelector(".steps")?.remove();
      info.append(el("div", "steps", task.steps.map((s) => `${s.ok ? "ok " : "ERR"} ${s.tool} (${s.ms} ms)`).join("\n") || "No actions recorded."));
    });
    actions.append(steps);
    if (["interrupted", "stopped", "failed"].includes(row.status)) {
      const again = el("button", "link", "Resume");
      again.addEventListener("click", () => post(`/tasks/${row.id}/resume`).then(() => document.querySelector('[data-view="operate"]').click()));
      actions.append(again);
    }
    const remove = el("button", "link", "Delete");
    remove.addEventListener("click", () => api(`/tasks/${row.id}`, { method: "DELETE" }).then(loadTasks));
    actions.append(remove);
    item.append(info, actions);
    list.append(item);
  }
}

// ---- Memory ------------------------------------------------------------------
async function loadMemory() {
  const { items, kinds } = await api("/memories");
  if (!$("m-kind").options.length) $("m-kind").append(...kinds.map((k) => { const o = el("option", "", k); o.value = k; return o; }));
  const list = $("memory-list");
  list.replaceChildren();
  if (!items.length) list.append(el("p", "sub", "Nothing stored yet."));
  for (const row of items) {
    const item = el("div", "item"), info = el("div");
    info.append(el("span", "pill", row.kind.replace("_", " ")), el("div", "item-title", row.title), el("small", "", row.content));
    const actions = el("div", "item-actions");
    const edit = el("button", "link", "Edit");
    edit.addEventListener("click", () => {
      const field = document.createElement("input");
      field.value = row.content; field.setAttribute("aria-label", "New content"); field.style.width = "100%";
      info.replaceChildren(field); field.focus();
      field.addEventListener("keydown", async (e) => {
        if (e.key === "Escape") return loadMemory();
        if (e.key !== "Enter") return;
        try { await api(`/memories/${row.id}`, { method: "PATCH", body: JSON.stringify({ content: field.value }) }); $("memory-error").textContent = ""; loadMemory(); }
        catch (err) { $("memory-error").textContent = err.message; }
      });
    });
    const remove = el("button", "link", "Delete");
    remove.addEventListener("click", () => api(`/memories/${row.id}`, { method: "DELETE" }).then(loadMemory));
    actions.append(edit, remove);
    item.append(info, actions);
    list.append(item);
  }
}
$("memory-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await post("/memories", { kind: $("m-kind").value, title: $("m-title").value, content: $("m-content").value });
    $("m-title").value = $("m-content").value = ""; $("memory-error").textContent = ""; loadMemory();
  } catch (err) { $("memory-error").textContent = err.message; }
});

// ---- Settings ----------------------------------------------------------------
let currentSettings = null;
function showKeyState(view) { $("key-state").textContent = view.keys[view.provider] ? "A key is saved." : "No key saved yet."; }
async function loadSettings() {
  currentSettings = await api("/settings");
  const form = $("settings-form");
  buildPermissions();
  for (const field of form.elements) {
    if (!field.name) continue;
    if (field.name.startsWith("perm:")) field.value = currentSettings.permissions[field.name.slice(5)];
    else if (field.type === "checkbox") field.checked = !!currentSettings[field.name];
    else field.value = currentSettings[field.name];
  }
  for (const option of form.elements.browser.options) option.disabled = !currentSettings.installed_browsers[option.value];
  showKeyState(currentSettings);
  const link = currentSettings.extension;
  $("ext-state").textContent = link.connected ? "Connected: SUDO can use your own browser." : "Not connected yet.";
  $("ext-folder").textContent = link.folder;
  $("ext-port").textContent = link.port;
  $("ext-code").value = link.code;
  api("/models").then(({ models }) => {
    $("model-list").replaceChildren(...models.map((id) => { const o = document.createElement("option"); o.value = id; return o; }));
  }).catch(() => {});
}

const PERMISSION_LABELS = { computer: "Computer (mouse, keyboard, apps)", browser: "Browser", terminal: "Terminal", files: "Files", research: "Research", system: "System (monitoring, ending programs)" };
function buildPermissions() {
  const box = $("permissions");
  if (box.querySelector("select")) return;
  for (const [group, label] of Object.entries(PERMISSION_LABELS)) {
    const row = el("label", "", label), select = document.createElement("select");
    select.name = "perm:" + group;
    for (const [value, text] of [["auto", "Normal"], ["ask", "Ask every time"], ["off", "Off"]]) {
      const option = el("option", "", text); option.value = value; select.append(option);
    }
    row.append(select);
    box.append(row);
  }
}
$("settings-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const values = { permissions: {} };
  for (const field of e.target.elements) {
    if (!field.name) continue;
    if (field.name.startsWith("perm:")) values.permissions[field.name.slice(5)] = field.value;
    else values[field.name] = field.type === "checkbox" ? field.checked : field.type === "number" ? Number(field.value) : field.value;
  }
  try { await api("/settings", { method: "PUT", body: JSON.stringify(values) }); await loadSettings(); $("settings-note").textContent = "Saved."; }
  catch (err) { $("settings-note").textContent = err.message; }
});
$("key-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!$("key").value.trim()) return;
  try { await api(`/keys/${currentSettings.provider}`, { method: "PUT", body: JSON.stringify({ value: $("key").value }) }); $("key").value = ""; await loadSettings(); }
  catch (err) { $("key-state").textContent = err.message; }
});
$("ext-copy").addEventListener("click", async () => {
  const field = $("ext-code");
  try { await navigator.clipboard.writeText(field.value); }
  catch { field.select(); document.execCommand("copy"); }
  $("ext-copied").textContent = "Copied.";
});
$("key-delete").addEventListener("click", async () => {
  currentSettings = await api(`/keys/${currentSettings.provider}`, { method: "DELETE" }); showKeyState(currentSettings);
});

// ---- Connection --------------------------------------------------------------
let retry = 500;
function connect() {
  const socket = new WebSocket(`ws://${location.host}/ws?token=${encodeURIComponent(token)}`);
  socket.onopen = async () => {
    retry = 500;
    try {
      await restoreConversation();
      const state = await api("/state");
      if (state.current) {
        setRunning(true, state.current.objective);
        paused = state.current.paused; refreshControls();
        promptQueue.length = 0;
        for (const prompt of state.current.prompts) if (prompt.id !== activePrompt?.id) queuePrompt(prompt);
      } else setRunning(false, "Idle");
    } catch {}
    setLink("connected");
  };
  socket.onmessage = (message) => handle(JSON.parse(message.data));
  socket.onclose = () => { setLink("reconnecting"); setTimeout(connect, retry); retry = Math.min(retry * 2, 5000); };
}
refreshControls();
connect();
