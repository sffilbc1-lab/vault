// Ask Vault: optional explanatory assistant. It only appears when the site is
// served by assistant/server.py (which provides /api/ask); served any other way,
// the website is unchanged.
//
// Answers arrive as typed blocks and are rendered with textContent only, so no
// server or model text is ever interpreted as HTML.

const reduced = matchMedia("(prefers-reduced-motion: reduce)");
const $ = (s, el = document) => el.querySelector(s);

const SECTION_TITLES = {
  top: "The life of one file",
  how: "01 · Machines fail",
  distribution: "02 · One file, many nodes",
  failure: "03 · Failure is expected",
  recovery: "04 · Detect. Repair. Restore.",
  architecture: "05 · How it's built",
  live: "06 · Running for real",
  engineering: "07 · Engineering",
};
const SUGGESTED = [
  "What is Vault?",
  "What happens when a node fails?",
  "How does redundancy work?",
  "What is erasure coding?",
  "How does Vault detect corruption?",
  "What am I looking at in this storage map?",
];
const HISTORY_TURNS = 3;

function h(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k === "text") e.textContent = v;
    else e.setAttribute(k, v);
  }
  for (const c of children) if (c) e.append(c);
  return e;
}

// ------------------------------------------------------------------ page context
// Which section is under the middle of the viewport, plus small bits of state the
// illustrations expose. Sent with each question so "what is this?" has a subject.
function currentSection() {
  const mid = innerHeight * 0.45;
  for (const s of document.querySelectorAll("main > section[id]")) {
    const r = s.getBoundingClientRect();
    if (r.top <= mid && r.bottom >= mid) return s.id;
  }
  return null;
}
function pageContext() {
  const ctx = {};
  const section = currentSection();
  if (section && SECTION_TITLES[section]) ctx.section = section;
  const stage = $("#life-steps li.now button")?.textContent?.trim();
  if (section === "top" && stage) ctx.stage = stage;
  const policy = $("[data-policy][aria-selected='true']")?.dataset.policy;
  if (section === "distribution" && policy) ctx.policy = policy;
  const reachable = parseInt($("#fail-count")?.textContent ?? "", 10);
  if (section === "failure" && Number.isInteger(reachable)) ctx.reachable = reachable;
  return ctx;
}

// ------------------------------------------------------------------ rendering
function renderBlocks(blocks) {
  const box = h("div", { class: "ask-a" });
  for (const b of blocks || []) {
    if (b.type === "p") box.append(h("p", { text: b.text }));
    else if (b.type === "h") box.append(h("p", { class: "ask-h", text: b.text }));
    else if (b.type === "list") box.append(h("ul", {}, ...b.items.map((t) => h("li", { text: t }))));
    else if (b.type === "note") box.append(h("p", { class: "ask-note", text: b.text }));
    else if (b.type === "context") box.append(h("p", { class: "ask-tag", text: `About · ${b.text}` }));
    else if (b.type === "status") box.append(h("p", { class: "ask-tag warn", text: b.text }));
    else if (b.type === "live") box.append(h("p", { class: "ask-tag live", text: b.text }));
  }
  return box;
}
function plainText(blocks) {
  return (blocks || []).map((b) => (b.items ? b.items.map((i) => `- ${i}`).join("\n") : b.text)).join("\n");
}

// ------------------------------------------------------------------ widget
async function init() {
  let status;
  try {
    const r = await fetch("/api/ask/status", { cache: "no-store" });
    if (!r.ok) return;                      // plain static hosting: no assistant
    status = await r.json();
  } catch { return; }

  const launch = h("button", { class: "ask-launch", type: "button", "aria-expanded": "false",
                               "aria-controls": "ask-panel" });
  launch.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="2" y="2" width="6" height="6" rx="1.2"/><rect x="16" y="2" width="6" height="6" rx="1.2"/><rect x="9" y="9" width="6" height="6" rx="1.2" class="core"/><rect x="2" y="16" width="6" height="6" rx="1.2"/><rect x="16" y="16" width="6" height="6" rx="1.2"/></svg><span>Ask Vault</span>`;

  const modeText = status.mode === "ai"
    ? "Answers phrased by AI, grounded in Vault's implementation"
    : "Curated answers, grounded in Vault's implementation";
  const close = h("button", { class: "ask-close", type: "button", "aria-label": "Close Ask Vault" });
  close.innerHTML = `<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8"/></svg>`;
  const ctxLabel = h("b", { text: "—" });
  const log = h("div", { class: "ask-log", role: "log", "aria-live": "polite", "aria-label": "Conversation" });
  const input = h("textarea", { rows: "1", maxlength: "600", placeholder: "Ask how Vault works…",
                                "aria-label": "Your question" });
  const send = h("button", { class: "ask-send", type: "submit", "aria-label": "Ask" });
  send.innerHTML = `<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8h9M8.5 4.5 12 8l-3.5 3.5"/></svg>`;
  const form = h("form", { class: "ask-form" }, input, send);

  const panel = h("section", { class: "ask-panel", id: "ask-panel", role: "dialog", "aria-label": "Ask Vault",
                               "aria-modal": "false", hidden: "" },
    h("header", { class: "ask-head" },
      h("div", {}, h("p", { class: "ask-title", text: "Ask Vault" }), h("p", { class: "ask-mode", text: modeText })),
      close),
    h("p", { class: "ask-context" }, h("span", { text: "Viewing" }), ctxLabel),
    log, form,
    h("p", { class: "ask-foot", text: "Explains Vault; it can't change anything. Website animations are illustrative; the Live Console shows real data." }));

  document.body.append(launch, panel);

  // welcome
  const welcome = h("div", { class: "ask-welcome" },
    h("p", { class: "ask-lead", text: "Curious how Vault stores data, survives node failures, or repairs corrupted data?" }),
    h("div", { class: "ask-chips" }, ...SUGGESTED.map((q) => h("button", { class: "ask-chip", type: "button", text: q }))));
  log.append(welcome);

  const history = [];
  let busy = false;

  const updateContext = () => {
    const s = currentSection();
    ctxLabel.textContent = s && SECTION_TITLES[s] ? SECTION_TITLES[s] : "Top of page";
  };
  addEventListener("scroll", () => { if (!panel.hidden) updateContext(); }, { passive: true });

  function open() {
    updateContext();
    panel.hidden = false;
    launch.setAttribute("aria-expanded", "true");
    document.body.classList.add("ask-open");
    requestAnimationFrame(() => panel.classList.add("is-open"));
    input.focus({ preventScroll: true });
  }
  function shut() {
    panel.classList.remove("is-open");
    launch.setAttribute("aria-expanded", "false");
    document.body.classList.remove("ask-open");
    const done = () => { panel.hidden = true; };
    if (reduced.matches) done(); else setTimeout(done, 160);
    launch.focus({ preventScroll: true });
  }
  launch.addEventListener("click", () => (panel.hidden ? open() : shut()));
  close.addEventListener("click", shut);
  panel.addEventListener("keydown", (e) => { if (e.key === "Escape") shut(); });

  function scrollDown() { log.scrollTo({ top: log.scrollHeight, behavior: reduced.matches ? "auto" : "smooth" }); }

  async function ask(question) {
    question = question.trim();
    if (!question || busy) return;
    busy = true;
    send.disabled = true;
    welcome.remove();
    const ctx = pageContext();
    updateContext();
    const turn = h("div", { class: "ask-turn" });
    turn.append(h("p", { class: "ask-q" }, h("span", { class: "ask-prompt", text: "›", "aria-hidden": "true" }),
                                              document.createTextNode(question)));
    const wait = h("p", { class: "ask-wait", text: "Looking that up" });
    turn.append(wait);
    log.append(turn);
    scrollDown();
    let data;
    try {
      const r = await fetch("/api/ask", { method: "POST", headers: { "Content-Type": "application/json" },
                                          body: JSON.stringify({ question, context: ctx, history: history.slice(-HISTORY_TURNS) }) });
      data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
    } catch (e) {
      data = { blocks: [{ type: "p", text: `Sorry, Ask Vault couldn't answer right now (${e.message}). The rest of the site works without it.` }], followups: [] };
    }
    wait.remove();
    turn.append(renderBlocks(data.blocks));
    const meta = h("p", { class: "ask-meta" });
    meta.append(h("span", { text: data.mode === "ai" ? "AI · grounded" : "curated" }));
    if (data.fallback) meta.append(h("span", { text: "AI unavailable, curated answer shown" }));
    if (data.sources?.length) meta.append(h("span", { text: data.sources[0] }));
    turn.append(meta);
    if (data.followups?.length) {
      turn.append(h("div", { class: "ask-chips" }, ...data.followups.map((q) => h("button", { class: "ask-chip", type: "button", text: q }))));
    }
    history.push({ q: question, a: plainText(data.blocks).slice(0, 1500) });
    busy = false;
    send.disabled = false;
    scrollDown();
  }

  log.addEventListener("click", (e) => {
    const chip = e.target.closest(".ask-chip");
    if (chip) ask(chip.textContent);
  });
  form.addEventListener("submit", (e) => { e.preventDefault(); const q = input.value; input.value = ""; autosize(); ask(q); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); }
  });
  const autosize = () => { input.style.height = "auto"; input.style.height = `${Math.min(input.scrollHeight, 120)}px`; };
  input.addEventListener("input", autosize);
}

init();
