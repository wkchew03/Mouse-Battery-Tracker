// Mouse Battery Tracker - Stream Deck plugin.
//
// Deliberately dependency-free: Node 24 (which Stream Deck ships) has a global
// WebSocket, so there is no npm install, no bundler and no node_modules to keep
// in sync. The whole plugin is this file.
//
// It only ever READS the feed the tray app publishes. All device access and all
// writes stay in the Python app -- two processes polling the same mice would
// contend for the HID handles.

"use strict";

const fs = require("node:fs");
const path = require("node:path");

const FEED_DIR = path.join(
  process.env.APPDATA || process.env.HOME || ".",
  "MouseBatteryTracker",
  "streamdeck"
);
const FEED_FILE = path.join(FEED_DIR, "feed.json");
const ICON_DIR = path.join(FEED_DIR, "icons");

const LOG_FILE = path.join(FEED_DIR, "plugin.log");
const LOG_LIMIT = 200 * 1024;

/** Append a diagnostic line. Stream Deck does not surface plugin stdout. */
function log(...parts) {
  try {
    const line = `${new Date().toISOString()} ${parts.join(" ")}\n`;
    try {
      if (fs.statSync(LOG_FILE).size > LOG_LIMIT) fs.unlinkSync(LOG_FILE);
    } catch {
      // no log yet
    }
    fs.appendFileSync(LOG_FILE, line);
  } catch {
    // logging must never break the plugin
  }
}

const POLL_MS = 3000;
// Past this, the tray app is assumed dead and readings are shown as stale
// rather than presented as current.
const STALE_AFTER_MS = 120000;

/** @type {Map<string, {controller: string, settings: object, index: number}>} */
const contexts = new Map();
let feed = { mice: [], generated_at: 0 };
let socket = null;

// ---------------------------------------------------------------- feed

function readFeed() {
  try {
    const raw = fs.readFileSync(FEED_FILE, "utf8");
    const parsed = JSON.parse(raw);
    if (parsed && Array.isArray(parsed.mice)) return parsed;
  } catch {
    // Missing or half-written: keep the previous snapshot.
  }
  return null;
}

function isStale(snapshot) {
  if (!snapshot || !snapshot.generated_at) return true;
  return Date.now() - snapshot.generated_at * 1000 > STALE_AFTER_MS;
}

function iconFor(mouse) {
  if (!mouse || !mouse.icon) {
    log("iconFor: no mouse or no icon field");
    return null;
  }
  const file = path.join(ICON_DIR, mouse.icon);
  try {
    const buffer = fs.readFileSync(file);
    return `data:image/png;base64,${buffer.toString("base64")}`;
  } catch (error) {
    log("iconFor: FAILED to read", file, String(error));
    return null;
  }
}

/** Pick the mouse a context should show. */
function mouseFor(entry) {
  const mice = feed.mice || [];
  if (mice.length === 0) return null;

  const pinned = entry.settings && entry.settings.key;
  if (pinned) {
    const found = mice.find((m) => m.key === pinned);
    if (found) return found;
  }
  // Unpinned keys follow whichever mouse is connected; dials are scrolled.
  if (entry.controller === "Encoder") {
    return mice[((entry.index % mice.length) + mice.length) % mice.length];
  }
  return mice.find((m) => m.connected) || mice[0];
}

function levelText(mouse) {
  if (!mouse) return "no data";
  if (mouse.percent === null || mouse.percent === undefined) {
    return mouse.charging ? "charging" : "unknown";
  }
  return `${mouse.percent}%`;
}

// ---------------------------------------------------------------- send

function send(payload) {
  if (socket && socket.readyState === 1) socket.send(JSON.stringify(payload));
}

function setTitle(context, title) {
  send({ event: "setTitle", context, payload: { title, target: 0 } });
}

function setImage(context, image) {
  // `state` is included explicitly: the action declares a State in the
  // manifest, and omitting it can leave the declared image in place.
  send({ event: "setImage", context, payload: { image, target: 0, state: 0 } });
}

function setFeedback(context, payload) {
  send({ event: "setFeedback", context, payload });
}

// ---------------------------------------------------------------- render

function renderContext(context, entry) {
  const mouse = mouseFor(entry);
  const stale = isStale(feed);

  if (entry.controller === "Encoder") {
    const bar = mouse && typeof mouse.percent === "number" ? mouse.percent : 0;
    setFeedback(context, {
      title: mouse ? mouse.name : "Mouse Battery",
      value: stale ? "app offline" : levelText(mouse),
      indicator: { value: bar, opacity: stale || !mouse ? 0.4 : 1 },
      icon: iconFor(mouse) || undefined,
    });
    return;
  }

  // Keypad: icon carries the gauge, title carries the number.
  const image = iconFor(mouse);
  log(
    "render keypad",
    `ctx=${context}`,
    `mouse=${mouse ? mouse.name : "none"}`,
    `icon=${mouse ? mouse.icon : "-"}`,
    `imageChars=${image ? image.length : 0}`,
    `stale=${stale}`
  );
  if (image) setImage(context, image);

  if (stale) {
    setTitle(context, "app\noffline");
    return;
  }
  if (!mouse) {
    setTitle(context, "no mice");
    return;
  }
  const suffix = mouse.connected ? "" : `\n${mouse.last_seen_text}`;
  setTitle(context, `${levelText(mouse)}${suffix}`);
}

function renderAll() {
  for (const [context, entry] of contexts) renderContext(context, entry);
}

function poll() {
  const snapshot = readFeed();
  if (snapshot) feed = snapshot;
  renderAll();
}

// ---------------------------------------------------------------- events

function onMessage(raw) {
  let message;
  try {
    message = JSON.parse(raw);
  } catch {
    return;
  }

  const { event, context, payload } = message;
  switch (event) {
    case "willAppear": {
      const controller = (payload && payload.controller) || "Keypad";
      log("willAppear", `ctx=${context}`, `controller=${controller}`);
      contexts.set(context, {
        controller,
        settings: (payload && payload.settings) || {},
        index: 0,
      });
      renderContext(context, contexts.get(context));
      break;
    }
    case "willDisappear":
      contexts.delete(context);
      break;
    case "didReceiveSettings": {
      const entry = contexts.get(context);
      if (entry) {
        entry.settings = (payload && payload.settings) || {};
        renderContext(context, entry);
      }
      break;
    }
    case "keyDown":
    case "dialDown":
    case "touchTap": {
      // Jump to the connected mouse, or cycle if none is connected.
      const entry = contexts.get(context);
      if (!entry) break;
      const mice = feed.mice || [];
      const connectedAt = mice.findIndex((m) => m.connected);
      entry.index = connectedAt >= 0 ? connectedAt : entry.index + 1;
      poll();
      break;
    }
    case "dialRotate": {
      const entry = contexts.get(context);
      if (!entry) break;
      entry.index += (payload && payload.ticks) || 0;
      renderContext(context, entry);
      break;
    }
    default:
      break;
  }
}

// ---------------------------------------------------------------- startup

function parseArgs(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i += 1) {
    const flag = argv[i];
    if (flag.startsWith("-")) args[flag.slice(1)] = argv[i + 1];
  }
  return args;
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const port = args.port;
  const uuid = args.pluginUUID;
  const registerEvent = args.registerEvent;
  if (!port || !uuid || !registerEvent) {
    console.error("missing Stream Deck registration arguments");
    process.exit(1);
  }

  log("=== plugin start ===", `node=${process.version}`);
  log("feed dir", FEED_DIR, `exists=${fs.existsSync(FEED_FILE)}`);
  log("icon dir", ICON_DIR, `exists=${fs.existsSync(ICON_DIR)}`);

  socket = new WebSocket(`ws://127.0.0.1:${port}`);
  socket.addEventListener("open", () => {
    log("connected, registering");
    send({ event: registerEvent, uuid });
    poll();
    setInterval(poll, POLL_MS);
  });
  socket.addEventListener("message", (event) => onMessage(event.data));
  socket.addEventListener("close", () => process.exit(0));
  socket.addEventListener("error", () => {});
}

main();
