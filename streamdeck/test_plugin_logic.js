// Exercises the plugin's feed handling against the real published feed,
// without Stream Deck running. Run: node streamdeck/test_plugin_logic.js
//
// Loads plugin.js with a fake WebSocket so main() does not exit, then drives it
// with synthetic Stream Deck events and prints what it would send.

"use strict";

const fs = require("node:fs");
const path = require("node:path");
const Module = require("node:module");

const sent = [];

// Fake the global WebSocket the plugin uses.
class FakeSocket {
  constructor() {
    this.readyState = 1;
    this.listeners = {};
    setTimeout(() => this.fire("open"), 0);
  }
  addEventListener(name, fn) {
    (this.listeners[name] ||= []).push(fn);
  }
  fire(name, arg) {
    for (const fn of this.listeners[name] || []) fn(arg);
  }
  send(raw) {
    sent.push(JSON.parse(raw));
  }
}
global.WebSocket = FakeSocket;

process.argv = [
  process.argv[0],
  "plugin.js",
  "-port",
  "12345",
  "-pluginUUID",
  "test-uuid",
  "-registerEvent",
  "registerPlugin",
  "-info",
  "{}",
];

const pluginPath = path.join(
  __dirname,
  "com.kai.mousebattery.sdPlugin",
  "bin",
  "plugin.js"
);

// Capture the socket the plugin creates.
let created = null;
const RealFake = global.WebSocket;
global.WebSocket = function (...args) {
  created = new RealFake(...args);
  return created;
};

require(pluginPath);

function describe(label) {
  console.log(`\n=== ${label} ===`);
  for (const message of sent.splice(0)) {
    const p = message.payload || {};
    if (message.event === "setImage") {
      console.log(`  setImage    ${(p.image || "").slice(0, 34)}... (${(p.image || "").length} chars)`);
    } else if (message.event === "setTitle") {
      console.log(`  setTitle    ${JSON.stringify(p.title)}`);
    } else if (message.event === "setFeedback") {
      console.log(
        `  setFeedback title=${JSON.stringify(p.title)} value=${JSON.stringify(p.value)} ` +
          `indicator=${JSON.stringify(p.indicator)} icon=${p.icon ? "yes" : "no"}`
      );
    } else {
      console.log(`  ${message.event} ${JSON.stringify(p)}`);
    }
  }
}

setTimeout(() => {
  describe("registration + first poll");

  created.fire("message", {
    data: JSON.stringify({
      event: "willAppear",
      context: "key-1",
      payload: { controller: "Keypad", settings: {} },
    }),
  });
  describe("keypad appears (follows connected mouse)");

  created.fire("message", {
    data: JSON.stringify({
      event: "willAppear",
      context: "dial-1",
      payload: { controller: "Encoder", settings: {} },
    }),
  });
  describe("dial appears");

  created.fire("message", {
    data: JSON.stringify({
      event: "dialRotate",
      context: "dial-1",
      payload: { ticks: 1 },
    }),
  });
  describe("dial rotated one tick");

  created.fire("message", {
    data: JSON.stringify({
      event: "dialRotate",
      context: "dial-1",
      payload: { ticks: -3 },
    }),
  });
  describe("dial rotated back three (wrap-around)");

  const feedFile = path.join(
    process.env.APPDATA,
    "MouseBatteryTracker",
    "streamdeck",
    "feed.json"
  );
  const mice = JSON.parse(fs.readFileSync(feedFile, "utf8")).mice;
  console.log(`\nfeed contains ${mice.length} mice: ${mice.map((m) => m.name).join(", ")}`);
  process.exit(0);
}, 200);
