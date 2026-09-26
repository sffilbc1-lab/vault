
// Vault site behaviour. All maps are ILLUSTRATIVE; the live console is linked
// (and embedded when it is running) for real telemetry.
//
// Behaviour shown here mirrors the implementation:
//  - erasure coding 4+2: any 4 of 6 pieces rebuild a chunk (vault/erasure.py)
//  - pieces are spread across zones first (vault/placement.py: zone_order)
//  - write quorum: replication n//2+1 copies, erasure k+1 distinct nodes (vault/policy.py)
//  - heartbeats every 1 s; suspect on a miss; failed after dead_after (default 10 s)
//  - repair rebuilds from survivors via the gateway-side repair service and
//    verifies rebuilt data before storing it (vault/maintenance.py)

import { StorageMap, reducedMotion } from "./storage-map.js";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

// ------------------------------------------------------------------ console link
// Public deployment: no Live Console is hosted. Set this to a real, publicly
// reachable console URL to enable the link/embed (and allow its origin in the
// server's --console option). Empty = never link to, embed or probe any console.
const PUBLIC_CONSOLE_URL = "https://untaxed-carving-sterilize.ngrok-free.dev";
const CONSOLE = PUBLIC_CONSOLE_URL ? PUBLIC_CONSOLE_URL.replace(/\/?$/, "/") : null;
if (CONSOLE) $$(".console-link").forEach((a) => { a.href = CONSOLE; });
$$("[data-console-url]").forEach((el) => {
  el.textContent = CONSOLE ? CONSOLE.replace(/^https?:\/\//, "") : "Live Console · not hosted on this site";
});

let consoleUp = null;
async function checkConsole() {
  if (!CONSOLE) {  // no public console configured: say so, never probe
    $$("[data-console-status]").forEach((el) => {
      el.innerHTML = `<i class="dot"></i>Live Console: local demo only · not hosted on this site`;
    });
    return;
  }
  let up = false;
  try {
    // no-cors: an opaque response still proves the console server answered.
    await fetch(`${CONSOLE}api/snapshot`, { mode: "no-cors", cache: "no-store" });
    up = true;
  } catch { up = false; }
  if (up === consoleUp) return;
  consoleUp = up;
  $$("[data-console-status]").forEach((el) => {
    el.innerHTML = up
      ? `<i class="dot ok"></i>Console running at ${CONSOLE.replace(/^https?:\/\//, "")}`
      : `<i class="dot"></i>Console not running · start it with <code>python3 dashboard/run_demo.py</code>`;
  });
  const frame = $("#console-frame");
  const off = $("#console-offline");
  const existing = $("iframe", frame);
  if (up && !existing) {
    const f = document.createElement("iframe");
    f.src = CONSOLE;
    f.title = "Vault live console";
    f.loading = "lazy";
    f.width = 1280; f.height = 800;
    frame.appendChild(f);
    off.hidden = true;
    fitFrame();
  } else if (!up) {
    existing?.remove();
    off.hidden = false;
  }
}
function fitFrame() {
  const frame = $("#console-frame"), f = $("iframe", frame);
  if (f) f.style.transform = `scale(${frame.clientWidth / 1280})`;
}
window.addEventListener("resize", fitFrame);
checkConsole();
if (CONSOLE) setInterval(checkConsole, 5000);

// ------------------------------------------------------------------ helpers
function whenVisible(el, cb, threshold = 0.35) {
  const io = new IntersectionObserver((es) => es.forEach((e) => cb(e.isIntersecting)), { threshold });
  io.observe(el);
}

// ------------------------------------------------------------------ 1. hero lifecycle
const DIST = [[0, "data", "1"], [2, "data", "2"], [4, "data", "3"], [6, "data", "4"], [1, "parity", "P1"], [5, "parity", "P2"]];
const HOLDERS = DIST.map((d) => d[0]);
const FAILED = 2, SPARE = 3, SURVIVORS = [0, 4, 6, 1];
const placeAll = (m) => DIST.forEach(([i, k, l]) => m.putPiece(i, k, l));

const LIFE = [
  { key: "Upload", tone: "data", readout: "photo.jpg · arriving",
    caption: "A file arrives at the gateway, Vault's front door.",
    end: (m) => m.showObject(true),
    run: (m) => m.objectIn() },
  { key: "Distribute", tone: "data", readout: "6 pieces → 6 nodes in 4 zones",
    caption: "It's split into four data pieces plus two parity pieces, each sent to a different node, spread across zones.",
    end: (m) => { m.hideObject(); placeAll(m); },
    run: (m) => m.distribute(DIST) },
  { key: "Redundancy", tone: "ok", readout: "readable · survives 2 node failures",
    caption: "Any four of the six pieces rebuild the file, so any two nodes can fail without losing it.",
    end: (m) => m.mark("✓ stored", "ok"),
    run: async (m) => { m.mark("✓ stored", "ok"); await m.tokens(HOLDERS, { back: HOLDERS, ms: 480, stagger: 40 }); } },
  { key: "Failure", tone: "warn", readout: "5 of 6 pieces reachable · still readable",
    caption: "Node n3 stops responding. Five pieces are still reachable, and reads keep working from any four of them.",
    end: (m) => { m.mark("✓ read ok", "ok"); m.setNode(FAILED, "off"); m.setLink(FAILED, "dead"); },
    run: async (m) => {
      m.mark(""); m.setNode(FAILED, "off"); m.setLink(FAILED, "dead");
      if (!(await m.wait(700))) return;
      await m.tokens(SURVIVORS, { dir: "in", cls: "data", ms: 650 });
      m.mark("✓ read ok", "ok");
      await m.wait(700);
    } },
  { key: "Detect", tone: "bad", readout: "n3 declared failed",
    caption: "Heartbeats to n3 go unanswered. Vault marks it suspect first, then failed once a timeout passes.",
    end: (m) => { m.mark(""); m.setNode(FAILED, "failed"); },
    run: async (m) => {
      m.mark("");
      const alive = [0, 1, 3, 4, 5, 6, 7];
      await m.tokens([...alive, FAILED], { back: alive, ms: 420, stagger: 25 });
      m.setNode(FAILED, "suspect");
      if (!(await m.wait(700))) return;
      await m.tokens([FAILED], { ms: 420 });
      m.setNode(FAILED, "failed");
      await m.wait(300);
    } },
  { key: "Repair", tone: "warn", readout: "rebuilding piece 2 from 4 survivors",
    caption: "The missing piece is recomputed from four surviving pieces, verified, and stored on a healthy node.",
    end: (m) => m.putPiece(SPARE, "data", "2"),
    run: (m) => m.rebuild(SURVIVORS, SPARE, "data", "2") },
  { key: "Recover", tone: "ok", readout: "6 of 6 pieces · survives 2 node failures",
    caption: "All six pieces exist again. The file can survive two more failures, and nobody had to step in.",
    end: (m) => m.mark("✓ protected", "ok"),
    run: async (m) => { m.mark("✓ protected", "ok"); await m.wait(300); } },
];

class Lifecycle {
  constructor(host) {
    this.map = new StorageMap(host, { nodes: 8, zones: 4, width: 800, height: 520, client: true,
                                      label: "Illustration: a file moving through Vault" });
    this.i = 0;
    this.playing = !reducedMotion.matches;
    this.visible = true;
    this.pending = null;
    this.steps = $("#life-steps");
    this.steps.innerHTML = LIFE.map((s, k) =>
      `<li><button type="button" data-k="${k}" aria-label="Stage ${k + 1}: ${s.key}">${s.key}</button></li>`).join("");
    this.steps.addEventListener("click", (e) => {
      const b = e.target.closest("button[data-k]");
      if (b) { this.setPlaying(false); this.go(+b.dataset.k); }
    });
    this.toggle = $("#life-toggle");
    this.toggle.addEventListener("click", () => {
      this.setPlaying(!this.playing);
      if (this.playing) this.go((this.i + 1) % LIFE.length);
    });
    this.setPlaying(this.playing);
    // Only advance while the map is on screen *and* the page is visible; resume
    // from wherever it stopped when both become true again.
    this.inView = true;
    const recheck = () => {
      this.visible = this.inView && !document.hidden;
      if (this.visible && this.pending != null) { const k = this.pending; this.pending = null; this.go(k); }
    };
    whenVisible(host, (v) => { this.inView = v; recheck(); }, 0.2);
    document.addEventListener("visibilitychange", recheck);
  }

  setPlaying(p) {
    this.playing = p;
    this.toggle.dataset.paused = String(!p);
    this.toggle.setAttribute("aria-label", p ? "Pause animation" : "Play animation");
    // Announce captions only when the viewer is stepping manually.
    $("#life-caption").setAttribute("aria-live", p ? "off" : "polite");
  }

  render(k) {
    const s = LIFE[k];
    $$("li", this.steps).forEach((li, j) => {
      li.className = j < k ? "done" : j === k ? "now" : "";
      li.querySelector("button").setAttribute("aria-current", j === k ? "step" : "false");
    });
    $("#life-caption").textContent = s.caption;
    $("#life-readout").textContent = s.readout;
    $("#life-dot").className = `dot ${s.tone}`;
    this.map.svg.setAttribute("aria-label", `Illustration, stage ${k + 1} of ${LIFE.length}: ${s.caption}`);
  }

  async go(k) {
    const m = this.map;
    this.i = k;
    m.reset();
    const ep = m.epoch;
    for (let j = 0; j < k; j++) LIFE[j].end(m);
    this.render(k);
    if (reducedMotion.matches) { LIFE[k].end(m); return; }
    await LIFE[k].run(m);
    if (m.epoch !== ep) return;
    LIFE[k].end(m);
    if (!this.playing) return;
    if (!(await m.wait(k === LIFE.length - 1 ? 3200 : 1400))) return;
    if (!this.playing) return;
    const next = (k + 1) % LIFE.length;
    if (this.visible) this.go(next); else this.pending = next;
  }
}
const life = new Lifecycle($("#hero-map"));
life.go(0);

// ------------------------------------------------------------------ 2. the problem
const miniOpts = { nodes: 3, zones: 0, width: 320, height: 210, client: false, spread: 40, size: 46 };
$$("[data-problem]").forEach((host) => {
  const kind = host.dataset.problem;
  const m = new StorageMap(host, { ...miniOpts, label: `Illustration: ${kind}` });
  const setup = () => {
    m.reset();
    [0, 1, 2].forEach((i) => m.putPiece(i, "data", "A"));
    if (kind === "crash") { m.setNode(1, "failed"); m.setLink(1, "dead"); }
    if (kind === "partition") { m.setNode(1, "suspect"); m.setLink(1, "broken"); }
    if (kind === "missing") { m.clearPiece(1); m.setNode(1, "missing"); }
    if (kind === "rot") m.corruptPiece(1);
  };
  setup();
  if (reducedMotion.matches) return;
  // A slow heartbeat loop keeps the motif alive; the troubled node never answers.
  let running = false;
  whenVisible(host, async (v) => {
    if (!v || running) return;
    running = true;
    while (running && host.getBoundingClientRect().bottom > 0 && host.getBoundingClientRect().top < innerHeight) {
      const answer = kind === "crash" || kind === "partition" ? [0, 2] : [0, 1, 2];
      await m.tokens([0, 1, 2], { back: answer, ms: 520, stagger: 60 });
      await new Promise((r) => setTimeout(r, 1800));
    }
    running = false;
  });
});

// ------------------------------------------------------------------ 3. one object, many nodes
const POLICIES = {
  replicate: {
    assign: [[0, "data", "A"], [2, "data", "A"], [4, "data", "A"]],
    facts: [
      ["Pieces", "3 full copies, one per zone"],
      ["Survives", `<span class="big">2<small>node failures</small></span>`],
      ["Storage used", `<span class="big">3.0×<small>the file size</small></span><div class="bar"><span style="width:100%"></span></div>`],
      ["A write succeeds", "once 2 of the 3 copies are safely stored"],
      ["A read needs", "any one intact copy"],
    ],
  },
  erasure: {
    assign: [[0, "data", "1"], [2, "data", "2"], [4, "data", "3"], [1, "data", "4"], [3, "parity", "P1"], [5, "parity", "P2"]],
    facts: [
      ["Pieces", "4 data + 2 parity, two per zone"],
      ["Survives", `<span class="big">2<small>node failures</small></span>`],
      ["Storage used", `<span class="big">1.5×<small>the file size</small></span><div class="bar"><span style="width:50%"></span></div>`],
      ["A write succeeds", "once 5 different nodes hold a piece"],
      ["A read needs", "any 4 pieces; data pieces first, so usually no decoding"],
    ],
  },
};
const policyMap = new StorageMap($("#policy-map"), { nodes: 6, zones: 3, width: 720, height: 440, client: false,
                                                      label: "Illustration: how a file is spread under a durability policy" });
let policy = "replicate";
async function showPolicy(p, animate = true) {
  policy = p;
  $$("[data-policy]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.policy === p)));
  $("#policy-facts").innerHTML = POLICIES[p].facts.map(([k, v]) => `<div><dt>${k}</dt><dd>${v}</dd></div>`).join("");
  policyMap.reset();
  if (!animate || reducedMotion.matches) { POLICIES[p].assign.forEach(([i, k, l]) => policyMap.putPiece(i, k, l)); return; }
  policyMap.showObject(true);
  if (await policyMap.wait(400)) await policyMap.distribute(POLICIES[p].assign, 90);
}
$$("[data-policy]").forEach((b) => b.addEventListener("click", () => showPolicy(b.dataset.policy)));
showPolicy("replicate", false);
let policySeen = false;
whenVisible($("#policy-map"), (v) => { if (v && !policySeen) { policySeen = true; showPolicy(policy); } });

// ------------------------------------------------------------------ 4. failure is expected
const EC = POLICIES.erasure.assign;
const failMap = new StorageMap($("#fail-map"), { nodes: 6, zones: 3, width: 720, height: 440, client: false,
                                                  label: "Interactive illustration: take nodes offline" });
const offline = new Set();
function failSetup() {
  failMap.reset();
  offline.clear();
  EC.forEach(([i, k, l]) => failMap.putPiece(i, k, l));
  failMap.nodes.forEach((g, i) => {
    g.setAttribute("data-clickable", "");
    g.setAttribute("tabindex", "0");
    g.setAttribute("role", "button");
    g.setAttribute("aria-pressed", "false");
    g.setAttribute("aria-label", `Node n${i + 1}, online. Activate to take it offline.`);
  });
  updateFail(false);
}
async function updateFail(animate = true) {
  const reachable = EC.filter(([i]) => !offline.has(i));
  const n = reachable.length;
  $("#fail-count").textContent = n;
  $("#fail-cells").innerHTML = EC.map(([i, k]) => `<i class="${k === "parity" ? "p" : ""} ${offline.has(i) ? "off" : ""}"></i>`).join("");
  let tone, text;
  if (n >= 5) { tone = "ok"; text = "Readable. Vault reads any four reachable pieces."; }
  else if (n === 4) { tone = "warn"; text = "Still readable, with no margin left. One more failure would make this file unavailable."; }
  else { tone = "bad"; text = `Unavailable: only ${n} pieces reachable and 4 are needed. Nothing is deleted; the file is readable again as soon as a node returns.`; }
  if (offline.size >= 2 && n >= 4) text += " New uploads to this bucket need 5 reachable nodes, so writes pause until one returns.";
  $("#fail-verdict").innerHTML = `<i class="dot ${tone}"></i><span>${text}</span>`;
  if (!animate) return;
  failMap.mark("");
  if (n >= 4) {
    await failMap.tokens(reachable.slice(0, 4).map(([i]) => i), { dir: "in", cls: "data", ms: 520, stagger: 50 });
    failMap.mark("✓ read ok", "ok");
  } else {
    failMap.mark("✗ not enough pieces", "bad");
  }
}
function toggleNode(i) {
  const off = !offline.has(i);
  if (off) offline.add(i); else offline.delete(i);
  failMap.setNode(i, off ? "failed" : "ok");
  failMap.setLink(i, off ? "dead" : "");
  const g = failMap.nodes[i];
  g.setAttribute("aria-pressed", String(off));
  g.setAttribute("aria-label", `Node n${i + 1}, ${off ? "offline" : "online"}. Activate to ${off ? "bring it back" : "take it offline"}.`);
  updateFail(true);
}
failMap.nodes.forEach((g, i) => {
  g.addEventListener("click", () => toggleNode(i));
  g.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleNode(i); } });
});
$("#fail-reset").addEventListener("click", failSetup);
failSetup();

// ------------------------------------------------------------------ 5. detect → repair → restore
const phaseOpts = { nodes: 5, zones: 0, width: 360, height: 250, client: false, spread: 30, size: 44 };
const phases = Object.fromEntries($$("[data-phase]").map((h) =>
  [h.dataset.phase, new StorageMap(h, { ...phaseOpts, label: `Illustration: ${h.dataset.phase}` })]));
const COPIES = [0, 1, 2];
function phaseEnd(name) {
  const m = phases[name];
  m.reset();
  if (name === "detect") { COPIES.forEach((i) => m.putPiece(i, "data", "A")); m.setNode(1, "failed"); }
  if (name === "repair") { [0, 2, 3].forEach((i) => m.putPiece(i, "data", "A")); m.putPiece(1, "data", "A"); m.setNode(1, "failed"); }
  if (name === "restore") { [0, 2, 3].forEach((i) => m.putPiece(i, "data", "A")); m.putPiece(1, "data", "A"); m.setNode(1, "failed"); m.mark("✓ 3 copies", "ok"); }
}
async function playPhases() {
  if (reducedMotion.matches) { Object.keys(phases).forEach(phaseEnd); return; }
  const d = phases.detect, r = phases.repair, s = phases.restore;
  [d, r, s].forEach((m) => { m.reset(); COPIES.forEach((i) => m.putPiece(i, "data", "A")); });
  // detect
  await d.tokens([0, 1, 2, 3, 4], { back: [0, 2, 3, 4], ms: 480, stagger: 40 });
  d.setNode(1, "suspect");
  if (!(await d.wait(800))) return;
  await d.tokens([1], { ms: 480 });
  d.setNode(1, "failed");
  // repair
  r.setNode(1, "failed");
  if (!(await r.wait(600))) return;
  await r.rebuild([0], 3, "data", "A");
  // restore
  s.setNode(1, "failed");
  s.putPiece(3, "data", "A");
  if (!(await s.wait(500))) return;
  await s.tokens([0, 2, 3], { back: [0, 2, 3], ms: 480, stagger: 60 });
  s.mark("✓ 3 copies", "ok");
}
Object.keys(phases).forEach(phaseEnd);
let phasesSeen = false;
whenVisible($("#triptych"), (v) => { if (v && !phasesSeen) { phasesSeen = true; playPhases(); } }, 0.4);
$("#replay").addEventListener("click", playPhases);
