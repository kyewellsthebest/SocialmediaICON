/* Clip engine dashboard.

   Three views, a drawer, and a poll while the Live view is open. Read on a
   phone with one thumb, so: nothing hides behind a hover, every control is at
   least 44px, and the page answers "is it watching, has it caught anything"
   before you scroll. */

const $ = (s, r = document) => r.querySelector(s);
const TOKEN_KEY = "clipengine.token";
const POLL_MS = 5000;

const state = {
  token: localStorage.getItem(TOKEN_KEY) || "",
  view: "live",
  live: null,
  clips: [],
  timer: null,
};

const esc = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ---------- talking to the API ---------- */

async function api(path, options = {}) {
  const res = await fetch(`/api${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(state.token ? { "X-Dashboard-Token": state.token } : {}),
      ...(options.headers || {}),
    },
  });
  if (res.status === 401) { showGate(true); throw new Error("unauthorised"); }
  if (!res.ok) throw new Error((await res.text()).slice(0, 200) || `HTTP ${res.status}`);
  return res.status === 204 ? null : res.json();
}

function toast(message, tone = "") {
  const el = $("#toast");
  el.textContent = message;
  el.dataset.tone = tone;
  el.hidden = false;
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.hidden = true; }, 4200);
}

async function withBusy(btn, fn) {
  const was = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = `<span class="spinner"></span>`;
  try { return await fn(); } finally { btn.disabled = false; btn.innerHTML = was; }
}

/* ---------- small pieces ---------- */

const stat = (value, label, hot = false) =>
  `<div class="stat" data-hot="${hot}"><b>${esc(value)}</b><span>${esc(label)}</span></div>`;

const pill = (text, tone = "") => `<span class="pill" data-state="${tone}">${esc(text)}</span>`;

function bars(rows) {
  const top = Math.max(...rows.map(([, v]) => v), 1);
  return `<div class="bars">${rows.map(([name, v]) =>
    `<div class="bar"><span class="label">${esc(name)}</span>
      <span class="track"><span class="fill" style="width:${Math.round((v / top) * 100)}%"></span></span>
      <span class="n">${v.toFixed(1)}</span></div>`).join("")}</div>`;
}

/* Seconds into something human. A dashboard that says 4821 makes you do the
   arithmetic yourself. */
function ago(seconds) {
  if (seconds === null || seconds === undefined) return "never";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  return `${(seconds / 3600).toFixed(1)}h`;
}

/* ---------- live ---------- */

async function renderLive() {
  let data;
  try { data = await api("/live"); } catch (err) {
    if (err.message !== "unauthorised") toast(err.message, "critical");
    return;
  }
  state.live = data;

  const running = !!data.running;
  const queued = !!data.queued;
  const watching = (data.streams || []).length;
  const label = running ? "watching" : queued ? "queued" : "idle";
  const tone = running ? "live" : queued ? "warning" : "warning";
  $("#live-state").textContent = label;
  $("#live-state").dataset.state = tone;
  $("#top-state").textContent = running ? `${watching} live` : label;
  $("#top-state").dataset.state = tone;
  $("#nav-live").textContent = running ? `${watching}` : "";

  $("#live-head").textContent = running
    ? `Watching ${watching} of ${data.slots}`
    : queued
    ? "Starting…"
    : "Not watching";
  $("#live-sub").textContent = data.hint || (
    data.posting_enabled ? "Posting is ON." : "Clips are held for review — nothing is posted."
  );

  const caps = data.caps || {};
  $("#live-caps").innerHTML =
    stat(caps.per_day ?? "—", "per day") +
    stat(`${caps.min_gap_minutes ?? "—"}m`, "min gap") +
    stat(caps.allowed_now === false ? "no" : "yes", "can cut now", caps.allowed_now === false) +
    stat(data.posting_enabled ? "ON" : "off", "posting", !!data.posting_enabled);

  $("#streams").innerHTML = (data.streams || []).map(streamCard).join("")
    || `<div class="card"><p class="empty-note">${running
        ? "Attaching to streams — the first buffer takes about fifteen seconds."
        : "Nothing is being watched."}</p></div>`;

  const errors = data.errors || [];
  $("#live-errors").hidden = errors.length === 0;
  $("#live-errors-body").textContent = errors.join("\n");
}

function streamCard(s) {
  const chat = s.chat || {};
  const mood = chat.mood || {};
  const buffer = s.buffer || {};
  const requests = (chat.clip_requests || []).length;

  const lines = (chat.recent || []).map((m) =>
    `<div class="line"><span class="who">${esc(m.user || "—")}</span>
      <span class="said">${esc(m.text)}</span></div>`).join("")
    || `<div class="empty">No chat yet.</div>`;

  const why = Object.entries(s.why || {});

  return `<div class="card">
    <div class="spread">
      <div>
        <h3>${esc(s.channel)}</h3>
        <p class="muted">${Number(s.viewers || 0).toLocaleString()} watching · up ${ago(s.uptime_s)}</p>
      </div>
      ${pill(chat.connected ? "chat live" : "chat down", chat.connected ? "good" : "warning")}
    </div>

    <div class="stats">
      ${stat(chat.per_minute ?? 0, "msgs/min", (chat.per_minute || 0) > 120)}
      ${stat(mood.dominant || "—", "mood")}
      ${stat(requests, "clip asks", requests > 0)}
      ${stat((s.score ?? 0).toFixed(1), "score", (s.score || 0) > 0)}
      ${stat(`${Math.round(buffer.held_s || 0)}s`, "buffered")}
      ${stat(`${(buffer.megabytes || 0).toFixed(0)}MB`, "on disk")}
      ${stat(chat.messages_seen ?? 0, "msgs seen")}
      ${stat(ago(s.last_catch_s_ago), "last clip")}
    </div>

    ${why.length ? bars(why) : ""}
    ${mood.dominant ? `<p class="muted">Chat reads <b>${esc(mood.dominant)}</b> —
        ${Math.round((mood.confidence || 0) * 100)}% agreement over ${mood.emotive_lines || 0} lines.</p>` : ""}

    <div>
      <p class="label" style="margin-bottom:6px">Live chat · forgotten after 5 min</p>
      <div class="chat">${lines}</div>
    </div>
  </div>`;
}

/* ---------- clips ---------- */

async function renderClips() {
  let rows;
  try { rows = await api("/live/catches?limit=40"); } catch (err) {
    if (err.message !== "unauthorised") toast(err.message, "critical");
    return;
  }
  state.clips = rows;
  $("#nav-clips").textContent = rows.length ? String(rows.length) : "";
  $("#clips").innerHTML = rows.length
    ? rows.map(clipCard).join("")
    : `<div class="card"><p class="empty-note">Nothing caught yet. The watcher cuts a clip
        when chat spikes — at most ${state.live?.caps?.per_day ?? 10} a day, an hour apart.</p></div>`;
}

function clipCard(c) {
  const mood = c.mood || {};
  const why = Object.entries(c.why || {});
  const quotes = (c.quotes || []).map((q) =>
    `<span class="q">${esc(q.text)}${q.count > 1 ? `<b>${q.count}</b>` : ""}</span>`).join("");

  return `<div class="card clip">
    <div class="spread">
      <div>
        <h3>${esc(c.channel)}</h3>
        <p class="muted">${new Date(c.created_at).toLocaleString()} ·
          ${Number(c.peak_viewers || 0).toLocaleString()} watching</p>
      </div>
      ${pill(c.approved ? "kept" : mood.dominant || "caught", c.approved ? "good" : "")}
    </div>

    ${c.has_video
      ? `<video controls preload="metadata" playsinline src="/api/live/catches/${c.id}/video?token=${encodeURIComponent(state.token)}"></video>`
      : `<p class="empty-note">The clip file is no longer on this service's disk.</p>`}

    <div class="stats">
      ${stat((c.score ?? 0).toFixed(1), "score")}
      ${stat(mood.dominant || "—", "mood")}
      ${stat(`${Math.round((mood.confidence || 0) * 100)}%`, "agreement")}
      ${stat(`${Math.round(c.duration_s || 0)}s`, "length")}
    </div>

    ${why.length ? bars(why) : ""}
    ${quotes ? `<div><p class="label" style="margin-bottom:6px">What chat said</p>
        <div class="quotes">${quotes}</div></div>` : ""}

    <div class="row">
      <a class="btn btn-quiet" href="${esc(c.source_url)}" target="_blank" rel="noopener">Open channel</a>
      <button class="btn" data-keep="${c.id}" ${c.approved ? "disabled" : ""}>Keep</button>
      <button class="btn btn-danger" data-drop="${c.id}">Discard</button>
    </div>
  </div>`;
}

/* ---------- settings ---------- */

async function renderSettings() {
  let data;
  try { data = await api("/settings"); } catch (err) {
    if (err.message !== "unauthorised") toast(err.message, "critical");
    data = { connected: {} };
  }
  const live = state.live || {};
  $("#capture-config").innerHTML =
    stat(live.slots ?? "—", "streams") +
    stat(live.caps?.per_day ?? "—", "clips/day") +
    stat(`${live.caps?.min_gap_minutes ?? "—"}m`, "min gap") +
    stat("1080p", "clip quality");

  const wanted = [
    ["database", "Postgres"], ["redis", "Redis"], ["r2", "R2 storage"],
    ["anthropic", "Anthropic"], ["transcription", "Transcription"],
  ];
  $("#connections").innerHTML = wanted
    .map(([k, name]) => stat(data.connected?.[k] ? "on" : "—", name, !data.connected?.[k]))
    .join("");
}

/* yt-dlp appends boilerplate to every error — "please report this issue",
   "confirm you are on the latest version" — which is three lines of noise on
   a phone. Keep the first clause, which is the finding. */
function shortError(text) {
  const cut = String(text || "")
    .split(/\(caused by|please report|Confirm you are/i)[0]
    .replace(/^[A-Za-z]*Error:\s*/, "")
    .replace(/ERROR:\s*\[[^\]]+\]\s*\S+:\s*/, "")
    .trim();
  return cut.length > 140 ? cut.slice(0, 140) + "…" : cut;
}

function renderProbe(data, buffering) {
  const el = $("#probe-verdict");
  if (buffering) {
    const tone = data.network_blocked_locally ? "warning"
      : data.playback_url_served && data.buffered ? "good"
      : data.playback_url_served ? "warning" : "critical";
    const headline = data.network_blocked_locally
      ? "Inconclusive — this server's own network refused the connection"
      : data.verdict;
    el.innerHTML = `<p style="margin:10px 0 8px">${pill(tone === "good" ? "works" : tone === "critical" ? "blocked" : "unclear", tone)}
        <span class="muted">${esc(headline)}</span></p>` +
      `<div class="stats">${(data.checks || []).map((c) =>
        stat(c.ok ? "ok" : "no", c.what, !c.ok)).join("")}</div>` +
      `<pre class="raw">${esc((data.checks || []).map((c) => `${c.what}: ${shortError(c.detail)}`).join("\n"))}</pre>`;
    return;
  }
  if (!data.ok) {
    el.innerHTML = `<p style="margin-top:10px">${pill("blocked", "critical")}
      <span class="muted">${esc(shortError(data.error))}</span></p>`;
    return;
  }
  el.innerHTML = `<p style="margin:10px 0 8px">${pill("works", "good")}
      <span class="muted">${data.live ? "live now" : "not live"} ·
      ${Number(data.viewers || 0).toLocaleString()} watching</span></p>
    <div class="stats">
      ${stat(data.detect.label, "monitor on")}
      ${stat(`${data.detect.gb_per_day_x10} GB`, "per day x10")}
      ${stat(data.deliver.label, "post from")}
      ${stat((data.ladder || []).length, "renditions")}
    </div>`;
}

/* ---------- views ---------- */

const VIEWS = { live: renderLive, clips: renderClips, settings: renderSettings };
const TITLES = { live: "Live", clips: "Clips", settings: "Settings" };

function drawer(open) {
  $("#drawer").dataset.open = String(open);
  $("#scrim").dataset.open = String(open);
  $("#burger").setAttribute("aria-expanded", String(open));
}

async function show(view) {
  if (!VIEWS[view]) view = "live";
  state.view = view;
  $("#title").textContent = TITLES[view];
  for (const name of Object.keys(VIEWS)) $(`#view-${name}`).hidden = name !== view;
  // aria-current takes a value, not a presence: toggleAttribute would set it
  // to the empty string and the [aria-current="page"] rule would never match,
  // leaving nothing in the drawer marked as where you are.
  document.querySelectorAll("[data-view]").forEach((a) => {
    if (a.dataset.view === view) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  drawer(false);

  // Only the Live view polls. A dashboard left open on Clips should not keep
  // asking the server what chat is doing.
  clearInterval(state.timer);
  state.timer = view === "live" ? setInterval(renderLive, POLL_MS) : null;
  await VIEWS[view]();
}

function showGate(on) {
  $("#gate").hidden = !on;
  $("#app").hidden = on;
  if (on) { clearInterval(state.timer); $("#gate-token").focus(); }
}

/* ---------- events ---------- */

$("#burger").addEventListener("click", () => drawer($("#drawer").dataset.open !== "true"));
$("#scrim").addEventListener("click", () => drawer(false));
document.addEventListener("keydown", (e) => { if (e.key === "Escape") drawer(false); });

window.addEventListener("hashchange", () => show((location.hash.split("/")[1] || "live")));

$("#gate-go").addEventListener("click", unlock);
$("#gate-token").addEventListener("keydown", (e) => { if (e.key === "Enter") unlock(); });

async function unlock() {
  state.token = $("#gate-token").value.trim();
  localStorage.setItem(TOKEN_KEY, state.token);
  try {
    await api("/live");
    $("#gate-error").hidden = true;
    showGate(false);
    await show(location.hash.split("/")[1] || "live");
  } catch {
    $("#gate-error").hidden = false;
  }
}

$("#theme").addEventListener("click", () => {
  const now = document.documentElement.getAttribute("data-theme");
  const next = now === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  localStorage.setItem("clipengine.theme", next);
});

document.addEventListener("click", async (event) => {
  const t = event.target.closest("button, a[data-view]");
  if (!t) return;
  try {
    if (t.dataset.view) return;  // handled by hashchange

    if (t.id === "live-start") {
      await withBusy(t, () => api("/live/start", { method: "POST" }));
      toast("Starting. The first buffer takes about fifteen seconds.");
      return renderLive();
    }
    if (t.id === "live-stop") {
      await withBusy(t, () => api("/live/stop", { method: "POST" }));
      toast("Stopping.");
      return renderLive();
    }
    if (t.id === "live-refresh") return withBusy(t, renderLive);

    if (t.dataset.keep) {
      await withBusy(t, () => api(`/live/catches/${t.dataset.keep}/keep`, { method: "POST" }));
      toast("Kept.");
      return renderClips();
    }
    if (t.dataset.drop) {
      await withBusy(t, () => api(`/live/catches/${t.dataset.drop}`, { method: "DELETE" }));
      toast("Discarded.");
      return renderClips();
    }

    if (t.id === "probe-ladder" || t.id === "probe-buffer") {
      const channel = ($("#probe-channel").value || "").trim().replace(/^.*kick\.com\//, "");
      if (!channel) return toast("Paste a channel name first.", "critical");
      const buffering = t.id === "probe-buffer";
      if (buffering) toast("Holding the stream for 30 seconds…");
      const data = await withBusy(t, () => api(buffering
        ? `/probe/kick?channel=${encodeURIComponent(channel)}&seconds=30`
        : `/probe/ladder?channel=${encodeURIComponent(channel)}`));
      return renderProbe(data, buffering);
    }
  } catch (err) {
    if (err.message !== "unauthorised") toast(err.message, "critical");
  }
});

/* ---------- boot ---------- */

(async function boot() {
  const theme = localStorage.getItem("clipengine.theme");
  if (theme) document.documentElement.setAttribute("data-theme", theme);

  if (!state.token) return showGate(true);
  try {
    await api("/live");
  } catch {
    return showGate(true);
  }
  showGate(false);
  await show(location.hash.split("/")[1] || "live");
})();
