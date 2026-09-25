// Vault console. Renders only data returned by Vault through the dashboard adapter.
// Values derived in the browser (health verdict, observed recovery progress, the
// redundancy chart) are computed from those real values and labelled as such.

const POLL_MS = 2000;
const OBJECTS_MS = 4000;

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const enc = encodeURIComponent;
function setHTML(el, html) {
  if (el && el._h !== html) { el.innerHTML = html; el._h = html; }
}

// ---------------------------------------------------------------- state
const state = {
  session: null,         // {username, role, csrf} from the server; never credentials
  snap: null,
  skew: 0,               // adapter clock - browser clock (seconds)
  samples: [],           // redundancy samples taken by this page
  episode: null,         // current observed recovery {start, peak}
  lastEpisode: null,
  seen: new Set(),       // event ids already rendered (for the "new" flash)
  firstRender: true,
  bucket: localStorage.getItem("vault.bucket") || null,
  objects: [],
  objectsFor: null,
  objectsError: null,
  filter: "all",
  view: "overview",
  inflight: false,
  pageStart: Date.now() / 1000,
};
const now = () => Date.now() / 1000 + state.skew;

// ---------------------------------------------------------------- formatting
function fmtBytes(n) {
  if (n == null || Number.isNaN(n)) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, v = n;
  while (v >= 1000 && i < u.length - 1) { v /= 1000; i++; }
  return `${i ? v.toFixed(v < 10 ? 2 : v < 100 ? 1 : 0) : v} ${u[i]}`;
}
function ago(ts) {
  if (!ts) return "—";
  const s = Math.max(0, now() - ts);
  if (s < 10) return `${s.toFixed(1)}s ago`;
  if (s < 90) return `${Math.round(s)}s ago`;
  if (s < 5400) return `${Math.round(s / 60)}m ago`;
  if (s < 172800) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}
function clock(ts) {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
// Relative times are filled in by a ticker, so re-rendered HTML is identical between
// polls and unchanged elements are left alone (no flicker under the cursor).
const agoSpan = (ts) => `<span data-ago="${ts}"></span>`;
// Volatile values (relative times, probe latency) are written in place after each
// render instead of being baked into the HTML.
const tick = () => {
  $$("[data-ago]").forEach((el) => { el.textContent = ago(+el.dataset.ago); });
  const live = state.snap?.nodes_live || {};
  const seen = Object.fromEntries((state.snap?.cluster?.nodes || []).map((n) => [n.id, n.last_seen]));
  $$("[data-hb]").forEach((el) => { el.textContent = ago(seen[el.dataset.hb]); });
  $$("[data-lat]").forEach((el) => { const l = live[el.dataset.lat]; el.textContent = l?.reachable ? `${l.latency_ms} ms` : "—"; });
};
const plural = (n, w, p = w + "s") => `${n} ${n === 1 ? w : p}`;

// Vault blob keys are content-addressed: <sha256>.<policy>[.<shard index>]
function parseBlob(key) {
  const m = /^([0-9a-f]{64})\.(r\d+|ec\d+\+\d+)(?:\.(\d+))?$/.exec(key || "");
  if (!m) return { raw: key };
  return { sha: m[1], short: m[1].slice(0, 10), sig: m[2], idx: m[3] != null ? +m[3] : null };
}
function blobLabel(key) {
  const b = parseBlob(key);
  if (!b.sig) return `<span class="mono">${esc(key)}</span>`;
  return `chunk <span class="mono">${b.short}</span>` + (b.idx != null ? ` · shard ${b.idx}` : "");
}

function policyInfo(p) {
  if (!p) return null;
  if (p.scheme === "replicate") {
    return { label: `Replication ×${p.n}`, tol: p.n - 1, overhead: p.n, width: p.n, data: p.n, parity: 0,
             detail: `${p.n} full copies on different nodes · write needs ${p.w} acks` };
  }
  return { label: `Erasure ${p.k}+${p.m}`, tol: p.m, overhead: (p.k + p.m) / p.k, width: p.k + p.m,
           data: p.k, parity: p.m,
           detail: `${p.k} data + ${p.m} parity shards · write needs ${p.w} nodes` };
}

// ---------------------------------------------------------------- events
const CATS = [["all", "All"], ["nodes", "Nodes"], ["repair", "Repair"], ["integrity", "Integrity"],
              ["objects", "Objects"], ["admin", "Maintenance & demo"]];
const viaText = (v) => ({ read: "read-path checksum", scrub: "background scrub",
                          anti_entropy: "inventory reconciliation" })[v] || v || "unknown";
const b = (s) => `<b class="mono">${esc(s)}</b>`;

function describe(e) {
  const n = e.node;
  switch (e.kind) {
    case "node_added": return { cat: "nodes", tone: "info", title: `Node ${b(n)} joined the cluster`,
                                detail: `${esc(e.zone)} · ${esc(e.addr)}` };
    case "node_health":
      if (e.new === "alive") return { cat: "nodes", tone: "ok", title: `Node ${b(n)} recovered`, detail: `health ${esc(e.old)} → alive` };
      if (e.new === "dead") return { cat: "nodes", tone: "bad", title: `Node ${b(n)} declared failed`, detail: `health ${esc(e.old)} → dead · its data is rebuilt elsewhere` };
      return { cat: "nodes", tone: "warn", title: `Node ${b(n)} stopped responding`, detail: `health ${esc(e.old)} → ${esc(e.new)}` };
    case "node_draining": return { cat: "admin", tone: "info", title: `Node ${b(n)} draining`, detail: "data is being moved off this node" };
    case "node_drained": return { cat: "admin", tone: "ok", title: `Node ${b(n)} fully drained`, detail: "safe to decommission" };
    case "node_removed": return { cat: "admin", tone: "bad", title: `Node ${b(n)} removed`, detail: "its replicas were written off" };
    case "node_active": return { cat: "admin", tone: "info", title: `Node ${b(n)} set active`, detail: "" };
    case "replica_missing": return { cat: "integrity", tone: "warn", title: `Missing replica on ${b(n)}`,
                                     detail: `${blobLabel(e.blob)} · detected by ${viaText(e.via)}` };
    case "replica_corrupt": return { cat: "integrity", tone: "bad", title: `Corrupt replica detected on ${b(n)}`,
                                     detail: `${blobLabel(e.blob)} · detected by ${viaText(e.via)} · quarantined` };
    case "repaired": {
      const shard = parseBlob(e.blob).idx != null;
      return { cat: "repair", tone: "ok",
               title: `Rebuilt ${shard ? "shard" : "replica"} on ${(e.to || []).map(b).join(", ")}`,
               detail: `${blobLabel(e.blob)} · ${marginText(e.margin)}` };
    }
    case "chunk_at_risk": return { cat: "repair", tone: "bad", title: "Chunk could not be repaired",
                                   detail: `${blobLabel(e.chunk)} · ${esc(e.error || "")}` };
    case "blob_at_risk": return { cat: "repair", tone: "bad", title: "Replica could not be rebuilt",
                                  detail: `${blobLabel(e.blob)} · ${esc(e.error || "")}` };
    case "maintenance_error": case "detector_error":
      return { cat: "admin", tone: "bad", title: e.kind.replace("_", " "), detail: esc(e.error) };
    // ---- operations observed by the dashboard adapter
    case "object_uploaded": return { cat: "objects", tone: "ok", title: `Uploaded ${b(e.key)}`, detail: `bucket ${esc(e.bucket)}` };
    case "upload_failed": return { cat: "objects", tone: "bad", title: `Upload of ${b(e.key)} failed`, detail: `HTTP ${e.status}` };
    case "object_downloaded": return { cat: "objects", tone: "info", title: `Downloaded ${b(e.key)}`, detail: `${fmtBytes(e.bytes)} · bucket ${esc(e.bucket)}` };
    case "download_failed": return { cat: "objects", tone: "bad", title: `Download of ${b(e.key)} failed`, detail: `HTTP ${e.status}` };
    case "object_deleted": return { cat: "objects", tone: "info", title: `Deleted ${b(e.key)}`, detail: `bucket ${esc(e.bucket)} · space reclaimed by GC` };
    case "delete_failed": return { cat: "objects", tone: "bad", title: `Delete of ${b(e.key)} failed`, detail: `HTTP ${e.status}` };
    case "bucket_created": return { cat: "objects", tone: "info", title: `Bucket ${b(e.bucket)} created`, detail: "" };
    case "bucket_create_failed": return { cat: "objects", tone: "bad", title: `Could not create bucket ${b(e.bucket)}`, detail: `HTTP ${e.status}` };
    case "object_verified": return e.ok
      ? { cat: "integrity", tone: "ok", title: `Verified ${b(e.key)}`, detail: "SHA-256 of downloaded bytes matches the checksum recorded at write" }
      : { cat: "integrity", tone: "bad", title: `Verification of ${b(e.key)} failed`, detail: esc(e.error) };
    case "fault_injected": {
      const f = e.faults || {};
      if (f.down === true) return { cat: "admin", tone: "bad", title: `Simulated failure of ${b(n)}`, detail: "node now drops every request (fault injection)" };
      if (f.down === false) return { cat: "admin", tone: "ok", title: `Restored ${b(n)}`, detail: "fault injection cleared" };
      return { cat: "admin", tone: "info", title: `Faults changed on ${b(n)}`, detail: esc(JSON.stringify(f)) };
    }
    case "replica_corrupted": return { cat: "admin", tone: "warn", title: `Injected bit rot on ${b(n)}`, detail: `${blobLabel(e.blob)} · one byte flipped on disk` };
    case "maintenance_pass": return { cat: "admin", tone: "info", title: "Maintenance pass run", detail: statsSummary(e.stats) };
    default:
      if (/^node_\w+_requested$/.test(e.kind)) return { cat: "admin", tone: "info", title: `${esc(e.kind.slice(5, -10))} requested for ${b(n)}`, detail: `HTTP ${e.status}` };
      return { cat: "admin", tone: "info", title: esc(e.kind), detail: "" };
  }
}
function marginText(m) {
  if (m == null) return "";
  if (m < 0) return "chunk was unreadable before repair";
  if (m === 0) return "no spare copies were left";
  return `${plural(m, "spare copy", "spare copies")} left before repair`;
}
const STAT_NAMES = {
  scrubbed: "blobs verified", corrupt_found: "corrupt found", replicas_created: "replicas created",
  shards_reconstructed: "shards reconstructed", replicas_moved: "replicas moved", replicas_trimmed: "surplus trimmed",
  blobs_collected: "blobs garbage-collected", orphans_deleted: "orphans deleted", replicas_rediscovered: "replicas rediscovered",
  replicas_lost: "replicas lost", unrecoverable_chunks: "unrecoverable chunks", unrecoverable_blobs: "unrecoverable blobs",
  nodes_drained: "nodes drained",
};
function statsSummary(stats) {
  const parts = Object.entries(stats || {}).filter(([, v]) => v).map(([k, v]) => `${STAT_NAMES[k] || k}: ${v}`);
  return parts.length ? esc(parts.join(" · ")) : "nothing needed doing";
}

// Vault events + dashboard-observed operations, newest first. Identical Vault
// events logged twice within a moment (e.g. two health probes racing) are merged.
function feedItems(snap) {
  const out = [];
  const vault = (snap.events || []).map((e) => ({ ...e, source: "vault" }));
  for (const e of [...vault, ...(snap.activity || [])]) out.push(e);
  out.sort((a, z) => z.ts - a.ts);
  const merged = [];
  for (const e of out) {
    const prev = merged[merged.length - 1];
    if (prev && prev.source === "vault" && e.source === "vault" && prev.kind === e.kind &&
        prev.node === e.node && prev.old === e.old && prev.new === e.new && prev.blob === e.blob &&
        Math.abs(prev.ts - e.ts) < 3) { prev.count = (prev.count || 1) + 1; continue; }
    merged.push({ ...e });
  }
  return merged;
}
function evHTML(e) {
  const d = describe(e);
  const id = `${e.source}|${e.kind}|${e.ts}`;
  const fresh = !state.firstRender && !state.seen.has(id);
  state.seen.add(id);
  return `<div class="ev${fresh ? " new" : ""}">
    <span class="dot ${d.tone}"></span>
    <div><div class="ev-title">${d.title}${e.count > 1 ? `<span class="x">×${e.count}</span>` : ""}${e.source === "dashboard" ? '<span class="src">dashboard</span>' : ""}${e.user ? `<span class="src">by ${esc(e.user)}</span>` : ""}</div>
    ${d.detail ? `<div class="ev-detail">${d.detail}</div>` : ""}</div>
    <div class="ev-time" title="${esc(new Date(e.ts * 1000).toString())}">${clock(e.ts)}<br>${agoSpan(e.ts)}</div>
  </div>`;
}
const feedHTML = (items, empty) => items.length ? items.map(evHTML).join("") : `<div class="empty">${empty}</div>`;

// ---------------------------------------------------------------- health model
function nodeState(n, live) {
  if (n.admin === "removed") return { cls: "", label: "removed" };
  if (n.admin === "drained") return { cls: "info", label: "drained" };
  if (n.health === "dead") return { cls: "bad", label: "failed" };
  if (n.health === "suspect") return { cls: "warn", label: "suspect" };
  if (n.admin === "draining") return { cls: "info", label: "draining" };
  return { cls: "ok", label: live && !live.reachable ? "alive*" : "healthy" };
}

function assess(snap) {
  if (!snap || !snap.gateway || !snap.gateway.ok) {
    return { level: "bad", state: "Offline", title: "Gateway unreachable",
             text: esc(snap?.gateway?.error || "The dashboard cannot reach the Vault gateway.") };
  }
  const c = snap.cluster, m = c.metadata;
  const nodes = c.nodes.filter((n) => n.admin !== "removed");
  const active = nodes.filter((n) => n.admin === "active");
  const alive = nodes.filter((n) => n.health === "alive").length;
  const suspect = nodes.filter((n) => n.health === "suspect").length;
  const dead = nodes.filter((n) => n.health === "dead").length;
  const zones = new Set(nodes.map((n) => n.zone)).size;
  const policies = (snap.buckets || []).map((bk) => policyInfo(bk.policy));
  const minTol = policies.length ? Math.min(...policies.map((p) => p.tol)) : null;
  const risk = (snap.events || []).filter((e) => (e.kind === "chunk_at_risk" || e.kind === "blob_at_risk") && now() - e.ts < 300);
  const base = { nodes, active, alive, suspect, dead, zones, minTol, policies, deficient: m.deficient_chunks };
  const within = minTol == null ? "" : dead <= minTol
    ? ` That is within every bucket's tolerance of ${plural(minTol, "failure")}.`
    : ` That exceeds the ${plural(minTol, "failure")} some buckets tolerate: objects there may be unreadable until nodes return.`;
  if (risk.length && m.deficient_chunks > 0) {
    return { ...base, level: "bad", state: "Data at risk", title: "Some chunks cannot be rebuilt yet",
             text: `${plural(risk.length, "repair")} failed in the last 5 minutes because too few intact copies were reachable. Bringing failed nodes back will let Vault finish recovery.` };
  }
  if (dead || suspect || m.deficient_chunks) {
    const bits = [];
    if (dead) bits.push(`${plural(dead, "node")} failed`);
    if (suspect) bits.push(`${suspect} suspect`);
    const def = m.deficient_chunks
      ? `${plural(m.deficient_chunks, "chunk")} ${m.deficient_chunks === 1 ? "is" : "are"} below target redundancy and being rebuilt automatically.`
      : "No chunk has dropped below its target redundancy.";
    return { ...base, level: "warn", state: "Degraded · self-healing", title: bits.length ? `${bits.join(", ")} — Vault is still serving` : "Rebuilding redundancy",
             text: `${def}${dead ? within : ""}` };
  }
  return { ...base, level: "ok", state: "Operational", title: "All data fully protected",
           text: `${plural(m.objects, "object")} stored across ${plural(alive, "node")} in ${plural(zones, "zone")}. Every chunk has its full set of copies, so Vault keeps serving reads and writes when nodes fail, and rebuilds lost copies automatically.` };
}

// ---------------------------------------------------------------- sampling
function sample(snap) {
  if (!snap.gateway?.ok) return;
  const c = snap.cluster, nodes = c.nodes.filter((n) => n.admin !== "removed");
  const s = { t: snap.fetched_at, deficient: c.metadata.deficient_chunks,
              alive: nodes.filter((n) => n.health === "alive").length, total: nodes.length };
  state.samples.push(s);
  if (state.samples.length > 900) state.samples.shift();
  if (s.deficient > 0) {
    if (!state.episode) state.episode = { start: s.t, peak: s.deficient };
    state.episode.peak = Math.max(state.episode.peak, s.deficient);
  } else if (state.episode) {
    state.lastEpisode = { ...state.episode, end: s.t };
    state.episode = null;
  }
}

function chartSVG(el, series, { key, max, color, label, step = false, fmt = (v) => v }) {
  const W = Math.max(240, el.clientWidth || 600), H = el.clientHeight || 150;
  if (series.length < 2) return `<div class="chart-empty">Collecting samples…</div>`;
  const pad = { l: 30, r: 8, t: 18, b: 16 };
  const t0 = series[0].t, t1 = series[series.length - 1].t, span = Math.max(1, t1 - t0);
  const ymax = Math.max(1, max ?? Math.max(...series.map((s) => s[key])));
  const x = (t) => pad.l + ((t - t0) / span) * (W - pad.l - pad.r);
  const y = (v) => H - pad.b - (v / ymax) * (H - pad.t - pad.b);
  let d = "";
  series.forEach((s, i) => {
    if (i === 0) d += `M${x(s.t)},${y(s[key])}`;
    else if (step) d += `H${x(s.t)}V${y(s[key])}`;
    else d += `L${x(s.t)},${y(s[key])}`;
  });
  const last = series[series.length - 1];
  const area = `${d}V${H - pad.b}H${pad.l}Z`;
  const id = `g${key}`;
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(label)}">
    <defs><linearGradient id="${id}" x1="0" x2="0" y1="0" y2="1"><stop offset="0" stop-color="${color}" stop-opacity=".35"/><stop offset="1" stop-color="${color}" stop-opacity="0"/></linearGradient></defs>
    <text class="title" x="${pad.l}" y="11">${esc(label)}</text>
    <line class="axis" x1="${pad.l}" x2="${W - pad.r}" y1="${H - pad.b}" y2="${H - pad.b}"/>
    <line class="axis" x1="${pad.l}" x2="${W - pad.r}" y1="${y(ymax)}" y2="${y(ymax)}" stroke-dasharray="2 4"/>
    <text class="lbl" x="${pad.l - 6}" y="${y(ymax) + 3}" text-anchor="end">${fmt(ymax)}</text>
    <text class="lbl" x="${pad.l - 6}" y="${H - pad.b + 3}" text-anchor="end">0</text>
    <path d="${area}" fill="url(#${id})"/>
    <path d="${d}" fill="none" stroke="${color}" stroke-width="1.6" stroke-linejoin="round"/>
    <circle cx="${x(last.t)}" cy="${y(last[key])}" r="3" fill="${color}"/>
    <text class="lbl" x="${pad.l}" y="${H - 3}">${clock(t0)}</text>
    <text class="lbl" x="${W - pad.r}" y="${H - 3}" text-anchor="end">now · ${fmt(last[key])}</text>
  </svg>`;
}

// ---------------------------------------------------------------- rendering
function renderChrome(snap, a) {
  const conn = $("#conn");
  const ok = snap?.gateway?.ok;
  conn.classList.toggle("live", !!ok);
  conn.classList.toggle("down", !ok);
  $("#conn-text").textContent = ok
    ? `Live${snap.gateway.url ? ` · ${snap.gateway.url.replace(/^https?:\/\//, "")}` : ""} · ${snap.gateway.latency_ms} ms`
    : "Gateway unreachable";
  const core = $(".brand-mark .core");
  if (core) core.style.fill = a.level === "ok" ? "var(--ok)" : a.level === "warn" ? "var(--warn)" : "var(--bad)";
  setHTML($("#rail-foot"), ok ? `${snap.gateway.url ? `gateway<br>${esc(snap.gateway.url)}<br>` : ""}refresh ${POLL_MS / 1000}s` : "");
  if (!ok) return;
  const down = a.dead + a.suspect;
  const bn = $("#b-nodes"); bn.textContent = down ? `${down} down` : a.nodes.length;
  bn.className = "badge" + (a.dead ? " bad" : a.suspect ? " warn" : "");
  $("#b-objects").textContent = snap.cluster.metadata.objects;
  const br = $("#b-repair"); br.textContent = a.deficient || ""; br.className = "badge" + (a.deficient ? " warn" : "");
  const recent = (snap.events || []).filter((e) => (e.kind === "replica_corrupt" || e.kind === "replica_missing") && now() - e.ts < 900).length;
  const bi = $("#b-integrity"); bi.textContent = recent || ""; bi.className = "badge" + (recent ? " warn" : "");
}

function renderOverview(snap, a) {
  const hero = $("#hero");
  hero.className = `hero ${a.level}`;
  let tol = "";
  if (a.minTol != null) {
    const segs = Array.from({ length: Math.max(a.minTol, 1) }, (_, i) =>
      `<span class="${i < a.dead ? "used" : "spare"}"></span>`).join("");
    tol = `<div class="hero-tol">
      <div class="big">${a.minTol}<small>node ${a.minTol === 1 ? "failure" : "failures"}</small></div>
      <div class="cap">can be survived at once without losing data (lowest tolerance across ${plural(a.policies.length, "bucket")}).
      ${a.dead ? `<b>${a.dead}</b> currently failed.` : "No nodes currently failed."}</div>
      <div class="meter" title="red = failed nodes, green = remaining tolerance">${segs}</div></div>`;
  } else if (snap.gateway?.ok) {
    tol = `<div class="hero-tol"><div class="cap">No buckets yet. Create one in <a href="#objects">Objects</a> to choose a durability policy.</div></div>`;
  }
  setHTML(hero, `<div><span class="hero-state"><i></i>${esc(a.state)}</span><h1>${a.title}</h1><p>${a.text}</p></div>${tol}`);
  if (!snap.gateway?.ok) {
    ["#topology", "#kpis", "#policies", "#recent"].forEach((s) => setHTML($(s), ""));
    return;
  }
  const c = snap.cluster, m = c.metadata, live = snap.nodes_live || {};

  // topology
  const zones = {};
  a.nodes.forEach((n) => (zones[n.zone] ||= []).push(n));
  setHTML($("#topology"), Object.keys(zones).sort().map((z) => {
    const ns = zones[z];
    return `<div class="zone"><div class="zone-label"><span>${esc(z)}</span><span>${ns.filter((n) => n.health === "alive").length}/${ns.length}</span></div>
      <div class="zone-nodes">${ns.map((n) => {
        const st = nodeState(n, live[n.id]);
        return `<a class="tile ${st.cls}" href="#nodes" title="${esc(n.id)} · ${esc(st.label)}">
          <div class="tile-id">${esc(n.id)}</div><div class="tile-state">${esc(st.label)}</div>
          <div class="tile-meta">${n.replicas} replicas</div></a>`;
      }).join("")}</div></div>`;
  }).join(""));

  // KPIs
  const repaired10 = (snap.events || []).filter((e) => e.kind === "repaired" && now() - e.ts < 600).length;
  const overhead = m.logical_bytes ? (m.physical_bytes / m.logical_bytes).toFixed(2) : null;
  setHTML($("#kpis"), [
    kpi("Storage nodes", `${a.alive}/${a.nodes.length}`, a.dead ? "bad" : a.suspect ? "warn" : "ok",
        `<div class="split"><span><i class="dot ok"></i>${a.alive}</span><span><i class="dot warn"></i>${a.suspect}</span><span><i class="dot bad"></i>${a.dead}</span></div>`),
    kpi("Objects stored", m.objects, "", `in ${plural(m.buckets, "bucket")}`),
    kpi("Data stored", fmtBytes(m.logical_bytes), "", overhead ? `${fmtBytes(m.physical_bytes)} on disk · ${overhead}× overhead` : "nothing stored yet"),
    kpi("Below target redundancy", m.deficient_chunks, m.deficient_chunks ? "warn" : "ok",
        m.deficient_chunks ? "chunks awaiting automatic repair" : "every chunk fully replicated"),
    kpi("Repairs completed", repaired10, "", "in the last 10 min (event log)"),
    kpi("Replicas & shards", m.replicas, "", `${plural(m.blobs, "stored chunk/shard", "stored chunks/shards")}`),
  ].join(""));

  // charts (samples taken by this page)
  const s0 = state.samples[0];
  $("#chart-note").textContent = s0 ? `Sampled by this dashboard every ${POLL_MS / 1000}s since ${clock(s0.t)}.` : "";
  const total = Math.max(...state.samples.map((s) => s.total), 1);
  setHTML($("#chart-redundancy"), chartSVG($("#chart-redundancy"), state.samples,
    { key: "deficient", color: "#fbbf24", label: "Chunks below target redundancy" }));
  setHTML($("#chart-nodes"), chartSVG($("#chart-nodes"), state.samples,
    { key: "alive", max: total, color: "#34d399", label: "Nodes alive", step: true }));

  // policies
  const active = a.active.filter((n) => n.health !== "dead").length;
  setHTML($("#policies"), (snap.buckets || []).length ? snap.buckets.map((bk) => {
    const p = policyInfo(bk.policy);
    const cells = Array.from({ length: p.width }, (_, i) => `<i class="${i >= p.data ? "p" : ""}"></i>`).join("");
    const warn = active < p.width ? ` · <span style="color:var(--warn)">only ${active} usable nodes for ${p.width}-wide layout</span>` : "";
    return `<div class="policy"><div><span class="policy-name">${esc(bk.name)}</span> <span class="pill info">${p.label}</span></div>
      <div class="policy-tol">${p.tol}<small>failures tolerated</small></div>
      <div class="policy-desc">${esc(p.detail)} · ${p.overhead.toFixed(2)}× storage${warn}</div>
      <div class="shards" title="${p.parity ? "blue = data shards, amber = parity" : "one block per full copy"}">${cells}</div></div>`;
  }).join("") : `<div class="empty">No buckets yet.</div>`);

  setHTML($("#recent"), feedHTML(feedItems(snap).slice(0, 8), "No events yet."));
}

function kpi(label, value, tone, sub) {
  return `<div class="kpi ${tone}"><div class="kpi-label">${label}</div><div class="kpi-value">${esc(value)}</div>${
    sub?.startsWith("<") ? sub : `<div class="kpi-sub">${sub || ""}</div>`}</div>`;
}

function renderNodes(snap, a) {
  if (!snap.gateway?.ok) return setHTML($("#node-grid"), `<div class="empty">Gateway unreachable.</div>`);
  const live = snap.nodes_live || {};
  const feed = feedItems(snap);
  const maxBytes = Math.max(1, ...Object.values(live).map((l) => l.health?.bytes || 0));
  const zones = {};
  a.nodes.forEach((n) => (zones[n.zone] ||= []).push(n));
  setHTML($("#node-grid"), Object.keys(zones).sort().map((z) => `<div><h3 class="zone-title">${esc(z)}</h3><div class="node-grid">${
    zones[z].map((n) => {
      const l = live[n.id] || {}, st = nodeState(n, l), h = l.health;
      const simulated = l.faults?.down;
      const recent = feed.filter((e) => now() - e.ts < 60 &&
        (e.node === n.id || (e.kind === "repaired" && (e.to || []).includes(n.id))));
      const received = recent.filter((e) => e.kind === "repaired").length;
      const lines = [];
      if (simulated) lines.push("Failure simulated: node is dropping all requests");
      if (received) lines.push(`Received ${plural(received, "rebuilt copy", "rebuilt copies")} in the last minute`);
      recent.filter((e) => e.kind !== "repaired" && e.kind !== "fault_injected").slice(0, 2)
        .forEach((e) => lines.push(describe(e).title.replace(/<[^>]+>/g, "")));
      const act = lines.length ? esc(lines.join(" · ")) : "No events involving this node in the last 60 s";
      return `<article class="node ${st.cls}">
        <div class="node-top"><div><div class="node-id">${esc(n.id)}${simulated ? '<span class="tag demo">simulated</span>' : ""}</div>
          <div class="node-addr">${n.addr ? `${esc(n.addr)} · ` : ""}weight ${n.weight}</div></div>
          <span class="state"><i></i>${esc(st.label)}</span></div>
        <div class="stats">
          <div><div class="stat-label">Replicas tracked</div><div class="stat-value">${n.replicas}</div></div>
          <div><div class="stat-label">On disk</div><div class="stat-value">${h ? `${fmtBytes(h.bytes)} · ${plural(h.blobs, "blob")}` : "unreachable"}</div></div>
          <div class="bar" title="bytes on disk relative to the fullest node (capacity is not reported)"><span style="width:${h ? Math.round((h.bytes / maxBytes) * 100) : 0}%"></span></div>
          <div><div class="stat-label">Last heartbeat</div><div class="stat-value"><span data-hb="${esc(n.id)}"></span></div></div>
          <div><div class="stat-label">Direct probe</div><div class="stat-value">${l.reachable ? `<span data-lat="${esc(n.id)}"></span>` : "no answer"}</div></div>
        </div>
        <div class="activity-line">${act}</div>
        <div class="node-actions admin-only">
          ${simulated
            ? `<button class="btn sm ok" data-action="restore" data-node="${esc(n.id)}">Restore node</button>`
            : `<button class="btn sm danger" data-action="fail" data-node="${esc(n.id)}" ${l.faults ? "" : "disabled"}>Simulate failure</button>`}
          <button class="btn sm ghost" data-action="corrupt" data-node="${esc(n.id)}" ${l.reachable && h?.blobs ? "" : "disabled"} title="Flip one byte in a random replica on this node's disk">Corrupt a replica</button>
          <button class="btn sm ghost" data-action="drain" data-node="${esc(n.id)}" ${n.admin === "active" ? "" : "disabled"}>Drain</button>
        </div></article>`;
    }).join("")}</div></div>`).join(""));
}

function renderObjects(snap) {
  const buckets = snap.buckets || [];
  if (!snap.gateway?.ok) return;
  if (!buckets.length) {
    setHTML($("#bucket-bar"), `<div class="callout" style="flex:1">No buckets yet.${state.session?.role === "admin" ? " Create one with a durability policy:" : " An admin can create one."}
      <div class="form-actions admin-only"><button class="btn sm" data-preset="replicate">Create “files” · 3 copies</button>
      <button class="btn sm ghost" data-preset="erasure">Create “archive” · erasure 4+2</button></div></div>`);
    $("#upload-panel").hidden = true;
    setHTML($("#objects-table"), "");
    $("#objects-title").textContent = "Objects";
    return;
  }
  if (!buckets.some((bk) => bk.name === state.bucket)) { selectBucket(buckets[0].name, false); loadObjects(); }
  $("#upload-panel").hidden = false;
  setHTML($("#bucket-bar"), buckets.map((bk) => {
    const p = policyInfo(bk.policy);
    return `<button class="bucket${bk.name === state.bucket ? " active" : ""}" data-bucket="${esc(bk.name)}" type="button">
      <b>${esc(bk.name)}</b><div>${p.label} · survives ${plural(p.tol, "failure")}</div></button>`;
  }).join(""));
  const cur = buckets.find((bk) => bk.name === state.bucket);
  const p = policyInfo(cur.policy);
  setHTML($("#upload-target"), `into <b class="mono">${esc(cur.name)}</b> · ${p.label}, ${p.overhead.toFixed(2)}× storage, survives ${plural(p.tol, "node failure")}`);
  renderObjectTable(cur, p);
}

function renderObjectTable(bucket, p) {
  const title = $("#objects-title");
  if (state.objectsFor !== bucket.name) {
    title.textContent = `Objects in ${bucket.name}`;
    return setHTML($("#objects-table"), `<tr><td class="empty">Loading…</td></tr>`);
  }
  if (state.objectsError) return setHTML($("#objects-table"), `<tr><td class="empty">Could not list objects: ${esc(state.objectsError)}</td></tr>`);
  const q = ($("#object-filter").value || "").toLowerCase();
  const rows = state.objects.filter((o) => o.key.toLowerCase().includes(q));
  title.textContent = `Objects in ${bucket.name} (${state.objects.length}${state.objects.length >= 1000 ? "+" : ""})`;
  if (!rows.length) return setHTML($("#objects-table"), `<tr><td class="empty">${state.objects.length ? "No objects match the filter." : "This bucket is empty. Drop a file above to store it."}</td></tr>`);
  const ver = state.snap.verifications || {};
  setHTML($("#objects-table"), `<thead><tr><th>Key</th><th>Size</th><th>Status</th><th>Durability</th><th>Integrity</th><th></th></tr></thead><tbody>${
    rows.map((o) => {
      const v = ver[`${bucket.name}/${o.key}`];
      let integ = `<span class="pill">not verified</span>`;
      if (v && v.version && v.version !== o.version) integ = `<span class="pill">changed since check</span>`;
      else if (v) integ = v.ok
        ? `<span class="pill ok" title="SHA-256 ${esc(v.actual)}">✓ verified ${agoSpan(v.ts)}</span>`
        : `<span class="pill bad" title="${esc(v.error)}">✗ ${esc(v.error || "failed")}</span>`;
      const href = `/api/gw/buckets/${enc(bucket.name)}/${enc(o.key)}?dl=1`;
      return `<tr><td class="key">${esc(o.key)}<div class="sub" title="SHA-256 recorded at write">${o.etag.slice(0, 16)}…</div></td>
        <td class="num">${fmtBytes(o.size)}</td>
        <td><span class="pill ok">stored</span><div class="sub">v${o.version} · ${agoSpan(o.created)}</div></td>
        <td><span class="pill info">${p.label}</span><div class="sub">survives ${plural(p.tol, "failure")}</div></td>
        <td>${integ}</td>
        <td class="actions"><a class="btn sm ghost" href="${href}" download>Download</a><button class="btn sm ghost" data-verify="${esc(o.key)}">Verify</button><button class="btn sm danger admin-only" data-delete="${esc(o.key)}">Delete</button></td></tr>`;
    }).join("")}</tbody>`);
}

function renderRepair(snap, a) {
  if (!snap.gateway?.ok) return;
  const ev = snap.events || [];
  const repaired = ev.filter((e) => e.kind === "repaired");
  const risk = ev.filter((e) => e.kind === "chunk_at_risk" || e.kind === "blob_at_risk");
  const rate = repaired.filter((e) => now() - e.ts < 60).length;
  setHTML($("#repair-kpis"), [
    kpi("Below target now", a.deficient, a.deficient ? "warn" : "ok", a.deficient ? "chunks queued for repair" : "nothing to repair"),
    kpi("Repairs in the last 10 min", repaired.filter((e) => now() - e.ts < 600).length, "", "replicas/shards rebuilt"),
    kpi("Repair rate", `${rate}/min`, "", "rebuilt in the last 60 s"),
    kpi("Failed repairs", risk.length, risk.length ? "bad" : "ok", "in the retrieved event log"),
  ].join(""));

  let rec;
  const ep = state.episode, last = state.lastEpisode;
  if (ep) {
    const done = ep.peak - a.deficient, pct = Math.round((done / ep.peak) * 100);
    rec = `<div class="panel-head"><h2>Recovery in progress</h2><p class="hint">Observed by this dashboard: the count of chunks below target peaked at ${ep.peak}; ${a.deficient} remain.</p></div>
      <div class="progress-wrap"><div class="num" style="font-size:20px">${done} / ${ep.peak} chunks restored</div><div class="num">${pct}% · started ${agoSpan(ep.start)}</div>
      <div class="progress"><span style="width:${pct}%"></span></div></div>`;
  } else if (last) {
    rec = `<div class="panel-head"><h2>Last recovery</h2><p class="hint">Observed by this dashboard.</p></div>
      <div class="progress-wrap"><div class="num" style="font-size:20px">${last.peak} / ${last.peak} chunks restored in ${Math.max(1, Math.round(last.end - last.start))} s</div><div class="num">finished ${agoSpan(last.end)}</div>
      <div class="progress done"><span style="width:100%"></span></div></div>`;
  } else {
    rec = `<div class="panel-head"><h2>Recovery</h2></div><div class="empty">No chunk has been below target redundancy since this page was opened.</div>`;
  }
  setHTML($("#recovery-panel"), rec);

  setHTML($("#repairs-table"), repaired.length ? `<thead><tr><th>Time</th><th>Rebuilt</th><th>Placed on</th><th>Spare before</th></tr></thead><tbody>${
    repaired.slice(0, 60).map((e) => `<tr><td class="num">${clock(e.ts)}</td><td>${blobLabel(e.blob)}</td>
      <td class="mono">${(e.to || []).map(esc).join(", ")}</td><td class="num">${e.margin}</td></tr>`).join("")}</tbody>`
    : `<tr><td class="empty">No repairs in the event log yet.</td></tr>`);
  setHTML($("#risk-list"), feedHTML(risk.map((e) => ({ ...e, source: "vault" })), "No failed repairs. ✓"));
  const lm = snap.last_maintenance;
  setHTML($("#maint-result"), lm ? `<div class="hint" style="margin-bottom:10px">Run ${agoSpan(lm.ts)} (${clock(lm.ts)})</div>
    <div class="kv">${Object.entries(lm.stats).map(([k, v]) => `<div><b>${v}</b>${esc(STAT_NAMES[k] || k)}</div>`).join("") || "<div>Nothing needed doing.</div>"}</div>`
    : `<div class="empty">Not run from this dashboard yet. Vault also runs maintenance in the background.</div>`);
}

function renderIntegrity(snap) {
  if (!snap.gateway?.ok) return;
  const ev = snap.events || [];
  const detections = ev.filter((e) => e.kind === "replica_corrupt" || e.kind === "replica_missing");
  const repairedAt = (blob, ts) => ev.filter((e) => e.kind === "repaired" && e.blob === blob && e.ts >= ts)
    .reduce((m, e) => (m && m.ts < e.ts ? m : e), null);
  const fixed = detections.filter((d) => repairedAt(d.blob, d.ts)).length;
  const g = snap.cluster.gateway || {};
  const ver = Object.values(snap.verifications || {});
  setHTML($("#integrity-kpis"), [
    kpi("Corrupt replicas detected", detections.filter((e) => e.kind === "replica_corrupt").length,
        "", "in the retrieved event log"),
    kpi("Missing replicas detected", detections.filter((e) => e.kind === "replica_missing").length, "", "lost disks, vanished files"),
    kpi("Detections since repaired", `${fixed}/${detections.length}`, detections.length && fixed < detections.length ? "warn" : "ok",
        "a rebuild of the same chunk followed"),
    kpi("Read-path catches", `${g.replicas_corrupt || 0} / ${g.replicas_missing || 0}`, "", "corrupt / missing, gateway counters since start"),
    kpi("Objects verified here", `${ver.filter((v) => v.ok).length}/${ver.length}`, ver.some((v) => !v.ok) ? "bad" : "", "end-to-end checks from this dashboard"),
  ].join(""));
  setHTML($("#corrupt-table"), detections.length ? `<thead><tr><th>Time</th><th>Node</th><th>What</th><th>Detected by</th><th>Outcome</th></tr></thead><tbody>${
    detections.slice(0, 60).map((d) => {
      const r = repairedAt(d.blob, d.ts);
      return `<tr><td class="num">${clock(d.ts)}</td><td class="mono">${esc(d.node)}</td>
        <td><span class="pill ${d.kind === "replica_corrupt" ? "bad" : "warn"}">${d.kind === "replica_corrupt" ? "corrupt" : "missing"}</span><div class="sub">${blobLabel(d.blob)}</div></td>
        <td>${esc(viaText(d.via))}</td>
        <td>${r ? `<span class="pill ok">✓ rebuilt on ${esc((r.to || []).join(", "))}</span>` : `<span class="pill warn">pending repair</span>`}</td></tr>`;
    }).join("")}</tbody>` : `<tr><td class="empty">No corruption or missing replicas detected. ✓</td></tr>`);
  const items = feedItems(snap).filter((e) => ["object_verified", "maintenance_pass", "replica_corrupted"].includes(e.kind));
  setHTML($("#verify-feed"), feedHTML(items.slice(0, 30), "No verification run from this dashboard yet."));
}

function renderActivity(snap) {
  const items = feedItems(snap);
  const counts = {};
  items.forEach((e) => { const c = describe(e).cat; counts[c] = (counts[c] || 0) + 1; });
  setHTML($("#filters"), CATS.map(([k, label]) =>
    `<button class="chip${state.filter === k ? " active" : ""}" data-filter="${k}">${label}<span class="num">${k === "all" ? items.length : counts[k] || 0}</span></button>`).join(""));
  const shown = state.filter === "all" ? items : items.filter((e) => describe(e).cat === state.filter);
  setHTML($("#feed"), feedHTML(shown, "No events in this category."));
}

function render() {
  const snap = state.snap;
  if (!snap) return;
  const a = assess(snap);
  renderChrome(snap, a);
  renderOverview(snap, a);
  const v = state.view;
  if (snap.gateway?.ok) {
    if (v === "nodes") renderNodes(snap, a);
    if (v === "objects") renderObjects(snap);
    if (v === "repair") renderRepair(snap, a);
    if (v === "integrity") renderIntegrity(snap);
    if (v === "activity") renderActivity(snap);
  }
  // Mark everything rendered so far as seen; later arrivals flash once.
  if (state.firstRender) { feedItems(snap).forEach((e) => state.seen.add(`${e.source}|${e.kind}|${e.ts}`)); }
  state.firstRender = false;
  tick();
}

// ---------------------------------------------------------------- data
function authHeaders(method, headers = {}) {
  // State-changing requests carry the session's CSRF token (checked server-side).
  return method === "GET" || method === "HEAD" ? headers : { ...headers, "X-CSRF-Token": state.session?.csrf || "" };
}

function toLogin() { location.replace("/login"); }

async function api(method, path, body, headers) {
  const r = await fetch(path, { method, body, headers: authHeaders(method, headers), cache: "no-store" });
  if (r.status === 401) { toLogin(); throw new Error("Signed out"); }
  const text = await r.text();
  let j = null;
  try { j = JSON.parse(text); } catch { /* not JSON */ }
  if (r.status === 403) throw new Error(j?.error === "admin access required" ? "This needs an admin account." : (j?.error || "Not allowed"));
  if (!r.ok) throw new Error(j?.detail || j?.error || `HTTP ${r.status}`);
  return j;
}

async function loadSession() {
  const r = await fetch("/api/session", { cache: "no-store" });
  if (!r.ok) { toLogin(); throw new Error("login required"); }
  state.session = await r.json();
  document.body.dataset.role = state.session.role;
  $("#user-name").textContent = state.session.username;
  const role = $("#user-role");
  role.textContent = state.session.role;
  role.classList.toggle("admin", state.session.role === "admin");
  $("#user-chip").hidden = false;
}

$("#logout").addEventListener("click", async () => {
  try { await fetch("/api/logout", { method: "POST", headers: authHeaders("POST"), cache: "no-store" }); }
  finally { toLogin(); }
});

async function poll() {
  if (state.inflight) return;
  state.inflight = true;
  try {
    const snap = await api("GET", "/api/snapshot");
    state.skew = snap.fetched_at - Date.now() / 1000;
    state.snap = snap;
    sample(snap);
  } catch (e) {
    state.snap = { gateway: { ok: false, error: `Dashboard server unreachable (${e.message})` } };
  } finally {
    state.inflight = false;
  }
  render();
}

async function loadObjects() {
  const bucket = state.bucket;
  if (!bucket || !state.snap?.gateway?.ok) return;
  try {
    const list = await api("GET", `/api/gw/buckets/${enc(bucket)}?limit=1000`);
    if (bucket !== state.bucket) return;
    state.objects = list; state.objectsError = null;
  } catch (e) {
    state.objects = []; state.objectsError = e.message;
  }
  state.objectsFor = bucket;
  if (state.view === "objects") { renderObjects(state.snap); tick(); }
}

function selectBucket(name, refresh = true) {
  state.bucket = name;
  localStorage.setItem("vault.bucket", name);
  state.objectsFor = null;
  if (refresh) { renderObjects(state.snap); loadObjects(); }
}

// ---------------------------------------------------------------- ui actions
function toast(msg, tone = "") {
  const el = document.createElement("div");
  el.className = `toast ${tone}`;
  el.innerHTML = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), 5200);
}

async function withButton(btn, fn) {
  if (btn) btn.disabled = true;
  try { await fn(); } catch (e) { toast(`✗ ${esc(e.message)}`, "bad"); }
  finally { if (btn) btn.disabled = false; poll(); }
}

$("#node-grid").addEventListener("click", (ev) => {
  const btn = ev.target.closest("button[data-action]");
  if (!btn) return;
  const id = btn.dataset.node, action = btn.dataset.action;
  withButton(btn, async () => {
    if (action === "fail") {
      await api("POST", `/api/nodes/${enc(id)}/faults`, JSON.stringify({ down: true }), { "Content-Type": "application/json" });
      toast(`<b>${esc(id)}</b> is now dropping every request. Watch Vault mark it suspect, then failed, and rebuild its data.`);
    } else if (action === "restore") {
      await api("POST", `/api/nodes/${enc(id)}/faults`, JSON.stringify({ down: false, latency: 0 }), { "Content-Type": "application/json" });
      toast(`<b>${esc(id)}</b> restored. Vault will mark it alive on the next heartbeat.`, "ok");
    } else if (action === "corrupt") {
      const r = await api("POST", `/api/nodes/${enc(id)}/corrupt-random`);
      toast(r.corrupted ? `Flipped one byte in ${blobLabel(r.corrupted)} on <b>${esc(id)}</b>. Vault catches it on the next read or scrub (try Integrity → Run verification pass).`
                        : `Nothing to corrupt on ${esc(id)}.`);
    } else if (action === "drain") {
      if (!confirm(`Drain ${id}? Vault will copy all of its data to other nodes, then stop using it.`)) return;
      await api("POST", `/api/gw/cluster/nodes/${enc(id)}/drain`);
      toast(`Draining <b>${esc(id)}</b>.`);
    }
  });
});

$("#bucket-bar").addEventListener("click", (ev) => {
  const bk = ev.target.closest("[data-bucket]");
  if (bk) return selectBucket(bk.dataset.bucket);
  const preset = ev.target.closest("[data-preset]");
  if (preset) {
    const erasure = preset.dataset.preset === "erasure";
    const name = erasure ? "archive" : "files";
    withButton(preset, async () => {
      await api("PUT", `/api/gw/buckets/${enc(name)}`, JSON.stringify(erasure ? { scheme: "erasure", k: 4, m: 2 } : { scheme: "replicate", n: 3 }),
                { "Content-Type": "application/json" });
      toast(`Bucket <b>${name}</b> created.`, "ok");
      selectBucket(name);
    });
  }
});

$("#objects-table").addEventListener("click", (ev) => {
  const vb = ev.target.closest("[data-verify]");
  const db = ev.target.closest("[data-delete]");
  const bucket = state.bucket;
  if (vb) withButton(vb, async () => {
    const key = vb.dataset.verify;
    vb.textContent = "Verifying…";
    const r = await api("POST", `/api/verify/${enc(bucket)}/${enc(key)}`);
    toast(r.ok ? `✓ <b>${esc(key)}</b> intact: ${fmtBytes(r.bytes)} read back through Vault, SHA-256 matches (${r.ms} ms).`
               : `✗ <b>${esc(key)}</b> failed verification: ${esc(r.error)}`, r.ok ? "ok" : "bad");
  });
  if (db) {
    const key = db.dataset.delete;
    if (!confirm(`Delete ${key} from ${bucket}?`)) return;
    withButton(db, async () => {
      await api("DELETE", `/api/gw/buckets/${enc(bucket)}/${enc(key)}`);
      toast(`Deleted <b>${esc(key)}</b>.`);
      await loadObjects();
    });
  }
});

$("#object-filter").addEventListener("input", () => state.snap && renderObjects(state.snap));

// uploads
const drop = $("#drop");
["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => uploadFiles([...e.dataTransfer.files]));
$("#file-input").addEventListener("change", (e) => { uploadFiles([...e.target.files]); e.target.value = ""; });

function uploadOne(bucket, key, file, row) {
  return new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", `/api/gw/buckets/${enc(bucket)}/${enc(key)}`);
    xhr.setRequestHeader("X-CSRF-Token", state.session?.csrf || "");
    xhr.upload.onprogress = (e) => { if (e.lengthComputable) $(".pbar span", row).style.width = `${(e.loaded / e.total) * 100}%`; };
    xhr.onload = () => {
      let j = null; try { j = JSON.parse(xhr.responseText); } catch { /* ignore */ }
      $(".pbar span", row).style.width = "100%";
      if (xhr.status === 200) {
        row.classList.add("done");
        $(".msg", row).textContent = `stored · v${j.version} · sha256 ${j.etag.slice(0, 10)}…`;
      } else {
        row.classList.add("fail");
        $(".msg", row).textContent = j?.detail || j?.error || `HTTP ${xhr.status}`;
      }
      if (xhr.status === 401) toLogin();
      resolve(xhr.status === 200);
    };
    xhr.onerror = () => { row.classList.add("fail"); $(".msg", row).textContent = "network error"; resolve(false); };
    xhr.send(file);
  });
}

async function uploadFiles(files) {
  const bucket = state.bucket;
  if (!bucket || !files.length) return;
  const prefix = $("#key-prefix").value.trim();
  const list = $("#uploads");
  let ok = 0;
  for (const f of files) {
    const key = prefix + f.name;
    const row = document.createElement("div");
    row.className = "up";
    row.innerHTML = `<div class="name" title="${esc(key)}">${esc(key)}</div><div class="pbar"><span style="width:0"></span></div><div class="msg">${fmtBytes(f.size)} · uploading…</div>`;
    list.prepend(row);
    if (await uploadOne(bucket, key, f, row)) ok++;
  }
  toast(`${ok}/${files.length} file${files.length > 1 ? "s" : ""} stored in <b>${esc(bucket)}</b>.`, ok === files.length ? "ok" : "bad");
  setTimeout(() => { while (list.children.length > 6) list.lastChild.remove(); }, 8000);
  await loadObjects();
  poll();
}

// bucket form
const form = $("#bucket-form");
function syncPolicyForm() {
  const scheme = form.scheme.value;
  $$("[data-for]", form).forEach((el) => { el.hidden = el.dataset.for !== scheme; });
  const p = scheme === "replicate" ? policyInfo({ scheme, n: +form.n.value, w: Math.floor(+form.n.value / 2) + 1 })
    : policyInfo({ scheme, k: +form.k.value, m: +form.m.value, w: Math.min(+form.k.value + 1, +form.k.value + +form.m.value) });
  $("#policy-preview").textContent = p ? `${p.label}: survives ${plural(p.tol, "node failure")} at ${p.overhead.toFixed(2)}× storage; needs at least ${p.width} nodes to spread fully.` : "";
}
form.addEventListener("input", syncPolicyForm);
$("#new-bucket-toggle").addEventListener("click", () => { form.hidden = !form.hidden; syncPolicyForm(); if (!form.hidden) form.name.focus(); });
$("#bucket-cancel").addEventListener("click", () => { form.hidden = true; });
form.addEventListener("submit", (e) => {
  e.preventDefault();
  const scheme = form.scheme.value, name = form.name.value.trim();
  const body = scheme === "replicate" ? { scheme, n: +form.n.value } : { scheme, k: +form.k.value, m: +form.m.value };
  withButton($("button[type=submit]", form), async () => {
    await api("PUT", `/api/gw/buckets/${enc(name)}`, JSON.stringify(body), { "Content-Type": "application/json" });
    toast(`Bucket <b>${esc(name)}</b> created.`, "ok");
    form.hidden = true; form.reset();
    await poll();
    selectBucket(name);
  });
});

// maintenance
for (const id of ["#run-maint", "#run-scrub"]) {
  const btn = $(id), label = btn.textContent;
  btn.addEventListener("click", () => withButton(btn, async () => {
    btn.textContent = "Running…";
    try {
      const stats = await api("POST", "/api/gw/cluster/maintenance");
      toast(`Maintenance pass finished: ${statsSummary(stats)}.`, stats.corrupt_found ? "bad" : "ok");
    } finally {
      btn.textContent = label;
    }
  }));
}

$("#filters").addEventListener("click", (e) => {
  const c = e.target.closest("[data-filter]");
  if (c) { state.filter = c.dataset.filter; renderActivity(state.snap); }
});

// ---------------------------------------------------------------- routing & timers
function route() {
  const v = (location.hash || "#overview").slice(1);
  state.view = ["overview", "nodes", "objects", "repair", "integrity", "activity"].includes(v) ? v : "overview";
  $$(".view").forEach((s) => { s.hidden = s.dataset.view !== state.view; });
  $$("[data-nav]").forEach((a) => a.classList.toggle("active", a.dataset.nav === state.view));
  render();
  if (state.view === "objects") loadObjects();
  window.scrollTo({ top: 0 });
}
window.addEventListener("hashchange", route);
window.addEventListener("resize", () => { $("#chart-redundancy")._h = null; $("#chart-nodes")._h = null; render(); });

setInterval(tick, 500);
setInterval(poll, POLL_MS);
setInterval(() => { if (state.view === "objects") loadObjects(); }, OBJECTS_MS);
loadSession().then(() => poll()).then(route).catch(() => { /* redirected to login */ });
