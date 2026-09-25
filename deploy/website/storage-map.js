// Storage map: the site's recurring visual. A gateway fans out to independent
// storage nodes; small squares are pieces of a file moving through the system.
//
// ILLUSTRATIVE ONLY. Nothing here reads live data; the live console does that.
// The behaviour it depicts mirrors what Vault actually does (see site.js).

const NS = "http://www.w3.org/2000/svg";
export const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

function el(tag, attrs = {}, parent) {
  const e = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (parent) parent.appendChild(e);
  return e;
}
const ease = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);

export class StorageMap {
  /**
   * @param {HTMLElement} host
   * @param {object} o
   *   nodes: number of storage nodes; zones: nodes per zone are grouped in order
   *   client: draw an incoming file on the left; spread: arc curvature (0 = straight column)
   */
  constructor(host, { nodes = 8, zones = 4, width = 800, height = 520, client = true,
                      spread = 20, size = 64, label = "Storage map" } = {}) {
    this.W = width; this.H = height; this.n = nodes; this.s = size;
    this.epoch = 0;
    this.svg = el("svg", { viewBox: `0 0 ${width} ${height}`, class: "smap", role: "img", "aria-label": label });
    host.appendChild(this.svg);

    // Nodes are spaced evenly by height along a gentle arc (evenly by angle crowds
    // the middle), zones get a little extra gap, and alternate nodes are staggered
    // sideways when rows would otherwise touch.
    const pad = 16, lab = 20, zgap = zones > 0 ? size * 0.35 : 0;
    const per = zones > 0 ? Math.ceil(nodes / zones) : nodes;
    this.zoneIdx = (i) => Math.floor(i / per);
    const nz = Math.ceil(nodes / per);
    this.g = { x: width * (client ? 0.24 : 0.14), y: height / 2 };
    this.c = { x: width * 0.06, y: height / 2 };
    const top = pad + size / 2, bottom = height - pad - size / 2 - lab;
    const step = nodes > 1 ? (bottom - top - (nz - 1) * zgap) / (nodes - 1) : 0;
    const stagger = nodes > 1 && step < size * 1.3 ? size * 1.45 : 0;
    const bracket = zones > 0 ? 34 : 0;
    const xMax = width - pad - size / 2 - bracket - stagger / 2;
    const span = nodes > 1 ? (bottom - top) / 2 : 1;
    // Curve only when rows have room; a staggered zigzag stays straight so the
    // curve can never pull neighbouring nodes back on top of each other.
    const curve = stagger ? 0 : spread / 100;
    this.pos = Array.from({ length: nodes }, (_, i) => {
      const y = nodes > 1 ? top + i * step + this.zoneIdx(i) * zgap : height / 2;
      const t = (y - height / 2) / span;
      const x = xMax - (xMax - this.g.x) * curve * t * t * 0.55 + (i % 2 ? -stagger / 2 : stagger / 2);
      return { x, y };
    });

    this.layers = {
      zones: el("g", { class: "zones" }, this.svg),
      links: el("g", { class: "links" }, this.svg),
      nodes: el("g", { class: "nodes" }, this.svg),
      gw: el("g", { class: "gw" }, this.svg),
      fly: el("g", { class: "fly" }, this.svg),
    };
    if (zones > 0) this._drawZones();
    this.links = this.pos.map((p) => {
      const a = this._edge(this.g, p, 0.9 * size), b = this._edge(p, this.g, size * 0.62);
      return el("line", { x1: a.x, y1: a.y, x2: b.x, y2: b.y, class: "link" }, this.layers.links);
    });
    this.nodes = this.pos.map((p, i) => this._drawNode(p, i));
    this._drawGateway();
    this.pieces = new Array(nodes).fill(null);
    this.obj = null;
  }

  // ---------------------------------------------------------------- drawing
  _edge(from, to, inset) {
    const dx = to.x - from.x, dy = to.y - from.y, d = Math.hypot(dx, dy) || 1;
    return { x: from.x + (dx / d) * inset, y: from.y + (dy / d) * inset };
  }

  _drawZones() {
    const groups = {};
    this.pos.forEach((p, i) => (groups[this.zoneIdx(i)] ||= []).push(p));
    const h = this.s / 2 + 4;
    const x = Math.max(...this.pos.map((p) => p.x)) + this.s / 2 + 14;
    for (const [z, ps] of Object.entries(groups)) {
      const y0 = Math.min(...ps.map((p) => p.y)) - h, y1 = Math.max(...ps.map((p) => p.y)) + h;
      el("path", { d: `M${x - 5} ${y0}H${x}V${y1}H${x - 5}`, class: "zone" }, this.layers.zones);
      const t = el("text", { x: x + 10, y: (y0 + y1) / 2, class: "zone-label", "text-anchor": "middle",
                             transform: `rotate(90 ${x + 10} ${(y0 + y1) / 2})` }, this.layers.zones);
      t.textContent = `zone ${String.fromCharCode(97 + +z)}`;
    }
  }

  _drawNode(p, i) {
    const s = this.s, g = el("g", { class: "node st-ok", transform: `translate(${p.x} ${p.y})` }, this.layers.nodes);
    el("rect", { x: -s / 2, y: -s / 2, width: s, height: s, rx: s * 0.16, class: "node-body" }, g);
    const q = s * 0.42;
    el("rect", { x: -q / 2, y: -q / 2, width: q, height: q, rx: 3, class: "node-slot" }, g);
    el("circle", { cx: s / 2 - 9, cy: -s / 2 + 9, r: 3.5, class: "node-dot" }, g);
    const x = el("path", { d: `M${-q / 2} ${-q / 2}L${q / 2} ${q / 2}M${q / 2} ${-q / 2}L${-q / 2} ${q / 2}`, class: "node-x" }, g);
    x.setAttribute("opacity", "0");
    const t = el("text", { x: -s / 2, y: s / 2 + 16, class: "node-label" }, g);
    t.textContent = `n${i + 1}`;
    return g;
  }

  _drawGateway() {
    const w = this.s * 1.5, h = this.s * 0.95, { x, y } = this.g;
    this.gwNode = el("g", { class: "gateway", transform: `translate(${x} ${y})` }, this.layers.gw);
    el("rect", { x: -w / 2, y: -h / 2, width: w, height: h, rx: 12, class: "gw-body" }, this.gwNode);
    for (let k = -1; k <= 1; k++) el("line", { x1: -w / 2 + 14, x2: -w / 2 + 26, y1: k * 7, y2: k * 7, class: "gw-tick" }, this.gwNode);
    const t = el("text", { x: 6, y: 4, class: "gw-label", "text-anchor": "middle" }, this.gwNode);
    t.textContent = "GATEWAY";
    this.gwMark = el("text", { x: 0, y: -h / 2 - 12, class: "gw-mark", "text-anchor": "middle" }, this.gwNode);
  }

  _piece(kind, label, size = this.s * 0.42) {
    const g = el("g", { class: `piece ${kind}` }, this.layers.fly);
    el("rect", { x: -size / 2, y: -size / 2, width: size, height: size, rx: 3 }, g);
    if (kind === "parity") {  // hollow with one diagonal: "derived from the data"
      el("line", { x1: -size / 2 + 3, y1: size / 2 - 3, x2: size / 2 - 3, y2: -size / 2 + 3, class: "hatch" }, g);
    }
    if (label) { const t = el("text", { x: 0, y: 4, "text-anchor": "middle" }, g); t.textContent = label; }
    return g;
  }

  static place(g, p, scale = 1, opacity = 1) {
    g.setAttribute("transform", `translate(${p.x} ${p.y}) scale(${scale})`);
    // Leave full opacity to CSS so state classes (e.g. .dim) can take effect.
    g.style.opacity = opacity >= 1 ? "" : opacity;
  }

  // ---------------------------------------------------------------- animation primitives
  /** Resolves true when finished, false if the map was reset meanwhile. */
  tween(ms, fn) {
    const ep = this.epoch;
    if (reducedMotion.matches || ms <= 0) { fn(1); return Promise.resolve(this.epoch === ep); }
    return new Promise((resolve) => {
      const t0 = performance.now();
      const step = (now) => {
        if (this.epoch !== ep) return resolve(false);
        const t = Math.min(1, (now - t0) / ms);
        fn(ease(t));
        if (t < 1) requestAnimationFrame(step); else resolve(true);
      };
      requestAnimationFrame(step);
    });
  }
  wait(ms) {
    const ep = this.epoch;
    if (reducedMotion.matches) return Promise.resolve(true);
    return new Promise((r) => setTimeout(() => r(this.epoch === ep), ms));
  }
  async fly(g, from, to, ms = 700, { s0 = 1, s1 = 1, o0 = 1, o1 = 1 } = {}) {
    return this.tween(ms, (t) => StorageMap.place(g, { x: from.x + (to.x - from.x) * t, y: from.y + (to.y - from.y) * t },
                                                   s0 + (s1 - s0) * t, o0 + (o1 - o0) * t));
  }

  // ---------------------------------------------------------------- state
  reset() {
    this.epoch++;
    this.layers.fly.replaceChildren();
    this.pieces.fill(null);
    this.obj = null;
    this.nodes.forEach((_, i) => this.setNode(i, "ok"));
    this.links.forEach((l) => l.setAttribute("class", "link"));
    this.gwMark.textContent = "";
    this.gwNode.setAttribute("class", "gateway");
  }

  /** ok | off (went dark, not yet noticed) | suspect | failed | spare-hi */
  setNode(i, state) {
    this.nodes[i].setAttribute("class", `node st-${state}`);
    this.nodes[i].querySelector(".node-x").setAttribute("opacity", state === "failed" ? "1" : "0");
    const pc = this.pieces[i];
    if (pc) pc.classList.toggle("dim", state === "off" || state === "suspect" || state === "failed");
  }
  setLink(i, cls = "") { this.links[i].setAttribute("class", `link ${cls}`.trim()); }
  mark(text, cls = "") { this.gwMark.textContent = text; this.gwNode.setAttribute("class", `gateway ${cls}`.trim()); }

  putPiece(i, kind, label) {
    this.pieces[i]?.remove();
    const g = this._piece(kind, label);
    StorageMap.place(g, this.pos[i]);
    this.pieces[i] = g;
    return g;
  }
  corruptPiece(i) { this.pieces[i]?.classList.add("rot"); }
  clearPiece(i) { this.pieces[i]?.remove(); this.pieces[i] = null; }

  hideObject() { this.obj?.remove(); this.obj = null; }

  showObject(atGateway = false) {
    this.obj?.remove();
    const g = el("g", { class: "object" }, this.layers.fly);
    const s = this.s * 0.62;
    el("path", { d: `M${-s / 2} ${-s / 2}H${s / 4}L${s / 2} ${-s / 4}V${s / 2}H${-s / 2}Z`, class: "obj-body" }, g);
    el("path", { d: `M${s / 4} ${-s / 2}V${-s / 4}H${s / 2}`, class: "obj-fold" }, g);
    const t = el("text", { x: 0, y: s / 2 + 16, "text-anchor": "middle", class: "obj-label" }, g);
    t.textContent = "photo.jpg";
    StorageMap.place(g, atGateway ? this.g : this.c, atGateway ? 0.7 : 1, 1);
    this.obj = g;
    return g;
  }

  // ---------------------------------------------------------------- composite motions
  async objectIn() {
    const g = this.showObject(false);
    await this.wait(300);
    return this.fly(g, this.c, this.g, 900, { s0: 1, s1: 0.7 });
  }

  /** assignments: [[nodeIndex, kind, label], ...] — pieces leave the gateway for their nodes. */
  async distribute(assignments, stagger = 110) {
    if (!this.obj) this.showObject(true);
    const ep = this.epoch;
    const flights = assignments.map(([i, kind, label], k) => (async () => {
      if (!(await this.wait(k * stagger))) return;
      const g = this._piece(kind, label);
      this.setLink(i, "active");
      await this.fly(g, this.g, this.pos[i], 800, { s0: 0.5, s1: 1, o0: 0, o1: 1 });
      if (this.epoch !== ep) return;
      this.pieces[i]?.remove();
      this.pieces[i] = g;
      this.setLink(i, "");
    })());
    const obj = this.obj;
    this.tween(500, (t) => { if (obj) obj.style.opacity = 1 - t; });
    await Promise.all(flights);
    if (this.epoch === ep) { this.obj?.remove(); this.obj = null; }
    return this.epoch === ep;
  }

  /** Small tokens travel between gateway and nodes (heartbeats or reads). */
  async tokens(nodeIdx, { dir = "out", cls = "hb", ms = 650, stagger = 60, back = [] } = {}) {
    const ep = this.epoch;
    await Promise.all(nodeIdx.map((i, k) => (async () => {
      if (!(await this.wait(k * stagger))) return;
      const g = el("circle", { r: 4, class: `token ${cls}` }, this.layers.fly);
      const from = dir === "out" ? this.g : this.pos[i], to = dir === "out" ? this.pos[i] : this.g;
      this.tween(ms, (t) => { g.setAttribute("cx", from.x + (to.x - from.x) * t); g.setAttribute("cy", from.y + (to.y - from.y) * t); })
        .then(async () => {
          if (back.includes(i) && this.epoch === ep) {
            await this.tween(ms, (t) => { g.setAttribute("cx", to.x + (from.x - to.x) * t); g.setAttribute("cy", to.y + (from.y - to.y) * t); });
          }
          g.remove();
        });
      await this.wait(ms * (back.includes(i) ? 2 : 1));
    })()));
    return this.epoch === ep;
  }

  /** Pieces from `sources` travel to the gateway; a rebuilt piece travels to `target`. */
  async rebuild(sources, target, kind, label) {
    const ep = this.epoch;
    sources.forEach((i) => this.setLink(i, "active"));
    await this.tokens(sources, { dir: "in", cls: "data", ms: 700, stagger: 90 });
    if (this.epoch !== ep) return false;
    sources.forEach((i) => this.setLink(i, ""));
    this.mark("rebuilding", "busy");
    await this.wait(450);
    this.setLink(target, "active");
    const g = this._piece(kind, label);
    const ok = await this.fly(g, this.g, this.pos[target], 800, { s0: 0.5, s1: 1, o0: 0, o1: 1 });
    if (!ok) return false;
    this.pieces[target]?.remove();
    this.pieces[target] = g;
    g.classList.add("fresh");
    this.setLink(target, "");
    this.mark("");
    return true;
  }
}
