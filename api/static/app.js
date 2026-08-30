/* Clip engine dashboard.

   Three views, a drawer, and a poll while the Live view is open. Read on a
   phone with one thumb, so: nothing hides behind a hover, every control is at
   least 44px, and the page answers "is it watching, has it caught anything"
   before you scroll. */

const $ = (s, r = document) => r.querySelector(s);
const TOKEN_KEY = "clipengine.token";
const POLL_MS = 5000;

const VIDEO_KEY = "clipengine.video";

const state = {
  token: localStorage.getItem(TOKEN_KEY) || "",
  view: "live",
  live: null,
  clips: [],
  timer: null,
  // Which stream is allowed to make noise. Three players talking over each
  // other is unusable, so it is one at a time or none.
  sound: null,
  clipOrder: localStorage.getItem("clipengine.cliporder") || "best",
  video: localStorage.getItem(VIDEO_KEY) !== "off",
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

/* What the bot heard and saw. Everything else - what chat said about it, how
   busy the channel is - can raise a moment but can never make one, so a score
   with nothing here is a score about nothing. Kept in step with
   moments.SENSED. */
const EVENTS = ["laughter", "shout", "audio_drop", "audio_jump",
                "motion_surge", "scene_cuts", "flash"];
const eventScore = (why) =>
  Object.entries(why || {}).reduce((n, [k, v]) => n + (EVENTS.includes(k) ? v : 0), 0);

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
  // "wanted" is the intention, which outlives a deploy or a crash. Showing it
  // as "restarting" rather than "idle" is the difference between a gap you
  // have to act on and a gap that closes itself.
  const streams = data.streams || [];
  // "Restarting" is only true while something is actually in flight. With an
  // empty queue and no worker it was a comforting word for a watch that had
  // stopped, and it kept saying it forever.
  const inflight = (data.queue?.waiting || 0) + (data.queue?.started || 0) > 0;
  const resuming = !running && !queued && data.wanted && inflight;
  const stuck = !running && !queued && !resuming && !!data.diagnosis;
  const label = running ? `${streams.length} live`
    : queued ? "starting" : resuming ? "restarting" : stuck ? "stuck" : "off";
  $("#top-state").textContent = label;
  $("#top-state").dataset.state = running ? "live" : stuck ? "critical" : "warning";
  $("#nav-live").textContent = running ? String(streams.length) : "";

  paintStreams(streams, { running, queued, resuming, stuck, hint: data.hint,
                          diagnosis: data.diagnosis, health: data.health });

  // What it is holding, waiting for a slot. Worth showing: it is the whole
  // difference between a watcher and a chooser, and it is otherwise invisible.
  const holding = data.shortlist || [];
  $("#holding").hidden = holding.length === 0;
  $("#holding-body").innerHTML = holding.map((h) =>
    `<div class="bar"><span class="label">${esc(h.channel)}</span>
      <span class="track"><span class="fill" style="width:${
        Math.round((h.score / Math.max(...holding.map((x) => x.score), 1)) * 100)}%"></span></span>
      <span class="n">${h.score.toFixed(0)}</span></div>`).join("");
  $("#holding-note").textContent = holding.length
    ? `${holding.length} moment${holding.length > 1 ? "s" : ""} cut and waiting. ` +
      `The best one goes out when the next slot opens.`
    : "";

  // What it looked at and decided not to watch. Worth showing: the bot spent
  // an evening on Hindi gaming streams and the page gave no sign it had
  // considered anything else, let alone rejected it.
  const skipped = (data.skipped || []).map((sk) => `${sk.channel} - ${sk.why}`);
  $("#skipped").hidden = skipped.length === 0;
  $("#skipped-body").textContent = skipped.join("\n");

  const declined = (data.declined || []).map(
    (d) => `${d.channel}: ${d.happening || d.why}`
  );
  const errors = [...declined.map((line) => "declined - " + line), ...(data.errors || [])];
  $("#live-errors").hidden = errors.length === 0;
  $("#live-errors-body").textContent = errors.join("\n");
  $("#live-errors").querySelector("h3").textContent =
    declined.length && !(data.errors || []).length ? "What it decided against" : "Recent problems";
}

/* Update the cards in place rather than rebuilding them.

   This poll runs every five seconds and each card holds a live player.
   Rewriting innerHTML would tear the iframe out and put a new one back, so
   every stream would restart from black twice a minute - which is exactly the
   thumbnail-refresh flicker the players were meant to replace. */
function paintStreams(streams, status) {
  const host = $("#streams");
  const seen = new Set();

  for (const s of streams) {
    seen.add(s.channel);
    let card = host.querySelector(`[data-channel="${cssId(s.channel)}"]`);
    if (!card) { card = buildStreamCard(s); host.append(card); }
    fillStreamCard(card, s);
  }

  for (const card of [...host.querySelectorAll("[data-channel]")]) {
    if (!seen.has(card.dataset.channel)) card.remove();
  }

  let note = host.querySelector(".empty-note-card");
  if (streams.length) { note?.remove(); return; }
  if (!note) {
    note = document.createElement("div");
    note.className = "card empty-note-card";
    host.append(note);
  }
  // Whatever the server actually knows beats anything written here. "Starting
  // up, this takes a few seconds after a deploy" was a guess, and it kept
  // saying it for as long as the page was open.
  const said = status.hint || status.diagnosis;
  // A fault beats every hint. "Attaching to streams" and "broken since
  // midnight" looked identical for eight hours, which is how a night of
  // clipping was lost without anybody being told.
  const health = status.health || {};
  if (health.ok === false) {
    note.className = "card empty-note-card bad";
    note.innerHTML =
      `<p class="empty-note"><b>Watching nothing.</b> ${esc(health.detail || "")}</p>` +
      (health.last_error ? `<p class="muted">${esc(health.last_error)}</p>` : "");
    return;
  }
  note.className = "card empty-note-card";
  note.innerHTML = `<p class="empty-note">${esc(said || (status.running
    ? "Attaching to streams - the first buffer takes about fifteen seconds."
    : "Nothing is being watched."))}</p>` +
    (status.diagnosis && status.diagnosis !== said
      ? `<p class="muted">${esc(status.diagnosis)}</p>` : "");
}

/* Attribute selectors are not a safe place to interpolate a channel name. */
const cssId = (v) => String(v).replace(/["\\]/g, "\\$&");

/* Kick serves an embeddable player per channel, which is the only way to show
   the actual stream: the buffer we hold is on the worker's disk, in another
   container, and re-serving it would be a second copy of every byte. */
const playerSrc = (channel, muted) =>
  `https://player.kick.com/${encodeURIComponent(channel)}` +
  `?autoplay=true&muted=${muted ? "true" : "false"}`;

function buildStreamCard(s) {
  const card = document.createElement("div");
  card.className = "card";
  card.dataset.channel = s.channel;
  card.innerHTML = `
    <a class="stream-row" href="#/stream/${encodeURIComponent(s.channel)}">
      <img class="pfp" alt="" data-f="pfp" hidden referrerpolicy="no-referrer"
           onerror="this.hidden = true">
      <span class="who"><b data-f="name"></b><small data-f="sub"></small></span>
      <span class="go">&rsaquo;</span>
    </a>
    <div class="player" data-f="player"></div>
    <p class="muted" data-f="about" hidden></p>
    <div class="stats" data-f="stats"></div>`;
  paintPlayer(card.querySelector('[data-f="player"]'), s);
  return card;
}

function paintPlayer(box, s) {
  // Remembered on the box because repaints are driven by a mute tap, which
  // knows the channel and nothing else about the stream.
  if (s.thumbnail) box.dataset.thumb = s.thumbnail;
  if (s.name || s.channel) box.dataset.label = s.name || s.channel;

  const wanted = state.video ? playerSrc(s.channel, state.sound !== s.channel) : "";
  if (box.dataset.src === wanted) return;
  box.dataset.src = wanted;

  if (!wanted) {
    box.innerHTML = box.dataset.thumb
      ? `<img class="shot" src="${esc(box.dataset.thumb)}" alt=""
           onerror="this.remove()" referrerpolicy="no-referrer">`
      : `<span class="off">Video is off</span>`;
    return;
  }
  const loud = state.sound === s.channel;
  box.innerHTML =
    `<iframe src="${esc(wanted)}" title="${esc(box.dataset.label || s.channel)}"
       allow="autoplay; fullscreen; picture-in-picture" allowfullscreen></iframe>
     <button class="sound" data-sound="${esc(s.channel)}" data-on="${loud}"
       aria-label="${loud ? "Mute" : "Unmute"}">${loud ? "&#128266;" : "&#128263;"}</button>`;
}

function fillStreamCard(card, s) {
  const chat = s.chat || {};
  const mood = chat.mood || {};
  const requests = (chat.clip_requests || []).length;

  const pfp = card.querySelector('[data-f="pfp"]');
  if (s.avatar && pfp.getAttribute("src") !== s.avatar) { pfp.src = s.avatar; pfp.hidden = false; }

  card.querySelector('[data-f="name"]').textContent = s.name || s.channel;
  card.querySelector('[data-f="sub"]').textContent =
    `${Number(s.viewers || 0).toLocaleString()} watching` +
    (s.messages_per_min ? ` \u00b7 ${Math.round(s.messages_per_min)} msgs/min` : "") +
    (s.dormant ? " \u00b7 asleep" : s.category ? " \u00b7 " + s.category : "");

  paintPlayer(card.querySelector('[data-f="player"]'), s);

  // Who this is, as the research found them. The bot decides what to watch on
  // this; showing it is how a wrong decision gets argued with.
  const about = card.querySelector('[data-f="about"]');
  about.textContent = s.about || "";
  about.hidden = !s.about;

  card.querySelector('[data-f="stats"]').innerHTML =
    stat(chat.per_minute ?? 0, "msgs/min", (chat.per_minute || 0) > 120) +
    stat(s.dormant ? "asleep"
         : (s.senses?.heard?.laughs || []).length ? "laughing"
         : (s.senses?.heard?.shouts || []).length ? "shouting"
         : (s.senses?.seen?.surges || []).length ? "moving"
         : s.reason === "nothing happened" ? "nothing"
         : mood.dominant || "\u2014", "heard") +
    stat(requests, "clip asks", requests > 0) +
    stat((s.score ?? 0).toFixed(1), "score", (s.score || 0) > 0);
}

/* ---------- one stream, everything ---------- */

async function renderStream() {
  const channel = decodeURIComponent((location.hash.split("/")[2] || ""));
  const box = $("#stream-detail");
  const top = $("#stream-player").closest(".card");
  if (!channel) {
    top.hidden = true;
    box.innerHTML = `<div class="card"><p class="empty-note">No stream.</p></div>`;
    return;
  }

  let s;
  try {
    s = await api(`/live/streams/${encodeURIComponent(channel)}`);
  } catch (err) {
    // A stream that has been dropped from the roster 404s here, and leaving
    // the old header up would say it is still being watched.
    top.hidden = true;
    stopPlayer();
    box.innerHTML = `<div class="card"><p class="empty-note">${esc(err.message)}</p></div>`;
    return;
  }
  top.hidden = false;
  $("#title").textContent = s.name || s.channel;
  paintStreamTop(s);

  const chat = s.chat || {};
  const mood = chat.mood || {};
  const buffer = s.buffer || {};
  const audio = s.audio || {};
  const activity = s.activity || {};
  const senses = s.senses || {};
  const heard = senses.heard;
  const seen = senses.seen;
  const why = Object.entries(s.why || {});
  const counts = Object.entries(mood.counts || {});

  const lines = (chat.recent || []).map((m) =>
    `<div class="line"><span class="who">${esc(m.user || "—")}</span>
      <span class="said">${esc(m.text)}</span></div>`).join("")
    || `<div class="empty">No chat yet.</div>`;

  box.innerHTML = `
    <div class="card">
      <header><h3>What it just heard and saw</h3>
        <span class="label">${senses.window_s ? `last ${Math.round(senses.window_s)}s` : "\u2014"}</span></header>
      ${heard || seen ? `<div class="stats">
        ${stat((heard?.laughs || []).length, "laughs", (heard?.laughs || []).length > 0)}
        ${stat((heard?.shouts || []).length, "raised voices", (heard?.shouts || []).length > 0)}
        ${stat((heard?.gasps || []).length, "gasps?", (heard?.gasps || []).length > 0)}
        ${stat(heard?.voiced_share != null ? `${Math.round(heard.voiced_share * 100)}%`
               : "\u2014", "is a voice")}
        ${stat((seen?.surges || []).length, "motion surges", (seen?.surges || []).length > 0)}
        ${stat((seen?.cuts || []).length, "cuts")}
        ${stat((heard?.drops || []).length, "quiet drops")}
        ${stat(heard ? `${Math.round((heard.speech_share || 0) * 100)}%` : "\u2014", "sounds like speech")}
      </div>
      ${senses.said?.words
        ? `<p class="label" style="margin-top:6px">Being said right now</p>
           <p class="muted">${esc(senses.said.recent)}</p>
           <p class="label">${senses.said.words} words held \u00b7
             ${senses.said.minutes_spent} min transcribed today</p>` : ""}
      <p class="muted">This is what decides. Chat can agree with it and cannot
        replace it.</p>`
      : `<p class="empty-note">${esc((senses.problems || []).join(" \u00b7 ")
          || "Nothing read yet - the first pass takes about twenty seconds.")}</p>`}
    </div>

    <div class="card">
      <header><h3>Is anyone there</h3>
        <span class="label">${s.dormant ? "nobody home" : "someone is"}</span></header>
      <div class="stats">
        ${stat(activity.motion ?? "—", "picture moving", activity.still === true)}
        ${stat(activity.quiet === true ? "silent" : "sound", "room",
               activity.quiet === true)}
        ${stat(s.dormant ? "asleep" : "awake", "verdict", !!s.dormant)}
        ${stat(ago(s.uptime_s), "watched for")}
      </div>
      ${s.dormant
        ? `<p class="muted">Silent and still for long enough to count as away.
             The slot goes to the next stream down until this one moves again.</p>`
        : ""}
    </div>

    <div class="card">
      <header><h3>Sound</h3><span class="label">last ${Math.round(audio.held_s || 0)}s</span></header>
      ${audio.ok
        ? `<div class="wave">${wave(audio.loudness_db || [])}</div>
           <div class="stats">
             ${stat(`${audio.mean_db ?? "—"}`, "mean dB")}
             ${stat(`${audio.peak_db ?? "—"}`, "peak dB")}
             ${stat((audio.jumps || []).length, "spikes", (audio.jumps || []).length > 0)}
             ${stat((audio.quiet_runs || []).length, "pauses")}
           </div>
           ${audio.has_spectrogram ? `<img class="spectro" id="spectro" alt="spectrogram">` : ""}`
        : `<p class="empty-note">${esc(audio.why || "Waiting for the buffer to fill.")}</p>`}
    </div>

    <div class="card">
      <header><h3>Why it is scoring</h3><span class="label">${(s.score ?? 0).toFixed(1)} total</span></header>
      ${why.length ? bars(why) : `<p class="empty-note">Nothing is standing out right now.</p>`}
      ${mood.dominant
        ? `<p class="muted">Chat reads <b>${esc(mood.dominant)}</b> —
             ${Math.round((mood.confidence || 0) * 100)}% agreement over
             ${mood.emotive_lines || 0} lines.</p>
           ${counts.length ? bars(counts) : ""}`
        : `<p class="muted">Chat is not reading as any particular feeling.</p>`}
    </div>

    <div class="card">
      <header><h3>Buffer</h3></header>
      <div class="stats">
        ${stat(`${Math.round(buffer.held_s || 0)}s`, "held")}
        ${stat(`${(buffer.megabytes || 0).toFixed(0)}MB`, "on disk")}
        ${stat(buffer.segments ?? "—", "segments")}
        ${stat(ago(s.last_catch_s_ago), "last clip")}
      </div>
    </div>

    <div class="card">
      <header><h3>Chat</h3><span class="label">forgotten after 5 min</span></header>
      <div class="stats">
        ${stat(chat.per_minute ?? 0, "msgs/min", (chat.per_minute || 0) > 120)}
        ${stat(chat.messages_seen ?? 0, "seen")}
        ${stat((chat.bursts || []).length, "bursts", (chat.bursts || []).length > 0)}
        ${stat((chat.clip_requests || []).length, "clip asks",
               (chat.clip_requests || []).length > 0)}
      </div>
      <div class="chat">${lines}</div>
    </div>`;

  // Built after the card so the poll below can leave it alone: this view
  // refreshes every five seconds too, and the player must survive that.
  if (audio.has_spectrogram) refreshSpectrogram(s.channel);
}

/* The header and the player: written field by field, never rebuilt. */
function paintStreamTop(s) {
  const chat = s.chat || {};
  const pfp = $("#stream-pfp");
  if (s.avatar && pfp.getAttribute("src") !== s.avatar) { pfp.src = s.avatar; pfp.hidden = false; }
  $("#stream-name").textContent = s.name || s.channel;
  $("#stream-title").textContent = s.title || "";
  $("#stream-pill").innerHTML = s.dormant
    ? pill("asleep", "warning")
    : pill(chat.connected ? "chat live" : "chat down", chat.connected ? "good" : "warning");
  $("#stream-open").href = s.page || `https://kick.com/${s.channel}`;
  $("#stream-meta").textContent =
    `${Number(s.viewers || 0).toLocaleString()} watching \u00b7 up ${ago(s.uptime_s)}` +
    (s.category ? " \u00b7 " + s.category : "");

  const box = $("#stream-player");
  box.dataset.channel = s.channel;
  paintPlayer(box, s);
}

/* Load the new spectrogram off-screen and only swap it in once it has
   decoded. Setting src on the visible <img> blanks it to black for as long as
   the fetch takes, and on a five second poll that reads as a flicker rather
   than an update. */
function refreshSpectrogram(channel) {
  const el = $("#spectro");
  if (!el) return;
  const next = new Image();
  next.onload = () => { el.src = next.src; };
  next.src = `/api/live/streams/${encodeURIComponent(channel)}/spectrogram` +
    `?token=${encodeURIComponent(state.token)}&t=${Date.now()}`;
}

/* A loudness curve as bars. Height is the dB range mapped onto the box, so a
   quiet stream still shows shape rather than a flat line at the bottom. */
function wave(curve) {
  if (!curve.length) return "";
  curve = fitBars(curve, 64);
  const lo = Math.min(...curve), hi = Math.max(...curve);
  const span = Math.max(hi - lo, 1);
  return curve.map((v) => {
    const h = Math.max(3, Math.round(((v - lo) / span) * 100));
    return `<i style="height:${h}%;opacity:${(0.35 + 0.65 * (v - lo) / span).toFixed(2)}"></i>`;
  }).join("");
}

/* Half a minute of loudness is a few hundred samples, and a flex row of a few
   hundred bars cannot be narrower than one pixel each - which is how the
   sound card came to be wider than the phone it was being read on. Peak-pool
   down to something that fits, keeping the peaks, because the peaks are the
   whole point of looking at it. */
function fitBars(values, most) {
  if (values.length <= most) return values;
  const per = values.length / most;
  return Array.from({ length: most }, (_, i) =>
    Math.max(...values.slice(Math.floor(i * per), Math.max(Math.floor((i + 1) * per), Math.floor(i * per) + 1))));
}

/* ---------- clips ---------- */

async function renderClips() {
  let rows;
  try { rows = await api(`/live/catches?limit=60&by=${state.clipOrder}`); } catch (err) {
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
  const rank = c.rank || {};
  const parts = Object.entries(rank.parts || {});
  return `<div class="card clip">
    <div class="spread">
      <div>
        <h3>${esc(c.channel)}</h3>
        <p class="muted">${new Date(c.created_at).toLocaleString()} ·
          ${Number(c.peak_viewers || 0).toLocaleString()} watching</p>
      </div>
      <button class="btn btn-quiet" data-inspect="${c.id}">Inspect</button>
    </div>

    ${c.rank_score != null ? `<div class="rankbar">
      <b class="mono">${c.rank_score.toFixed(0)}</b>
      ${parts.map(([name, v]) =>
        `<span class="seg" style="flex:${Math.max(v, 0.02)}"
           title="${esc(name)} ${(v * 100).toFixed(0)}%" data-part="${esc(name)}"></span>`
      ).join("")}
    </div>` : ""}

    ${c.has_video
      ? `<video class="portrait" controls preload="metadata" playsinline
           src="/api/live/catches/${c.id}/video?token=${encodeURIComponent(state.token)}"></video>`
      : `<div class="portrait missing"><span>${
          esc(c.video_note || "No video held for this one.")}</span></div>`}

    <div class="row">
      <span class="pill" data-state="${c.approved ? "good"
        : c.verdict?.watched ? "good" : "warning"}">${
        c.approved ? "kept" : c.verdict?.kind || (c.verdict?.watched ? "caught" : "unwatched")
      }</span>
      <span class="muted">${rank.carried_by
        ? esc(rank.carried_by)
        : (c.score ?? 0).toFixed(1)} · ${Math.round(c.duration_s || 0)}s</span>
      <span style="flex:1 1 auto"></span>
      <button class="btn" data-keep="${c.id}" ${c.approved ? "disabled" : ""}>Keep</button>
      <button class="btn btn-danger" data-drop="${c.id}">Discard</button>
    </div>
  </div>`;
}

/* The inspect sheet: everything behind the decision, on top of the clip
   rather than beside it. The card is for judging the video; this is for
   arguing with the reason it was chosen. */
function showInspect(id) {
  const c = (state.clips || []).find((row) => String(row.id) === String(id));
  if (!c) return;
  const mood = c.mood || {};
  const why = Object.entries(c.why || {});
  const quotes = (c.quotes || []).map((q) =>
    `<span class="q">${esc(q.text)}${q.count > 1 ? `<b>${q.count}</b>` : ""}</span>`).join("");

  const seenBy = c.verdict || {};
  const rank = c.rank || {};

  $("#sheet-body").innerHTML = `
    <div class="spread">
      <div><h3>${esc(c.channel)}</h3>
        <p class="muted">${new Date(c.created_at).toLocaleString()}</p></div>
      <button class="btn btn-quiet" id="sheet-close" aria-label="Close">✕</button>
    </div>

    <div class="card">
      <header><h3>What it saw when it watched this</h3>
        <span class="label">${seenBy.watched ? `${Math.round((seenBy.confidence || 0) * 100)}% sure`
          : "not watched"}</span></header>
      ${seenBy.watched
        ? `<p><b>${esc(seenBy.happening || "\u2014")}</b></p>
           <p class="muted">${esc(seenBy.why || "")}</p>
           <div class="row">
             ${seenBy.kind ? pill(seenBy.kind, seenBy.worth_it ? "good" : "warning") : ""}
             ${seenBy.setting ? pill(seenBy.setting) : ""}
             ${(seenBy.faces || []).map((f) => pill(f.expression || "")).join("")}
           </div>`
        : `<p class="empty-note">Nothing watched this before it was cut${
            (seenBy.problems || []).length ? ` \u2014 ${esc(seenBy.problems.join("; "))}` : ""
          }. Clips cut before the check existed all read this way.</p>`}
    </div>
    ${Object.keys(rank.parts || {}).length
      ? `<div><p class="label" style="margin-bottom:6px">How it ranks, part by part</p>
           ${bars(Object.entries(rank.parts).map(([k, v]) => [k, v * 100]))}
           <p class="muted">${c.rank_score?.toFixed(0)} out of 100.
             ${rank.detail?.rejected ? esc(rank.detail.rejected) : ""}</p></div>` : ""}
    <div class="stats">
      ${stat((c.rank_score ?? c.score ?? 0).toFixed(0), "rank")}
      ${stat(eventScore(c.why).toFixed(1), "from events", eventScore(c.why) <= 0)}
      ${stat(mood.background ? "background" : mood.dominant || "—", "mood",
             !!mood.background)}
      ${stat(`${mood.lift ?? "—"}\u00d7`, "vs usual", (mood.lift ?? 9) < 1.35)}
      ${stat(Number(c.peak_viewers || 0).toLocaleString(), "watching")}
      ${stat(`${Math.round(c.duration_s || 0)}s`, "length")}
    </div>
    <div><p class="label" style="margin-bottom:6px">Why it was cut</p>
      ${why.length ? bars(why) : `<p class="empty-note">No breakdown recorded.</p>`}
      ${eventScore(c.why) <= 0
        ? `<p class="muted">Nothing here says anything <em>happened</em> — this is
             how busy the channel was, not a reaction to it. A clip scored this
             way is the mistake this panel exists to make visible.</p>` : ""}</div>
    ${Object.keys(mood.counts || {}).length
      ? `<div><p class="label" style="margin-bottom:6px">What chat felt</p>
           ${bars(Object.entries(mood.counts))}
           <p class="muted">${mood.background
             ? `Chat feels this way all the time on this channel (${mood.lift}\u00d7 its
                usual rate), so it is not a reaction to anything.`
             : `${Math.round((mood.confidence || 0) * 100)}% agreement over
                ${mood.emotive_lines || 0} lines, ${mood.lift}\u00d7 the channel's
                usual rate.`}</p></div>` : ""}
    ${quotes ? `<div><p class="label" style="margin-bottom:6px">What chat said</p>
        <div class="quotes">${quotes}</div></div>` : ""}
    ${c.transcript ? `<div><p class="label" style="margin-bottom:6px">What was said</p>
        <p class="muted">${esc(c.transcript)}</p></div>` : ""}
    <a class="btn btn-quiet" href="${esc(c.source_url)}" target="_blank" rel="noopener">Open channel</a>`;
  sheet(true);
}

function sheet(open) {
  $("#sheet").hidden = !open;
  $("#sheet-scrim").hidden = !open;
}

/* ---------- settings ---------- */

async function renderSettings() {
  let data;
  try { data = await api("/settings"); } catch (err) {
    if (err.message !== "unauthorised") toast(err.message, "critical");
    data = { connected: {} };
  }
  // Opened cold - a bookmark straight to Settings - the Live view has never
  // run and there is nothing to report the watcher's state from.
  if (!state.live) { try { state.live = await api("/live"); } catch { /* reported below */ } }

  const live = state.live || {};
  $("#capture-config").innerHTML =
    stat(live.slots ?? "—", "streams") +
    stat(live.caps?.per_day ?? "—", "clips/day") +
    stat(`${live.caps?.min_gap_minutes ?? "—"}m`, "min gap") +
    stat("1080p", "clip quality");

  // Moved off the Live page, which is now three streams and nothing else.
  const caps = live.caps || {};
  $("#pref-video").checked = state.video;
  $("#live-caps").innerHTML =
    stat(live.running ? "watching" : live.queued ? "starting" : live.wanted ? "restarting" : "off",
         "state", !live.running) +
    stat(caps.cut_today ?? "—", "cut today") +
    stat(live.posting_enabled ? "ON" : "off", "posting", !!live.posting_enabled) +
    // "no" on its own could be the cap, the hour, or a dead database, and only
    // one of those is worth getting out of your chair for.
    stat(caps.cap_reason || (caps.allowed_now === false ? "no" : "yes"), "can cut now",
         caps.allowed_now === false && caps.cap_reason !== "hourly gap");
  $("#cap-detail").textContent = caps.cap_detail || "";
  $("#live-sub").textContent = [live.hint, live.diagnosis]
    .filter((line, i, all) => line && all.indexOf(line) === i).join(" ")
    || (live.running
      ? "Watching. It restarts itself after a deploy or a crash."
      : "Not watching right now — it restarts itself, or start it by hand below.");

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

const VIEWS = { live: renderLive, stream: renderStream, clips: renderClips, settings: renderSettings };
const TITLES = { live: "Live", stream: "Stream", clips: "Clips", settings: "Settings" };

function drawer(open) {
  $("#drawer").dataset.open = String(open);
  $("#scrim").dataset.open = String(open);
  $("#burger").setAttribute("aria-expanded", String(open));
}

async function show(view) {
  if (!VIEWS[view]) view = "live";
  state.view = view;
  $("#title").textContent = TITLES[view];
  // A hidden iframe is still a running stream, so the players a view is
  // leaving behind are torn down rather than left pulling video in the
  // background. They come back when the view does; that is one deliberate
  // reload on navigation, not one every five seconds.
  if (view !== "stream") stopPlayer();
  if (view !== "live") $("#streams").replaceChildren();
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
  state.timer = view === "live" ? setInterval(renderLive, POLL_MS)
    : view === "stream" ? setInterval(renderStream, POLL_MS)
    : null;
  await VIEWS[view]();
}

function showGate(on) {
  $("#gate").hidden = !on;
  $("#app").hidden = on;
  if (on) { clearInterval(state.timer); $("#gate-token").focus(); }
}

/* ---------- events ---------- */

/* Leaving the view is not enough to stop an iframe: a display: none subtree
   keeps its media running, so the detail player is torn down by hand. */
function stopPlayer() {
  const box = $("#stream-player");
  box.innerHTML = "";
  delete box.dataset.src;
}

/* Reload only the players whose muting actually changed - paintPlayer is a
   no-op when the src it would set is the one already there, so the other two
   streams keep playing through the tap. */
function repaintPlayers() {
  for (const box of document.querySelectorAll(".player")) {
    const channel = box.closest("[data-channel]")?.dataset.channel || box.dataset.channel;
    if (channel) paintPlayer(box, { channel, thumbnail: "" });
  }
}

$("#pref-video").addEventListener("change", (e) => {
  state.video = e.target.checked;
  if (!state.video) state.sound = null;
  localStorage.setItem(VIDEO_KEY, state.video ? "on" : "off");
  repaintPlayers();
});

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
      toast("Watching. It will restart itself from now on.");
      return renderSettings();
    }
    if (t.id === "live-stop") {
      await withBusy(t, () => api("/live/stop", { method: "POST" }));
      toast("Stopped. It will stay stopped until you start it again.");
      return renderSettings();
    }

    // One stream at a time, and tapping the one already talking mutes it.
    if (t.dataset.sound) {
      state.sound = state.sound === t.dataset.sound ? null : t.dataset.sound;
      return repaintPlayers();
    }
    if (t.id === "live-why") {
      const d = await withBusy(t, () => api("/live/debug"));
      const fail = (d.recent_failures || [])[0];
      $("#live-why-out").innerHTML =
        `<p style="margin:10px 0 8px">${pill("diagnosis", "warning")}
           <span class="muted">${esc(d.verdict || "no verdict")}</span></p>
         <div class="stats">
           ${stat(d.web?.live_enabled ? "on" : "OFF", "web live_enabled", !d.web?.live_enabled)}
           ${stat(d.queue?.workers_listening_on_live?.length ?? 0, "workers on live",
                  !(d.queue?.workers_listening_on_live || []).length)}
           ${stat(d.queue?.waiting ?? "—", "queued")}
           ${stat(d.queue?.failed ?? "—", "failed", (d.queue?.failed || 0) > 0)}
         </div>` +
        (fail ? `<pre class="raw">${esc(fail.error || "")}</pre>` : "");
      return;
    }

    if (t.dataset.inspect) return showInspect(t.dataset.inspect);
    if (t.id === "sheet-close") return sheet(false);
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
