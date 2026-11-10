"use strict";
const $ = (id) => document.getElementById(id);

const MESSAGES = {
  connected: "Connected to SUDO.",
  connecting: "Connecting…",
  "wrong-code": "Wrong pairing code. Press Copy in SUDO's Settings and paste it here again.",
  unreachable: "Can't reach SUDO on this port. Is SUDO open, and does the port match its Settings?",
  "no-code": "Paste the pairing code from SUDO's Settings.",
};

async function refresh() {
  const { status } = await chrome.storage.session.get("status");
  $("state").textContent = MESSAGES[status] || MESSAGES["no-code"];
}

chrome.storage.local.get(["code", "port"]).then(({ code, port }) => {
  if (code) $("code").value = code;
  if (port) $("port").value = port;
});

$("form").addEventListener("submit", async (event) => {
  event.preventDefault();
  await chrome.storage.local.set({ code: $("code").value.replace(/\s/g, ""), port: Number($("port").value) });
  $("code").value = $("code").value.replace(/\s/g, "");
  chrome.runtime.sendMessage({ connect: true });
});

chrome.storage.session.onChanged.addListener(refresh);
refresh();
