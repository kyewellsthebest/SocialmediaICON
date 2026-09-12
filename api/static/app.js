/* PutItUpp dashboard.
 *
 * No build step and no framework: the whole app is a queue, a list of what
 * went out, and a few buttons. Everything it draws comes from four endpoints.
 */

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};

// The token lives in localStorage rather than a cookie so that clearing site
// data is enough to lock it again, and so it never rides along on requests
// the page did not make.
const KEY = "putitupp.token";
let token = localStorage.getItem(KEY) || "";

async function api(path, options = {}) {
  const response = await fetch("/api" + path, {
    ...options,
    headers: {
      "content-type": "application/json",
      ...(token ? { "x-dashboard-token": token } : {}),
      ...(options.headers || {}),
    },
  });
  if (response.status === 401) {
    // Never reload here. The first thing the page does is call /overview to
    // find out whether the stored token still works, so reloading on 401 is a
    // loop: load, ask, get refused, reload, ask again - several times a
    // second, forever, against a server that is answering correctly.
    localStorage.removeItem(KEY);
    token = "";
    gate();
    throw new Error("unauthorised");
  }
  if (!response.ok) {
    const detail = await response.json().catch(() => ({}));
    throw new Error(detail.detail || `${response.status} ${response.statusText}`);
  }
  const type = response.headers.get("content-type") || "";
  return type.includes("json") ? response.json() : response.text();
}

function say(message, bad = false) {
  const banner = $("banner");
  banner.textContent = message;
  banner.className = "banner" + (bad ? " bad" : "");
  banner.hidden = false;
  clearTimeout(say.timer);
  say.timer = setTimeout(() => { banner.hidden = true; }, 9000);
}

const num = (n) => (n === null || n === undefined ? "—" : n.toLocaleString());
const secs = (s) => (s ? `${Math.round(s)}s` : "—");

/* --- views -------------------------------------------------------------- */

function showView(name) {
  for (const tab of document.querySelectorAll(".tab")) {
    tab.classList.toggle("on", tab.dataset.view === name);
  }
  for (const view of document.querySelectorAll(".view")) {
    view.hidden = view.id !== "view-" + name;
  }
  if (name === "queue") loadQueue();
  if (name === "posted") loadPosted();
  if (name === "setup") loadSetup();
}

/* --- queue -------------------------------------------------------------- */

async function loadQueue() {
  const [over, queue] = await Promise.all([api("/overview"), api("/queue")]);

  const stats = $("stats");
  stats.replaceChildren();
  const tiles = [
    [`${over.waiting}/${over.slots}`, "waiting", true],
    [over.bar === null ? "open" : num(over.bar), "upvotes to get in", false],
    [num(over.posted), "posted", false],
    [num(over.beaten), "beaten", false],
  ];
  for (const [n, k, green] of tiles) {
    const tile = el("div", "stat");
    tile.append(el("div", "n" + (green ? " green" : ""), n), el("div", "k", k));
    stats.append(tile);
  }

  // The pipeline in one line, because "why is nothing posting" is nearly
  // always one of these four facts rather than a fault.
  const flow = $("flow");
  flow.replaceChildren();
  const steps = [
    `${over.rooms} rooms`,
    `top of the ${over.window}`,
    `best ${over.slots} kept`,
    over.autopost
      ? `${over.posts_per_run} posted per run via ${over.publisher}`
      : `posting OFF (AUTOPOST_ENABLED)`,
  ];
  steps.forEach((step, i) => {
    if (i) flow.append(el("span", "arrow", "→"));
    flow.append(el("b", null, step));
  });

  const ran = over.last_run;
  if (ran) {
    const when = new Date(ran.at);
    const mins = Math.round((Date.now() - when.getTime()) / 60000);
    flow.append(el("span", "arrow", "·"));
    flow.append(el("span", null,
      `last run ${mins < 1 ? "just now" : mins + "m ago"}: ` +
      `${ran.found} found, ${ran.added} added, ${ran.pushed_out} pushed out, ` +
      `${ran.posted} posted (${ran.seconds}s)`));
  } else {
    flow.append(el("span", "arrow", "·"));
    flow.append(el("span", null, "no run has finished yet"));
  }

  $("queue-sub").textContent = queue.items.length
    ? `— the top ${Math.min(queue.goes_out_next, queue.items.length)} go out next run`
    : "";

  const list = $("queue-list");
  list.replaceChildren();
  if (!queue.items.length) {
    list.append(el("div", "empty",
      "Nothing waiting. Press Run now, or wait for the daily run."));
    return;
  }
  for (const reel of queue.items) {
    list.append(reelRow(reel, reel.place <= queue.goes_out_next));
  }
}

function reelRow(reel, next) {
  const row = el("div", "item" + (next ? " next" : ""));
  if (reel.place) row.append(el("div", "place", "#" + reel.place));

  const body = el("div", "body");
  body.append(el("span", "cap", reel.caption || "(no title)"));

  const meta = el("div", "meta");
  meta.append(el("span", "ups", num(reel.ups) + " ups"));
  meta.append(document.createTextNode(`  ·  ${secs(reel.duration_s)}  ·  `));
  const link = el("a", null, `r/${reel.subreddit}`);
  link.href = reel.permalink;
  link.target = "_blank";
  link.rel = "noopener";
  meta.append(link);
  if (reel.author) meta.append(document.createTextNode(`  ·  u/${reel.author}`));
  body.append(meta);
  row.append(body);

  if (reel.ready) row.append(el("span", "pill ok", "branded"));

  const actions = el("div", "actions");
  if (reel.state !== "posted") {
    const watch = el("button", "btn small", reel.ready ? "Watch" : "Prepare");
    watch.onclick = () => (reel.ready ? play(reel) : prepare(reel, watch));
    actions.append(watch);

    const drop = el("button", "btn small danger", "Drop");
    drop.onclick = async () => {
      await api(`/reels/${reel.id}/drop`, { method: "POST" });
      say("Dropped. It will not come back on a later run.");
      loadQueue();
    };
    actions.append(drop);
  }
  row.append(actions);
  return row;
}

async function prepare(reel, button) {
  button.disabled = true;
  button.textContent = "Downloading…";
  try {
    const done = await api(`/reels/${reel.id}/prepare`, { method: "POST" });
    say(`Downloaded and branded — ${done.megabytes} MB.`);
    loadQueue();
  } catch (error) {
    say(error.message, true);
    button.disabled = false;
    button.textContent = "Prepare";
  }
}

function play(reel) {
  $("player-video").src = `/api/reels/${reel.id}/video?token=${encodeURIComponent(token)}`;
  $("player-caption").textContent = reel.caption;
  $("player").hidden = false;
}

function closePlayer() {
  const video = $("player-video");
  video.pause();
  video.removeAttribute("src");
  video.load();
  $("player").hidden = true;
}

/* --- posted ------------------------------------------------------------- */

async function loadPosted() {
  const [out, lost] = await Promise.all([api("/posted"), api("/beaten")]);

  const postedList = $("posted-list");
  postedList.replaceChildren();
  if (!out.items.length) {
    postedList.append(el("div", "empty", "Nothing has gone out yet."));
  }
  for (const reel of out.items) {
    const row = reelRow(reel, false);
    for (const went of reel.went_to || []) {
      const pill = el("span", "pill " + (went.status === "posted" ? "ok" : "bad"),
        went.platform);
      if (went.url) {
        const link = el("a");
        link.href = went.url;
        link.target = "_blank";
        link.rel = "noopener";
        link.append(pill);
        row.append(link);
      } else {
        pill.title = went.error || "";
        row.append(pill);
      }
    }
    postedList.append(row);
  }

  const beatenList = $("beaten-list");
  beatenList.replaceChildren();
  if (!lost.items.length) {
    beatenList.append(el("div", "empty", "Nothing has been pushed out yet."));
  }
  for (const reel of lost.items) beatenList.append(reelRow(reel, false));
}

/* --- setup -------------------------------------------------------------- */

async function loadSetup() {
  const [where, over] = await Promise.all([api("/services"), api("/overview")]);

  const list = $("destinations");
  list.replaceChildren();

  for (const problem of where.blocked || []) {
    const row = el("div", "item");
    row.append(el("span", "pill bad", "blocked"), el("div", "body", problem));
    list.append(row);
  }

  if (!where.destinations.length) {
    list.append(el("div", "empty",
      `PUBLISHER=${where.publisher} has no credentials set. Nothing can be ` +
      `posted until it does.`));
  }

  for (const platform of where.destinations) {
    const row = el("div", "item");
    const body = el("div", "body");
    // The name Meta gave back, not one anybody typed. If this is not the
    // account you expected, the ids are pointing somewhere else.
    const named = (where.resolved || {})[platform];
    body.append(el("span", "cap", named || platform));
    body.append(el("div", "meta", named ? platform : "credentials set, name not resolved"));
    row.append(body, el("span", "pill" + (named ? " ok" : ""),
      where.autopost ? "live" : "ready"));
    list.append(row);
  }

  if (where.error) {
    const row = el("div", "item");
    row.append(el("span", "pill bad", "token"), el("div", "body", where.error));
    list.append(row);
  }

  for (const tokenRow of where.tokens || []) {
    if (tokenRow.days_left !== null && tokenRow.days_left < 14) {
      const row = el("div", "item");
      row.append(el("span", "pill bad", `${tokenRow.days_left}d`),
        el("div", "body", `${tokenRow.name} expires soon`));
      list.append(row);
    }
  }

  await loadWorker();

  const config = $("config");
  config.replaceChildren();
  const rows = [
    ["Rooms read", over.rooms],
    ["Window", "top of the " + over.window],
    ["Queue size", over.slots],
    ["Posted per run", over.posts_per_run],
    ["Publisher", over.publisher],
    ["Autopost", over.autopost ? "ON" : "off"],
    ["Reddit routes", (over.routes || []).join(", ")],
    ["Storage", over.storage],
    ["Background jobs", over.queued_jobs ? "Redis" : "run inline"],
  ];
  for (const [k, v] of rows) {
    const kv = el("div", "kv");
    kv.append(el("div", "k", k), el("div", "v", String(v)));
    config.append(kv);
  }
}

async function loadWorker() {
  const box = $("worker");
  box.replaceChildren();
  let state;
  try {
    state = await api("/run/status");
  } catch (error) {
    box.append(el("div", "empty", error.message));
    return;
  }

  if (!state.redis) {
    box.append(el("div", "empty", state.why || "no Redis"));
    return;
  }

  const row = el("div", "item");
  row.append(el("span", "pill " + (state.workers ? "ok" : "bad"),
    `${state.workers} worker${state.workers === 1 ? "" : "s"}`));
  const body = el("div", "body");
  body.append(el("span", "cap", state.workers
    ? `listening to ${(state.listening_to || []).join(", ") || "nothing"}`
    : "nothing is listening — queued runs will never start"));
  const counts = Object.entries(state.queues || {})
    .map(([name, q]) => `${name}: ${q.waiting} waiting, ${q.running} running, ${q.failed} failed`)
    .join("  ·  ");
  body.append(el("div", "meta", counts));
  row.append(body);
  box.append(row);

  if (state.why) {
    const warn = el("div", "item");
    warn.append(el("span", "pill bad", "!"), el("div", "body", state.why));
    box.append(warn);
  }

  if (state.last_error) {
    box.append(el("div", "meta", `last failure on ${state.last_error.queue}` +
      (state.last_error.at ? ` at ${state.last_error.at}` : "")));
    const pre = el("pre", null, state.last_error.traceback);
    box.append(pre);
  }
}

async function diagnose(path, button, label) {
  const pre = $("diag");
  button.disabled = true;
  button.textContent = "Working… this makes real requests";
  pre.hidden = false;
  pre.textContent = "…";
  try {
    pre.textContent = await api(path);
  } catch (error) {
    pre.textContent = error.message;
  }
  button.disabled = false;
  button.textContent = label;
}

/* --- wiring ------------------------------------------------------------- */

function start() {
  $("gate").hidden = true;
  $("app").hidden = false;

  for (const tab of document.querySelectorAll(".tab")) {
    tab.onclick = () => showView(tab.dataset.view);
  }

  $("run-now").onclick = async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    button.textContent = "Running…";
    try {
      const done = await api("/run", { method: "POST" });
      if (done.queued && !done.workers) {
        say(done.warning || "Queued, but no worker is listening — it will " +
          "never start. See Setup.", true);
      } else {
        say(done.queued
          ? "Run queued — it reads every room, so give it a few minutes."
          : `Done: ${done.result.added} added, ${done.result.pushed_out} pushed out, ` +
            `${done.result.posted} posted.`);
      }
      loadQueue();
    } catch (error) {
      say(error.message, true);
    }
    button.disabled = false;
    button.textContent = "Run now";
  };

  $("run-here").onclick = async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    button.textContent = "Reading three rooms…";
    try {
      const done = await api("/run?inline=true&rooms=3", { method: "POST" });
      const r = done.result;
      say(`${r.found} found, ${r.added} added, ${r.pushed_out} pushed out, ` +
        `${r.posted} posted — in ${r.seconds}s.`);
      loadWorker();
    } catch (error) {
      say(error.message, true);
    }
    button.disabled = false;
    button.textContent = "Run 3 rooms here, now";
  };

  $("ways-in").onclick = (e) =>
    diagnose("/reddit/ways-in", e.currentTarget, "Test every route");
  $("try-one").onclick = (e) =>
    diagnose("/reddit/try-one", e.currentTarget, "Download one and check the sound");

  showView("queue");
}

function gate() {
  // A refused token can arrive while the player is open, and a modal left
  // over a password box is one nothing can be done about.
  closePlayer();
  $("app").hidden = true;
  $("gate").hidden = false;
  const go = async () => {
    token = $("gate-token").value.trim();
    try {
      await api("/overview");
      localStorage.setItem(KEY, token);
      start();
    } catch {
      $("gate-error").hidden = false;
    }
  };
  $("gate-go").onclick = go;
  $("gate-token").onkeydown = (e) => { if (e.key === "Enter") go(); };
}

// Bound here rather than inside start(), so the overlay can always be closed
// - including when the token was refused and the dashboard never started.
$("player-close").onclick = closePlayer;
$("player").onclick = (event) => {
  // The backdrop, not the video inside it.
  if (event.target === $("player")) closePlayer();
};
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closePlayer();
});
closePlayer();

// The stored token is checked once, on load. A failure here means the gate,
// not a retry - there is nothing to retry against.
api("/overview").then(start).catch(() => {});
