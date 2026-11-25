// Draws the installer's pictures (its side panel and its buttons) in the app's own font, logo
// and colors, at each display scale, with Microsoft Edge: src-tauri/windows/art/*.png, which
// build-installer.ps1 makes the bitmaps of. Run it again when the look changes:
//   node scripts/installer-art.mjs
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const desktop = join(dirname(fileURLToPath(import.meta.url)), "..");
const out = join(desktop, "src-tauri", "windows", "art");
const font = readFileSync(
  join(desktop, "node_modules", "@fontsource-variable", "geist", "files", "geist-latin-wght-normal.woff2"),
).toString("base64");

// The sizes are in pixels at 100%, multiples of 4 so that every scale comes out whole.
const SCALES = [100, 125, 150, 175, 200];
const SIDE = { width: 220, height: 400 };
// A button's picture has 2 pixels around it, which installer-theme.nsh cuts off with the
// border Windows draws there.
const MARGIN = 2;
const BUTTON = 32;
const BUTTONS = [
  ["install", "Install", 112, true],
  ["update", "Update", 112, true],
  ["reinstall", "Reinstall", 112, true],
  ["anyway", "Install anyway", 136, true],
  ["finish", "Finish", 112, true],
  ["uninstall", "Uninstall", 112, true],
  ["close", "Close", 112, true],
  ["cancel", "Cancel", 92, false],
  ["change", "Change...", 92, false],
];

// The logo (lucide's audio-lines) and, below, the lanes of a call: who spoke when.
const LOGO = ["M2 10v3", "M6 6v11", "M10 3v18", "M14 8v7", "M18 5v13", "M22 10v3"];
const LANES = [
  ["#fafafa", [[0, 26], [64, 40], [136, 18], [172, 30]]],
  ["#0ea5e9", [[30, 28], [110, 22], [158, 12], [206, 20]]],
  ["#f97316", [[20, 8], [86, 20], [124, 10], [190, 14]]],
  ["#d946ef", [[60, 12], [104, 6], [146, 24], [214, 10]]],
];

const lanes = LANES.map(
  ([color, bars], row) =>
    `<div class="lane" style="top:${276 + row * 20}px">` +
    bars
      .map(([x, w]) => `<i style="left:${x}px;width:${w}px;background:${color}"></i>`)
      .join("") +
    "</div>",
).join("");

const buttons = BUTTONS.map(
  ([id, label, width, primary]) =>
    `<div class="shot" id="button-${id}" style="width:${width + 2 * MARGIN}px;height:${BUTTON + 2 * MARGIN}px">` +
    `<div class="button ${primary ? "primary" : "outline"}" style="width:${width}px;height:${BUTTON}px">${label}</div></div>`,
).join("");

const page = `<!doctype html><meta charset="utf-8"><style>
@font-face { font-family: Geist; src: url(data:font/woff2;base64,${font}) format("woff2"); font-weight: 100 900; }
* { box-sizing: border-box; margin: 0; }
body { background: #fff; font-family: Geist; -webkit-font-smoothing: antialiased; }
.shot { position: relative; overflow: hidden; background: #fff; margin-bottom: 8px; }
#side { width: ${SIDE.width}px; height: ${SIDE.height}px; background: #0a0a0a; }
#side .glow { position: absolute; inset: 0; background: radial-gradient(130% 55% at 30% 108%, rgb(255 255 255 / 13%), transparent 70%); }
#side .tile { position: absolute; left: 28px; top: 36px; width: 48px; height: 48px; border-radius: 13px; background: #fafafa; display: grid; place-items: center; }
#side .name { position: absolute; left: 28px; top: 104px; color: #fafafa; font-size: 26px; font-weight: 600; letter-spacing: -0.03em; }
#side .tag { position: absolute; left: 28px; top: 140px; width: 160px; color: #a1a1a1; font-size: 13px; line-height: 1.5; }
#side .lane { position: absolute; left: 28px; right: 0; height: 12px; border-radius: 4px 0 0 4px; background: rgb(255 255 255 / 5%); overflow: hidden; }
#side .lane i { position: absolute; top: 0; bottom: 0; border-radius: 3px; }
#side .fade { position: absolute; left: 0; right: 0; bottom: 0; top: 268px; background: linear-gradient(to right, transparent 62%, #0a0a0a); }
.button { position: absolute; left: ${MARGIN}px; top: ${MARGIN}px; border-radius: 9px; display: grid; place-items: center; font-size: 14px; font-weight: 500; letter-spacing: -0.005em; }
.primary { background: #171717; color: #fafafa; }
.outline { background: #fff; color: #0a0a0a; border: 1px solid #e5e5e5; }
</style><body>
<div class="shot" id="side"><div class="glow"></div>
<div class="tile"><svg width="27" height="27" viewBox="0 0 24 24" fill="none" stroke="#0a0a0a" stroke-width="2" stroke-linecap="round">${LOGO.map((d) => `<path d="${d}"/>`).join("")}</svg></div>
<div class="name">ZachCap</div><div class="tag">Meetings that remember who said what</div>${lanes}<div class="fade"></div></div>
${buttons}</body>`;

const EDGE = "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe";
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const profile = mkdtempSync(join(tmpdir(), "zachcap-art-"));
const edge = spawn(
  EDGE,
  ["--headless=new", "--disable-gpu", "--no-first-run", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"],
  { stdio: "ignore" },
);
try {
  const file = join(profile, "DevToolsActivePort");
  for (let i = 0; i < 100 && !existsSync(file); i++) await sleep(100);
  const port = readFileSync(file, "utf8").split("\n")[0].trim();
  const targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json();
  const socket = new WebSocket(targets.find((target) => target.type === "page").webSocketDebuggerUrl);
  await new Promise((resolve, reject) => ((socket.onopen = resolve), (socket.onerror = reject)));
  let next = 1;
  const waiting = new Map();
  socket.onmessage = (message) => {
    const { id, result, error } = JSON.parse(message.data);
    if (!id) return;
    const { resolve, reject } = waiting.get(id);
    waiting.delete(id);
    if (error) reject(new Error(error.message));
    else resolve(result);
  };
  const send = (method, params = {}) =>
    new Promise((resolve, reject) => {
      waiting.set(next, { resolve, reject });
      socket.send(JSON.stringify({ id: next++, method, params }));
    });
  const { frameTree } = await send("Page.getFrameTree");
  await send("Page.setDocumentContent", { frameId: frameTree.frame.id, html: page });
  await send("Runtime.evaluate", { expression: "document.fonts.ready.then(() => 1)", awaitPromise: true });
  const { result } = await send("Runtime.evaluate", {
    expression: `JSON.stringify([...document.querySelectorAll(".shot")].map((e) => { const r = e.getBoundingClientRect(); return [e.id, r.x, r.y, r.width, r.height]; }))`,
    returnByValue: true,
  });
  mkdirSync(out, { recursive: true });
  for (const scale of SCALES) {
    for (const [id, x, y, width, height] of JSON.parse(result.value)) {
      // Drawn at the scale by the page itself, so its text is as sharp as the app's.
      await send("Emulation.setDeviceMetricsOverride", { width: 900, height: 1000, deviceScaleFactor: scale / 100, mobile: false });
      const { data } = await send("Page.captureScreenshot", { clip: { x, y, width, height, scale: 1 }, captureBeyondViewport: true });
      writeFileSync(join(out, `${id}-${scale}.png`), Buffer.from(data, "base64"));
    }
  }
  console.log(`${SCALES.length * (BUTTONS.length + 1)} pictures in ${out}`);
} finally {
  edge.kill();
}
