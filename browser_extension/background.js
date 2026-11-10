"use strict";
// Carries commands from the local SUDO app to this browser. It connects only to 127.0.0.1.
// Safety decisions are made in SUDO before a command ever arrives here.

let socket = null;
let targetTab = null;
// Focus mode: { tabId, frameId, area }. While set, SUDO is shown only that part of that page.
let scope = null;
const attached = new Set();
const detachTimers = new Map();
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// ---- Connection --------------------------------------------------------------
const setStatus = (status) => chrome.storage.session.set({ status });

async function connect(force = false) {
  if (socket && socket.readyState <= WebSocket.OPEN) {
    if (!force) return;
    const previous = socket;
    socket = null;
    previous.close();
  }
  const { code, port = 8765 } = await chrome.storage.local.get(["code", "port"]);
  if (!code) { setStatus("no-code"); return; }
  setStatus("connecting");
  const ws = new WebSocket(`ws://127.0.0.1:${port}/ext?code=${encodeURIComponent(code)}`);
  socket = ws;
  ws.onmessage = (event) => {
    const message = JSON.parse(event.data);
    if (message.hello) setStatus("connected");
    else onCommand(message);
  };
  ws.onclose = (event) => {
    if (socket !== ws) return;  // Replaced by a newer attempt.
    socket = null;
    setStatus(event.code === 4403 ? "wrong-code" : "unreachable");
  };
}

chrome.runtime.onInstalled.addListener(() => connect());
chrome.runtime.onStartup.addListener(() => connect());
chrome.alarms.create("reconnect", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(() => connect());
chrome.runtime.onMessage.addListener((message) => { if (message.connect) connect(true); });
setInterval(() => { if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ ping: true })); }, 20000);
connect();

async function onCommand({ id, cmd, args }) {
  let reply;
  try {
    scope = (await chrome.storage.session.get("scope")).scope || null;
    reply = { id, result: await COMMANDS[cmd](args || {}) };
  } catch (error) {
    reply = { id, error: String(error?.message || error) };
  }
  if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(reply));
}

// ---- Tabs and page access ----------------------------------------------------
// Follow the tab the owner is looking at, so "this page" always means what they see.
chrome.tabs.onActivated.addListener(({ tabId }) => { targetTab = tabId; });
chrome.windows.onFocusChanged.addListener(async (windowId) => {
  if (windowId === chrome.windows.WINDOW_ID_NONE) return;
  const [tab] = await chrome.tabs.query({ active: true, windowId }).catch(() => []);
  if (tab) targetTab = tab.id;
});

// Tabs SUDO may be told about: ones it has worked in, opened, or the owner named.
async function knownTabs() {
  return new Set((await chrome.storage.session.get("known")).known || []);
}
async function remember(...ids) {
  const known = await knownTabs();
  ids.forEach((id) => known.add(id));
  await chrome.storage.session.set({ known: [...known] });
}

async function setScope(value) {
  scope = value;
  await chrome.storage.session.set({ scope: value });
}

async function currentTab() {
  let tab = null;
  if (scope) {
    try { tab = await chrome.tabs.get(scope.tabId); targetTab = tab.id; } catch { await setScope(null); }
  }
  if (!tab && targetTab !== null) {
    try { tab = await chrome.tabs.get(targetTab); } catch { targetTab = null; }
  }
  if (!tab) {
    [tab] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
    if (!tab) throw new Error("No open tab was found.");
    targetTab = tab.id;
  }
  await remember(tab.id);
  return tab;
}

// Refs from the top page look like "e5"; refs inside an embedded frame look like "f12e5".
function splitRef(ref) {
  const match = /^f(\d+)(e\d+)$/.exec(ref || "");
  return match ? { frameId: Number(match[1]), local: match[2] } : { frameId: 0, local: ref };
}

// Shared helpers, installed once per frame in the extension's own isolated world.
function installHelpers() {
  if (window.__sudo?.version === 2) return;
  const deep = (root, sel, acc = []) => {
    acc.push(...root.querySelectorAll(sel));
    for (const el of root.querySelectorAll("*")) if (el.shadowRoot) deep(el.shadowRoot, sel, acc);
    return acc;
  };
  const area = () => deep(document, "[data-sudo-scope]")[0] || null;
  const inside = (root, el) => {
    for (let node = el; node; node = node.parentNode || node.host) if (node === root) return true;
    return false;
  };
  const find = (ref) => {
    const el = deep(document, `[data-sudo-ref="${ref}"]`)[0] || null;
    const root = area();
    return el && root && !inside(root, el) ? null : el;
  };
  window.__sudo = { version: 2, deep, find, area, inside };
}

async function inFrames(tabId, frameIds, func, args = []) {
  const target = frameIds ? { tabId, frameIds } : { tabId, allFrames: true };
  await chrome.scripting.executeScript({ target, func: installHelpers }).catch(() => {});
  return chrome.scripting.executeScript({ target, func, args });
}

async function inFrame(tabId, frameId, func, ...args) {
  const [frame] = await inFrames(tabId, [frameId], func, args);
  return frame.result;
}

async function inPage(func, ...args) {
  return inFrame((await currentTab()).id, 0, func, ...args);
}

function waitForLoad(tabId, timeout = 30000) {
  return new Promise((resolve) => {
    const finish = () => {
      chrome.tabs.onUpdated.removeListener(listener);
      clearTimeout(timer);
      resolve();
    };
    const listener = (id, info) => { if (id === tabId && info.status === "complete") finish(); };
    const timer = setTimeout(finish, timeout);
    chrome.tabs.onUpdated.addListener(listener);
    chrome.tabs.get(tabId).then((tab) => { if (tab.status === "complete") finish(); }).catch(finish);
  });
}

const line = (tab) => (scope ? { title: "(hidden: focus mode)", url: "(hidden)" } : { title: tab.title, url: tab.url });

// ---- Trusted input through the debugger ---------------------------------------
async function debug(tabId, method, params = {}, retry = true) {
  if (!attached.has(tabId)) {
    try { await chrome.debugger.attach({ tabId }, "1.3"); } catch (error) {
      if (!/already attached/i.test(String(error?.message || error))) throw error;
    }
    attached.add(tabId);
  }
  clearTimeout(detachTimers.get(tabId));
  // onDetach does not fire when we detach ourselves, so forget the tab here too.
  detachTimers.set(tabId, setTimeout(() => { attached.delete(tabId); chrome.debugger.detach({ tabId }).catch(() => {}); }, 20000));
  try {
    return await chrome.debugger.sendCommand({ tabId }, method, params);
  } catch (error) {
    if (!retry || !/not attached/i.test(String(error?.message || error))) throw error;
    attached.delete(tabId);
    return debug(tabId, method, params, false);
  }
}
chrome.debugger.onDetach.addListener(({ tabId }) => attached.delete(tabId));

async function activate(tab) {
  await chrome.tabs.update(tab.id, { active: true });
  await chrome.windows.update(tab.windowId, { focused: true });
}

async function clickAt(tab, point) {
  const base = { x: point.x, y: point.y, button: "left", clickCount: 1 };
  await debug(tab.id, "Input.dispatchMouseEvent", { type: "mouseMoved", x: point.x, y: point.y });
  await debug(tab.id, "Input.dispatchMouseEvent", { type: "mousePressed", ...base });
  await debug(tab.id, "Input.dispatchMouseEvent", { type: "mouseReleased", ...base });
}

const KEYS = {
  Enter: [13, "\r"], Tab: [9], Escape: [27], Backspace: [8], Delete: [46], Home: [36], End: [35], PageUp: [33], PageDown: [34],
  ArrowLeft: [37], ArrowUp: [38], ArrowRight: [39], ArrowDown: [40],
};
const MODIFIERS = { Alt: 1, Control: 2, Ctrl: 2, Meta: 4, Shift: 8 };

async function pressKey(tab, combo) {
  const parts = combo.split("+");
  const name = parts.pop();
  const flags = parts.reduce((sum, part) => sum + (MODIFIERS[part] || 0), 0);
  let vk, text;
  if (KEYS[name]) [vk, text] = KEYS[name];
  else if (name.length === 1) { vk = name.toUpperCase().charCodeAt(0); text = flags & 6 ? undefined : name; }
  else throw new Error(`Unknown key: ${name}`);
  const event = { key: name, windowsVirtualKeyCode: vk, nativeVirtualKeyCode: vk, modifiers: flags };
  const commands = flags & 2 && name.toLowerCase() === "a" ? ["selectAll"] : undefined;
  await debug(tab.id, "Input.dispatchKeyEvent", { type: text ? "keyDown" : "rawKeyDown", ...event, text, commands });
  await debug(tab.id, "Input.dispatchKeyEvent", { type: "keyUp", ...event });
}

// ---- Functions that run inside the page (in every frame, shadow DOM included) -----
function pageSnapshot(scoped) {
  const root = scoped ? window.__sudo.area() : document;
  if (!root) return { items: [], captcha: false };
  const sel = 'a[href],button,input,select,textarea,summary,[role=button],[role=link],[role=textbox],[role=combobox],' +
    '[role=checkbox],[role=radio],[role=tab],[role=menuitem],[role=option],[role=searchbox],[contenteditable=""],[contenteditable=true],[onclick]';
  window.__sudoN = window.__sudoN || 0;
  const out = [];
  for (const el of window.__sudo.deep(root, sel)) {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    if (r.width <= 0 || r.height <= 0 || s.visibility === "hidden" || s.display === "none") continue;
    if (!el.dataset.sudoRef) el.dataset.sudoRef = "e" + (++window.__sudoN);
    const labels = el.labels ? [...el.labels].map((l) => l.innerText).join(" ") : "";
    const name = (el.getAttribute("aria-label") || labels || el.innerText || el.getAttribute("title") || el.getAttribute("alt") ||
      el.getAttribute("name") || "").trim().replace(/\s+/g, " ").slice(0, 90);
    const type = el.getAttribute("type") || "";
    const sensitive = type === "password" || /^cc-|one-time-code/.test(el.getAttribute("autocomplete") || "");
    const value = sensitive ? "" : ("value" in el ? String(el.value) : (el.isContentEditable ? el.innerText : ""));
    out.push({ ref: el.dataset.sudoRef, tag: el.tagName.toLowerCase(), type, role: el.getAttribute("role") || "", name, value: value.slice(0, 120),
      placeholder: el.getAttribute("placeholder") || "", disabled: !!el.disabled, checked: el.checked === true, sensitive, inView: r.bottom > 0 && r.top < innerHeight });
  }
  const frames = window.__sudo.deep(document, "iframe").map((f) => f.src).join(" ");
  const shown = innerWidth > 0 && innerHeight > 0;
  return { items: shown ? out : [], captcha: /recaptcha|hcaptcha|turnstile|cf-chl|captcha/i.test(frames) ||
    (window !== top && /recaptcha|hcaptcha|turnstile|challenges\.cloudflare/i.test(location.href)) };
}

function pageText(scoped) {
  const root = scoped ? window.__sudo.area() : document.body;
  return { url: scoped ? "" : location.href, text: root ? root.innerText : "" };
}

// Finds the part of this frame that best fits the owner's words and remembers it as a candidate.
function pageScopeFind(words) {
  const S = window.__sudo;
  S.deep(document, "[data-sudo-scope]").forEach((el) => el.removeAttribute("data-sudo-scope"));
  const visible = (el) => {
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== "hidden" && s.display !== "none";
  };
  const fields = S.deep(document, "input:not([type=hidden]),select,textarea,[contenteditable=true],[role=textbox]").filter(visible);
  const count = (el) => fields.filter((f) => S.inside(el, f)).length;
  const label = (el) => (el.getAttribute("aria-label") || el.querySelector("h1,h2,h3,legend")?.innerText || el.id || el.tagName.toLowerCase()).trim().slice(0, 60);
  const done = (el, score) => { window.__sudoCandidate = el; return { score, fields: count(el), label: label(el), where: location.origin + location.pathname }; };
  const candidates = S.deep(document, "form,[role=form],fieldset,dialog,[role=dialog],section,article,main").filter(visible).map((el) => {
    const text = `${el.getAttribute("aria-label") || ""} ${el.id} ${el.className} ${el.getAttribute("name") || ""} ${(el.innerText || "").slice(0, 600)}`.toLowerCase();
    return { el, hits: words.filter((w) => text.includes(w)).length, fields: count(el), size: (el.innerText || "").length };
  });
  const most = Math.max(0, ...candidates.map((c) => c.hits));
  if (most > 0) {
    let pool = candidates.filter((c) => c.hits === most);
    if (pool.some((c) => c.fields)) pool = pool.filter((c) => c.fields);
    pool.sort((a, b) => a.size - b.size);  // The smallest matching part, so nothing extra comes with it.
    return done(pool[0].el, 100 * most + (pool[0].fields ? 10 : 0) + (pool[0].el.tagName === "FORM" ? 3 : 0));
  }
  const forms = candidates.filter((c) => c.fields && /^(FORM|FIELDSET)$/.test(c.el.tagName)).sort((a, b) => b.fields - a.fields);
  if (forms.length) return done(forms[0].el, 20 + Math.min(forms[0].fields, 9));
  if (fields.length) {  // No form tag: the smallest box that holds every field.
    let box = fields[0];
    while (box && !fields.every((f) => S.inside(box, f))) box = box.parentElement || box.getRootNode().host;
    const whole = !box || box === document.body || box === document.documentElement;
    if (box && !(whole && window === top)) return done(whole ? document.body : box, window === top ? 5 : 10);
  }
  window.__sudoCandidate = null;
  return { score: 0, fields: 0, label: "" };
}

function pageScopeApply(keep) {
  if (keep && window.__sudoCandidate) window.__sudoCandidate.setAttribute("data-sudo-scope", "1");
  window.__sudoCandidate = null;
}

function pageScopeClear() {
  window.__sudo.deep(document, "[data-sudo-scope]").forEach((el) => el.removeAttribute("data-sudo-scope"));
}

function pageScopeBox() {
  const el = window.__sudo.area();
  if (!el) return null;
  el.scrollIntoView({ block: "nearest", inline: "nearest", behavior: "instant" });
  const r = el.getBoundingClientRect();
  return { x: r.left, y: r.top, w: r.width, h: r.height };
}

function pageDescribe(ref) {
  let el = ref ? window.__sudo.find(ref) : document.activeElement;
  if (ref && !el) throw new Error(`Element ${ref} is gone. Take a new snapshot.`);
  if (!ref) {
    if (!document.hasFocus()) return null;
    while (el && el.shadowRoot && el.shadowRoot.activeElement) el = el.shadowRoot.activeElement;
    if (!el || el === document.body || /^(IFRAME|FRAME)$/.test(el.tagName)) return null;
  }
  const form = el.form || el.closest("form");
  const type = (el.getAttribute("type") || "").toLowerCase();
  const fields = form ? [...form.querySelectorAll("input,textarea")] : [];
  return { name: (el.getAttribute("aria-label") || el.innerText || el.value || el.getAttribute("title") || "").trim().slice(0, 120),
    tag: el.tagName.toLowerCase(), type, editable: el.isContentEditable, inForm: !!form,
    searchForm: !!form && (form.getAttribute("role") === "search" || /search/i.test(form.getAttribute("action") || "") ||
      fields.some((f) => f.type === "search" || /^(q|query|search|s)$/i.test(f.name || ""))),
    submits: !!form && (type === "submit" || (el.tagName === "BUTTON" && !type)),
    sensitive: type === "password" || /^cc-|one-time-code/.test(el.getAttribute("autocomplete") || "") };
}

function pagePoint(ref) {
  const el = window.__sudo.find(ref);
  if (!el) throw new Error(`Element ${ref} is gone. Take a new snapshot.`);
  el.scrollIntoView({ block: "center", inline: "center", behavior: "instant" });
  const r = el.getBoundingClientRect();
  return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
}

// Where an embedded frame's content starts, measured in this (its parent) frame.
function frameOffset(url) {
  const frames = window.__sudo.deep(document, "iframe,frame");
  const origin = (u) => { try { return new URL(u, location.href).origin; } catch { return ""; } };
  const el = frames.find((f) => f.src && new URL(f.src, location.href).href === url) ||
    frames.find((f) => f.src && origin(f.src) === origin(url)) || (frames.length === 1 ? frames[0] : null);
  if (!el) return null;
  el.scrollIntoView({ block: "nearest", inline: "nearest", behavior: "instant" });
  const r = el.getBoundingClientRect();
  return { x: r.left + el.clientLeft, y: r.top + el.clientTop };
}

function pageClick(ref) {
  const el = window.__sudo.find(ref);
  if (!el) throw new Error(`Element ${ref} is gone. Take a new snapshot.`);
  el.focus();
  el.click();
}

function pagePrepare(ref, clear) {
  const el = window.__sudo.find(ref);
  el.focus();
  if ("value" in el && el.setSelectionRange) {
    try { el.setSelectionRange(clear ? 0 : el.value.length, el.value.length); } catch { if (clear) el.select(); }
  } else {
    const range = document.createRange();
    range.selectNodeContents(el);
    if (!clear) range.collapse(false);
    const selection = getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  }
}

function pageValue(ref) {
  const el = window.__sudo.find(ref);
  return "value" in el ? String(el.value) : el.innerText;
}

function pageSelect(ref, label) {
  const el = window.__sudo.find(ref);
  const option = [...el.options].find((o) => o.text.trim() === label.trim());
  if (!option) return null;
  el.value = option.value;
  el.dispatchEvent(new Event("input", { bubbles: true }));
  el.dispatchEvent(new Event("change", { bubbles: true }));
  return el.options[el.selectedIndex].text;
}

// ---- Acting on an element, whichever frame it is in ---------------------------------
// The element's centre in top-page coordinates, or null when it cannot be worked out.
// How far a frame's content is shifted from the top page's corner, or null when unknown.
async function frameShift(tab, frameId) {
  const shift = { x: 0, y: 0 };
  let current = frameId;
  while (current !== 0) {
    const info = await chrome.webNavigation.getFrame({ tabId: tab.id, frameId: current });
    if (!info) return null;
    const offset = await inFrame(tab.id, info.parentFrameId, frameOffset, info.url).catch(() => null);
    if (!offset) return null;
    shift.x += offset.x;
    shift.y += offset.y;
    current = info.parentFrameId;
  }
  return shift;
}

async function screenPoint(tab, ref) {
  const { frameId, local } = splitRef(ref);
  const point = await inFrame(tab.id, frameId, pagePoint, local);
  const shift = await frameShift(tab, frameId);
  if (!shift) return null;
  const [width, height] = await inFrame(tab.id, 0, () => [innerWidth, innerHeight]);
  const x = point.x + shift.x, y = point.y + shift.y;
  return x >= 0 && y >= 0 && x < width && y < height ? { x, y } : null;
}

// ---- Focus mode -----------------------------------------------------------------------
const GENERIC = new Set(["the", "form", "page", "section", "part", "area", "only", "this", "that", "box", "see", "look", "and", "for"]);

// `prior` is the focus being found again after the page changed. A loose guess is only accepted on
// the same page as before; anywhere else the owner's words must really match, or nothing is shown.
async function locateScope(tab, area, prior = null) {
  const words = area.toLowerCase().split(/[^a-z0-9]+/).filter((w) => w.length > 2 && !GENERIC.has(w));
  const found = (await inFrames(tab.id, null, pageScopeFind, [words])).filter((f) => f.result?.score > 0)
    .filter((f) => !prior || f.result.score >= 100 || f.result.where === prior.where);
  found.sort((a, b) => b.result.score - a.result.score);
  const best = found[0];
  for (const frame of await chrome.webNavigation.getAllFrames({ tabId: tab.id })) {
    await inFrame(tab.id, frame.frameId, pageScopeApply, !!best && frame.frameId === best.frameId).catch(() => {});
  }
  if (!best) return null;
  await setScope({ tabId: tab.id, frameId: best.frameId, area, where: best.result.where });
  return best.result;
}

// Makes sure the focus area is still marked on the page, finding it again after a page change.
async function ensureScope(tab) {
  if (!scope) return;
  const present = await inFrame(tab.id, scope.frameId, () => !!window.__sudo.area()).catch(() => false);
  if (present) return;
  const area = scope.area;
  if (!(await locateScope(tab, area, scope))) {
    throw new Error(`Focus mode is on for "${area}", but that part is not on the page right now, so nothing is shown. Ask the owner what to do.`);
  }
}

const FOCUS_ONLY = "Focus mode is on: the owner allowed only one part of one page. Ask the owner before anything else.";

async function cropTo(dataUrl, box, scale) {
  const blob = await (await fetch(dataUrl)).blob();
  const full = await createImageBitmap(blob);
  const x = Math.max(0, Math.floor(box.x * scale)), y = Math.max(0, Math.floor(box.y * scale));
  const w = Math.min(full.width - x, Math.ceil(box.w * scale)), h = Math.min(full.height - y, Math.ceil(box.h * scale));
  if (w <= 0 || h <= 0) throw new Error("The focused part is not on screen.");
  const canvas = new OffscreenCanvas(w, h);
  canvas.getContext("2d").drawImage(full, x, y, w, h, 0, 0, w, h);
  const bytes = new Uint8Array(await (await canvas.convertToBlob({ type: "image/jpeg", quality: 0.6 })).arrayBuffer());
  let binary = "";
  for (let i = 0; i < bytes.length; i += 8192) binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
  return btoa(binary);
}

async function pressElement(tab, ref) {
  const point = await screenPoint(tab, ref);
  if (point) await clickAt(tab, point);
  else { const { frameId, local } = splitRef(ref); await inFrame(tab.id, frameId, pageClick, local); }
}

// ---- Commands from SUDO ----------------------------------------------------------
const COMMANDS = {
  // Privacy: SUDO is only told about the tab the owner is on, tabs it opened itself, and tabs
  // the owner named (found with `match`). Every other tab stays hidden; only a count is given.
  async tabs({ action, tab, match, url }) {
    if (scope) throw new Error(FOCUS_ONLY);
    let all = await chrome.tabs.query({});
    let matched = null;
    if (action === "new") {
      const created = await chrome.tabs.create({ url: url || "about:blank" });
      targetTab = created.id;
      await remember(created.id);
      await waitForLoad(created.id);
    } else if (action === "switch" && match) {
      const needle = match.toLowerCase();
      const hits = all.filter((t) => `${t.title} ${t.url}`.toLowerCase().includes(needle));
      await remember(...hits.map((t) => t.id));
      matched = hits.length;
      if (hits.length === 1) { targetTab = hits[0].id; await activate(hits[0]); }
    } else if (action === "switch" || action === "close") {
      const target = all.find((t) => t.id === tab);
      if (!target || !(await knownTabs()).has(tab)) throw new Error(`No tab ${tab} in your list. Use a number from the list, or match.`);
      if (action === "switch") { targetTab = target.id; await activate(target); }
      else { if (targetTab === target.id) targetTab = null; await chrome.tabs.remove(target.id); }
    }
    const current = await currentTab();
    const known = await knownTabs();
    all = await chrome.tabs.query({});
    const alive = all.filter((t) => known.has(t.id));
    await chrome.storage.session.set({ known: alive.map((t) => t.id) });
    return { rows: alive.map((t) => ({ tab: t.id, ...line(t), active: t.id === current.id })), hidden: all.length - alive.length, matched };
  },
  async focus() {
    const tab = await currentTab();
    await activate(tab);
    return line(tab);
  },
  async scope_set({ area }) {
    const tab = await currentTab();
    await setScope(null);
    const found = await locateScope(tab, area);
    if (!found) throw new Error(`Could not find "${area}" on the page. Nothing was shown. Ask the owner where it is.`);
    return found;
  },
  async scope_clear() {
    if (scope) await inFrames(scope.tabId, null, pageScopeClear).catch(() => {});
    await setScope(null);
    return true;
  },
  scope_state() { return { on: !!scope, area: scope?.area || "" }; },
  async open({ url }) {
    if (scope) throw new Error(FOCUS_ONLY);
    const tab = await currentTab();
    await activate(tab);
    await chrome.tabs.update(tab.id, { url });
    await sleep(150);
    await waitForLoad(tab.id);
    return line(await chrome.tabs.get(tab.id));
  },
  async snapshot() {
    const tab = await currentTab();
    await ensureScope(tab);
    const items = [];
    let captcha = false;
    for (const frame of await inFrames(tab.id, scope ? [scope.frameId] : null, pageSnapshot, [!!scope])) {
      if (!frame.result) continue;
      captcha = captcha || frame.result.captcha;
      const prefix = frame.frameId ? `f${frame.frameId}` : "";
      for (const item of frame.result.items) items.push({ ...item, ref: prefix + item.ref, framed: !!frame.frameId });
    }
    return { ...line(tab), items, captcha, focus: scope?.area || "" };
  },
  async read() {
    const tab = await currentTab();
    await ensureScope(tab);
    const parts = [];
    for (const frame of await inFrames(tab.id, scope ? [scope.frameId] : null, pageText, [!!scope])) {
      const text = (frame.result?.text || "").trim();
      if (!text) continue;
      parts.push(frame.frameId && !scope ? `\n[embedded frame ${frame.result.url}]\n${text}` : text);
    }
    return { ...line(tab), text: parts.join("\n"), focus: scope?.area || "" };
  },
  async describe({ ref }) {
    const tab = await currentTab();
    if (ref) { const { frameId, local } = splitRef(ref); return inFrame(tab.id, frameId, pageDescribe, local); }
    const frames = await inFrames(tab.id, null, pageDescribe, [null]);
    return frames.map((f) => f.result).find(Boolean) || null;
  },
  async click({ ref }) {
    const tab = await currentTab();
    await ensureScope(tab);
    await activate(tab);
    await pressElement(tab, ref);
    await sleep(350);
    await waitForLoad(tab.id, 8000);
    return line(await chrome.tabs.get(tab.id));
  },
  async type({ ref, text, clear }) {
    const tab = await currentTab();
    await ensureScope(tab);
    await activate(tab);
    const { frameId, local } = splitRef(ref);
    await pressElement(tab, ref);
    await inFrame(tab.id, frameId, pagePrepare, local, clear);
    if (text) await debug(tab.id, "Input.insertText", { text });
    else if (clear) await pressKey(tab, "Delete");
    return inFrame(tab.id, frameId, pageValue, local);
  },
  async press({ key }) {
    const tab = await currentTab();
    await activate(tab);
    await pressKey(tab, key);
    await sleep(350);
    await waitForLoad(tab.id, 8000);
    return line(await chrome.tabs.get(tab.id));
  },
  select({ ref, label }) {
    return currentTab().then((tab) => { const { frameId, local } = splitRef(ref); return inFrame(tab.id, frameId, pageSelect, local, label); });
  },
  // Some pages keep the top page fixed and scroll inside an embedded frame, so scroll every frame.
  async scroll({ pixels }) {
    const frames = await inFrames((await currentTab()).id, null,
      (dy) => { window.scrollBy(0, dy); return [Math.round(scrollY), document.documentElement.scrollHeight]; }, [pixels]);
    return frames.find((f) => f.frameId === 0).result;
  },
  async screenshot() {
    const tab = await currentTab();
    await activate(tab);
    await sleep(120);
    if (scope) {  // Only the focused part is photographed.
      await ensureScope(tab);
      const box = await inFrame(tab.id, scope.frameId, pageScopeBox);
      const shift = await frameShift(tab, scope.frameId);
      if (!box || !shift) throw new Error("A picture of only the focused part is not possible here. Use my_snapshot and my_read.");
      await sleep(120);
      const scale = await inFrame(tab.id, 0, () => devicePixelRatio);
      const shot = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "png" });
      return { ...line(tab), data: await cropTo(shot, { ...box, x: box.x + shift.x, y: box.y + shift.y }, scale) };
    }
    const url = await chrome.tabs.captureVisibleTab(tab.windowId, { format: "jpeg", quality: 60 });
    return { ...line(tab), data: url.slice(url.indexOf(",") + 1) };
  },
};
