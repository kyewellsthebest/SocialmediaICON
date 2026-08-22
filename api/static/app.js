/* Clip engine dashboard.
   Vanilla JS + SVG: no build step, no CDN, one Railway service.
   Charts follow the data-viz rules - one axis per chart, thin marks, a legend
   whenever two series share a plot, hover on everything. */

const TOKEN_KEY = "clipengine.token";
const state = { token: localStorage.getItem(TOKEN_KEY) || "", view: "overview", data: {}, openPost: null };

/* ---------- helpers ---------- */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const esc = (value) =>
  String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

const fmt = (n) => {
  if (n === null || n === undefined) return "—";
  const abs = Math.abs(n);
  if (abs >= 1e9) return (n / 1e9).toFixed(1).replace(/\.0$/, "") + "B";
  if (abs >= 1e6) return (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M";
  if (abs >= 1e3) return (n / 1e3).toFixed(1).replace(/\.0$/, "") + "k";
  return String(Math.round(n));
};

const pct = (v, digits = 1) => (v === null || v === undefined ? "—" : (v * 100).toFixed(digits) + "%");

const clock = (s) => {
  if (!s && s !== 0) return "—";
  const m = Math.floor(s / 60);
  const sec = Math.round(s % 60);
  return `${m}:${String(sec).padStart(2, "0")}`;
};

const ago = (iso) => {
  if (!iso) return "—";
  const diff = (Date.now() - new Date(iso).getTime()) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
};

/** Media served by our own API needs the token in the query string:
 *  a <video src> cannot carry a header. */
const media = (url) => {
  if (!url || !url.startsWith("/api/") || !state.token) return url;
  return `${url}${url.includes("?") ? "&" : "?"}token=${encodeURIComponent(state.token)}`;
};

const STATUS_STATE = {
  posted: "good", approved: "good", done: "good", active: "good", rendered: "good", clipped: "good",
  queued: "warning", new: "warning", registered: "warning", downloading: "warning",
  transcribing: "warning", detecting: "warning", ranking: "warning", rendering: "warning",
  failed: "critical", rejected: "critical", error: "critical",
  ignored: "serious",
};

const pill = (status) =>
  `<span class="pill" data-state="${STATUS_STATE[status] || ""}">${esc(status)}</span>`;

function toast(message, state = "") {
  const el = $("#toast");
  el.textContent = message;
  el.dataset.state = state;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => (el.hidden = true), 4200);
}

/* ---------- api ---------- */

async function api(path, options = {}) {
  const response = await fetch(`/api${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(state.token ? { "X-Dashboard-Token": state.token } : {}),
      ...(options.headers || {}),
    },
  });
  if (response.status === 401) {
    showGate(true);
    throw new Error("unauthorised");
  }
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail.slice(0, 300) || `HTTP ${response.status}`);
  }
  return response.status === 204 ? null : response.json();
}

/* ---------- charts ---------- */

const svgNS = "http://www.w3.org/2000/svg";
const HEAT_STEPS = ["--heat-100", "--heat-250", "--heat-400", "--heat-550", "--heat-700"];

function tip(html, event) {
  const el = $("#tooltip");
  el.innerHTML = html;
  el.hidden = false;
  const pad = 14;
  const rect = el.getBoundingClientRect();
  let x = event.clientX + pad;
  let y = event.clientY + pad;
  if (x + rect.width > window.innerWidth) x = event.clientX - rect.width - pad;
  if (y + rect.height > window.innerHeight) y = event.clientY - rect.height - pad;
  el.style.left = `${Math.max(4, x)}px`;
  el.style.top = `${Math.max(4, y)}px`;
}
const untip = () => ($("#tooltip").hidden = true);

/** Sparkline: bare trend line for a table cell. */
function sparkline(series, width = 108, height = 28) {
  const points = (series || []).map((p) => p.views || 0);
  if (points.length < 2) return `<span class="heat-empty">—</span>`;
  const min = Math.min(...points);
  const max = Math.max(...points);
  const span = max - min || 1;
  const step = width / (points.length - 1);
  const d = points
    .map((v, i) => `${i ? "L" : "M"}${(i * step).toFixed(1)},${(height - 3 - ((v - min) / span) * (height - 6)).toFixed(1)}`)
    .join(" ");
  const last = points[points.length - 1];
  const lastY = height - 3 - ((last - min) / span) * (height - 6);
  return `<svg class="chart" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" role="img"
      aria-label="views trend, latest ${fmt(last)}">
    <path d="${d}" fill="none" stroke="var(--s1)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
    <circle cx="${width}" cy="${lastY.toFixed(1)}" r="2.5" fill="var(--s1)"/>
  </svg>`;
}

/** Most-replayed strip: sequential blue, one hue, light to dark. */
function heatStrip(heat, duration) {
  if (!heat || !heat.length) return `<span class="heat-empty">no heatmap</span>`;
  const buckets = 40;
  const total = duration || heat[heat.length - 1].end || 1;
  const cells = [];
  for (let i = 0; i < buckets; i++) {
    const from = (i / buckets) * total;
    const to = ((i + 1) / buckets) * total;
    const inside = heat.filter((m) => m.end > from && m.start < to);
    const value = inside.length ? Math.max(...inside.map((m) => m.value)) : 0;
    const step = HEAT_STEPS[Math.min(HEAT_STEPS.length - 1, Math.floor(value * HEAT_STEPS.length))];
    cells.push(
      `<i style="background: var(${step})" data-tip="${clock(from)} · replay ${Math.round(value * 100)}%"></i>`
    );
  }
  return `<div class="heat" role="img" aria-label="most replayed moments">${cells.join("")}</div>`;
}

/** Horizontal bars: one series, direct labels, 2px gaps. */
function barChart(rows, { valueKey = "value", labelKey = "label", format = fmt } = {}) {
  if (!rows.length) return `<div class="empty">Nothing posted yet.</div>`;
  const max = Math.max(...rows.map((r) => r[valueKey] || 0)) || 1;
  const barH = 22;
  const gap = 10;
  const labelW = 96;
  const height = rows.length * (barH + gap);
  const width = 520;
  const trackW = width - labelW - 66;

  const bars = rows
    .map((row, i) => {
      const value = row[valueKey] || 0;
      const w = Math.max(3, (value / max) * trackW);
      const y = i * (barH + gap);
      return `<g class="bar" data-label="${esc(row[labelKey])}" data-value="${format(value)}">
        <text x="0" y="${y + barH / 2 + 4}" font-size="12.5" fill="var(--ink-2)">${esc(row[labelKey])}</text>
        <rect x="${labelW}" y="${y + 2}" width="${w}" height="${barH - 4}" rx="4" fill="var(--s1)"/>
        <text x="${labelW + w + 8}" y="${y + barH / 2 + 4}" font-size="12.5"
          font-variant-numeric="tabular-nums" fill="var(--ink)">${format(value)}</text>
      </g>`;
    })
    .join("");

  return `<svg class="chart" viewBox="0 0 ${width} ${height}" height="${height}" role="img"
      aria-label="average views per platform">${bars}</svg>`;
}

/** Views over time: single series, crosshair + tooltip, last point labelled. */
function lineChart(series, { height = 210, label = "views" } = {}) {
  if (!series || series.length < 2) {
    return `<div class="empty">Not enough snapshots yet — readings are taken at 5m, 15m, 30m, 1h, 3h, 6h, 12h, 24h and 48h after posting.</div>`;
  }
  const width = 640;
  const pad = { top: 14, right: 54, bottom: 26, left: 46 };
  const plotW = width - pad.left - pad.right;
  const plotH = height - pad.top - pad.bottom;

  const times = series.map((p) => new Date(p.t).getTime());
  const values = series.map((p) => p.views || 0);
  const t0 = times[0];
  const t1 = times[times.length - 1] || t0 + 1;
  const vMax = Math.max(...values) || 1;

  const x = (t) => pad.left + ((t - t0) / (t1 - t0 || 1)) * plotW;
  const y = (v) => pad.top + plotH - (v / vMax) * plotH;

  const ticks = [0, 0.5, 1].map((f) => {
    const value = vMax * f;
    return `<g>
      <line x1="${pad.left}" x2="${pad.left + plotW}" y1="${y(value)}" y2="${y(value)}"
        stroke="var(--grid)" stroke-width="1"/>
      <text x="${pad.left - 8}" y="${y(value) + 4}" text-anchor="end" font-size="11"
        font-variant-numeric="tabular-nums" fill="var(--muted)">${fmt(value)}</text>
    </g>`;
  }).join("");

  const d = series.map((p, i) => `${i ? "L" : "M"}${x(times[i]).toFixed(1)},${y(values[i]).toFixed(1)}`).join(" ");
  const area = `${d} L${x(t1).toFixed(1)},${(pad.top + plotH).toFixed(1)} L${x(t0).toFixed(1)},${(pad.top + plotH).toFixed(1)} Z`;

  const dots = series
    .map((p, i) => `<circle class="pt" cx="${x(times[i]).toFixed(1)}" cy="${y(values[i]).toFixed(1)}" r="9"
        fill="transparent" data-t="${esc(p.t)}" data-v="${values[i]}" data-l="${p.likes || 0}"/>`)
    .join("");

  const lastX = x(times[times.length - 1]);
  const lastY = y(values[values.length - 1]);

  return `<svg class="chart js-line" viewBox="0 0 ${width} ${height}" height="${height}" role="img"
      aria-label="${esc(label)} over time">
    ${ticks}
    <path d="${area}" fill="var(--s1)" opacity=".10"/>
    <path d="${d}" fill="none" stroke="var(--s1)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
    <circle cx="${lastX.toFixed(1)}" cy="${lastY.toFixed(1)}" r="4" fill="var(--s1)" stroke="var(--surface)" stroke-width="2"/>
    <text x="${(lastX + 8).toFixed(1)}" y="${(lastY + 4).toFixed(1)}" font-size="12"
      font-variant-numeric="tabular-nums" fill="var(--ink)">${fmt(values[values.length - 1])}</text>
    <line class="crosshair" y1="${pad.top}" y2="${pad.top + plotH}" stroke="var(--axis)" stroke-width="1" opacity="0"/>
    ${dots}
    <line x1="${pad.left}" x2="${pad.left + plotW}" y1="${pad.top + plotH}" y2="${pad.top + plotH}"
      stroke="var(--axis)" stroke-width="1"/>
  </svg>`;
}

/* ---------- views ---------- */

async function renderOverview() {
  const [data, feed, platforms] = await Promise.all([
    api("/overview"),
    api("/overview/activity?limit=12"),
    api("/analytics/platforms"),
  ]);
  state.data.overview = data;

  const quotaState = data.quota.pct > 85 ? "critical" : data.quota.pct > 60 ? "warning" : "";
  const spendPct = Math.min(100, (data.spend.estimate_month / data.spend.budget) * 100);
  const spendState = spendPct > 90 ? "critical" : spendPct > 70 ? "warning" : "";

  const kpis = [
    { label: "Awaiting review", value: data.clips.awaiting_review, sub: `${data.clips.approved} approved and ready` },
    { label: "Posted (24h)", value: data.posts.last_24h, sub: `${data.posts.total} all time` },
    { label: "Views tracked", value: fmt(data.posts.total_views), sub: "latest snapshot per post" },
    { label: "Videos tracked", value: data.tracking.total, sub: `${data.tracking.new} not yet clipped` },
    {
      label: "YouTube quota today",
      value: `${data.quota.pct}%`,
      sub: `${fmt(data.quota.youtube_used)} of ${fmt(data.quota.youtube_limit)} units`,
      meter: data.quota.pct,
      state: quotaState,
    },
    {
      label: "Spend this month",
      value: `$${data.spend.estimate_month}`,
      sub: `estimate against a $${data.spend.budget} budget`,
      meter: spendPct,
      state: spendState,
    },
  ];

  $("#kpis").innerHTML = kpis
    .map(
      (k) => `<div class="card kpi">
        <span class="label">${esc(k.label)}</span>
        <span class="value">${esc(k.value)}</span>
        <span class="sub">${esc(k.sub)}</span>
        ${k.meter !== undefined ? `<div class="meter"><span style="width:${Math.min(100, k.meter)}%" data-state="${k.state}"></span></div>` : ""}
      </div>`
    )
    .join("");

  $("#platform-chart").innerHTML = barChart(
    platforms.map((p) => ({ label: p.platform, value: p.avg_views })),
    {}
  );

  $("#activity").innerHTML = feed.length
    ? `<div class="checklist">${feed
        .map(
          (e) => `<div class="check-row">
            <div class="what"><span>${esc(e.text)}</span><small>${esc(e.kind)} · ${ago(e.at)}</small></div>
            ${pill(e.status)}
          </div>`
        )
        .join("")}</div>`
    : `<div class="empty">Nothing has run yet.</div>`;

  $("#env-label").textContent = `${data.config.env} · ${data.config.publisher} · storage ${data.config.storage}`;
}

async function renderTrending() {
  const filter = $("#trending-filter").value;
  const rows = await api(`/trending${filter ? `?status=${filter}` : ""}`);
  state.data.trending = rows;

  if (!rows.length) {
    $("#trending-body").innerHTML = `<div class="empty">Nothing tracked yet. Set your keywords in Settings, then hit <strong>Scan now</strong>.</div>`;
    return;
  }

  $("#trending-body").innerHTML = `<div class="table-wrap"><table>
    <thead><tr>
      <th>Video</th><th class="num">Score</th><th class="num">Views</th>
      <th class="num">Views/hr</th><th class="num">Like rate</th>
      <th>Trend</th><th>Most replayed</th><th>Hot moment</th><th></th>
    </tr></thead>
    <tbody>${rows.map(trendingRow).join("")}</tbody>
  </table></div>`;
}

function trendingRow(v) {
  const hot = (v.hot_segments || [])[0];
  return `<tr data-id="${v.id}">
    <td>
      <div class="title-cell">
        ${v.thumbnail_url ? `<img src="${esc(v.thumbnail_url)}" alt="" loading="lazy">` : ""}
        <div>
          <div class="t"><a href="${esc(v.url)}" target="_blank" rel="noopener">${esc(v.title || v.url)}</a></div>
          <div class="c">${esc(v.channel_title || "")} · ${clock(v.duration_s)} · ${ago(v.published_at)}</div>
        </div>
      </div>
    </td>
    <td class="num"><strong>${v.score ?? "—"}</strong></td>
    <td class="num">${fmt(v.views)}</td>
    <td class="num">${fmt(v.velocity_vph)}</td>
    <td class="num">${pct(v.like_rate)}</td>
    <td>${sparkline(v.series)}</td>
    <td>${v.has_heatmap
      ? `<button class="btn btn-sm js-heat" data-id="${v.id}">show curve</button>`
      : `<span class="heat-empty">none yet</span>`}</td>
    <td class="hot-seg">${hot ? `${clock(hot.start_s)}–${clock(hot.end_s)}` : "—"}</td>
    <td>
      <div class="row" style="gap: 6px; flex-wrap: nowrap;">
        <button class="btn btn-sm btn-primary js-clip" data-id="${v.id}" ${v.status !== "new" ? "disabled" : ""}>Clip</button>
        <button class="btn btn-sm js-ignore" data-id="${v.id}">Hide</button>
      </div>
    </td>
  </tr>`;
}

async function renderReview() {
  const clips = await api("/review/queue");
  $("#review-count").textContent = clips.length;
  $("#review-count").hidden = clips.length === 0;

  if (!clips.length) {
    $("#review-body").innerHTML = `<div class="empty">Queue is empty. Clips land here once a source finishes rendering.</div>`;
    return;
  }

  $("#review-body").innerHTML = `<div class="review-grid">${clips
    .map(
      (c) => `<div class="card review-card" data-id="${c.id}">
        ${c.url ? `<video controls preload="metadata" src="${esc(media(c.url))}"></video>` : `<div class="empty">No file</div>`}
        <div class="meta">
          <span>${clock(c.duration_s)}</span>
          <span>${c.start_s !== null ? `${clock(c.start_s)}–${clock(c.end_s)}` : ""}</span>
          <span>score ${c.predicted_score === null || c.predicted_score === undefined ? "—" : Math.round(c.predicted_score)}</span>
          ${pill(c.status)}
        </div>
        <div class="field">
          <label for="title-${c.id}">Title</label>
          <input type="text" id="title-${c.id}" value="${esc(c.title || "")}">
        </div>
        <div class="field">
          <label for="tags-${c.id}">Hashtags</label>
          <input type="text" id="tags-${c.id}" value="${esc((c.hashtags || []).join(" "))}">
        </div>
        <div class="row">
          <button class="btn btn-sm js-save" data-id="${c.id}">Save</button>
          <button class="btn btn-sm btn-primary js-approve" data-id="${c.id}">Approve</button>
          <button class="btn btn-sm js-reject" data-id="${c.id}">Reject</button>
        </div>
      </div>`
    )
    .join("")}</div>`;
}

async function renderPosts() {
  const posts = await api("/analytics/posts");
  state.data.posts = posts;

  if (!posts.length) {
    $("#posts-body").innerHTML = `<div class="empty">Nothing posted yet. Approve a clip, then either post it by hand or turn on autopost.</div>`;
    $("#post-detail").hidden = true;
    return;
  }

  $("#posts-body").innerHTML = `<div class="table-wrap"><table>
    <thead><tr>
      <th>Clip</th><th>Platform</th><th>Status</th>
      <th class="num">Views</th><th class="num">Like rate</th><th>Posted</th><th>Trend</th>
    </tr></thead>
    <tbody>${posts
      .map(
        (p) => `<tr data-post="${p.id}" style="cursor: pointer;">
          <td>${esc(p.title || `clip ${p.clip_id}`)}${p.url ? ` <a href="${esc(p.url)}" target="_blank" rel="noopener">↗</a>` : ""}</td>
          <td>${esc(p.platform)}</td>
          <td>${pill(p.status)}${p.error ? `<div class="c" style="font-size:12px;color:var(--muted)">${esc(p.error.slice(0, 90))}</div>` : ""}</td>
          <td class="num">${fmt(p.views)}</td>
          <td class="num">${pct(p.like_rate)}</td>
          <td>${ago(p.posted_at)}</td>
          <td>${sparkline(p.series)}</td>
        </tr>`
      )
      .join("")}</tbody>
  </table></div>`;

  if (state.openPost) showPostDetail(state.openPost);
}

function showPostDetail(postId) {
  const post = (state.data.posts || []).find((p) => p.id === postId);
  if (!post) return;
  state.openPost = postId;
  $("#post-detail").hidden = false;
  $("#post-detail").innerHTML = `
    <div class="chart-head">
      <h3>${esc(post.title || `clip ${post.clip_id}`)} — views over time</h3>
      <span class="hint">${esc(post.platform)} · posted ${ago(post.posted_at)}</span>
    </div>
    ${lineChart(post.series)}
    <div class="legend">
      <span>likes ${fmt(post.likes)}</span>
      <span>comments ${fmt(post.comments)}</span>
      <span>${post.series.length} snapshots</span>
    </div>`;
}

async function renderSources() {
  const rows = await api("/sources");
  $("#sources-body").innerHTML = rows.length
    ? `<div class="table-wrap"><table>
        <thead><tr><th>Source</th><th>Licence</th><th>Status</th><th class="num">Length</th><th>Added</th></tr></thead>
        <tbody>${rows
          .map(
            (s) => `<tr>
              <td><a href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.title || s.url)}</a></td>
              <td>${esc(s.license)}</td>
              <td>${pill(s.status)}${s.error ? `<div style="font-size:12px;color:var(--critical)">${esc(s.error.slice(0, 120))}</div>` : ""}</td>
              <td class="num">${clock(s.duration_s)}</td>
              <td>${ago(s.created_at)}</td>
            </tr>`
          )
          .join("")}</tbody>
      </table></div>`
    : `<div class="empty">No sources yet. Add one above, or clip something from the Trending tab.</div>`;
}

async function renderSettings() {
  const data = await api("/settings");
  state.data.settings = data;

  const CONNECTIONS = [
    ["database", "Postgres", "pipeline state and analytics"],
    ["redis", "Redis", "job queue and scheduler locks"],
    ["r2", "Cloudflare R2", "clip storage (falls back to local disk)"],
    ["anthropic", "Anthropic", "moment detection, ranking, captions"],
    ["transcription", "Transcription", "word-level timestamps"],
    ["youtube_read", "YouTube Data API", "trend scouting — free"],
    ["youtube_upload", "YouTube OAuth", "posting Shorts yourself"],
    ["upload_post", "Upload-Post", "TikTok, Instagram, Facebook, Snapchat"],
  ];

  $("#connections").innerHTML = CONNECTIONS.map(([key, name, why]) => {
    const on = data.connected[key];
    return `<div class="check-row">
      <div class="what"><span>${esc(name)}</span><small>${esc(why)}</small></div>
      <span class="pill" data-state="${on ? "good" : "warning"}">${on ? "connected" : "not set"}</span>
    </div>`;
  }).join("");

  const env = data.env;
  const ENV_ROWS = [
    ["Niche", env.default_niche],
    ["Scout", env.scout_enabled ? `every ${env.scout_interval_minutes} min` : "off"],
    ["Scout keywords (env)", (env.scout_keywords || []).join(", ") || "none"],
    ["Source length filter", env.scout_video_duration],
    ["Metrics", `every ${env.metrics_interval_minutes} min`],
    ["Autopost", env.autopost_enabled ? `${env.autopost_per_day}/day` : "off — approve and post by hand"],
    ["Publisher", env.publisher],
    ["Clips per source", env.top_n_clips],
    ["Clip length", `${env.clip_length_s[0]}–${env.clip_length_s[1]}s`],
    ["Transcription", env.transcribe_provider],
    ["Model", env.model],
  ];
  $("#env-config").innerHTML = ENV_ROWS.map(
    ([k, v]) => `<div class="check-row"><div class="what"><span>${esc(k)}</span></div>
      <span class="mono" style="color: var(--ink-2)">${esc(v)}</span></div>`
  ).join("");

  $("#niches-body").innerHTML = data.niches.length
    ? `<div class="checklist">${data.niches
        .map(
          (n) => `<div class="check-row">
            <div class="what"><span>${esc(n.name)}</span><small>${esc((n.keywords || []).join(", ") || "no keywords")}</small></div>
            <button class="btn btn-sm js-edit-niche" data-name="${esc(n.name)}"
              data-keywords="${esc((n.keywords || []).join(", "))}">Edit</button>
          </div>`
        )
        .join("")}</div>`
    : `<div class="empty">No niches yet.</div>`;

  $("#accounts-body").innerHTML = data.accounts.length
    ? `<div class="checklist">${data.accounts
        .map(
          (a) => `<div class="check-row">
            <div class="what"><span>${esc(a.platform)}</span><small>${esc(a.handle)}</small></div>
            <div class="row">${pill(a.status)}
              <button class="btn btn-sm js-del-account" data-id="${a.id}">Remove</button></div>
          </div>`
        )
        .join("")}</div>`
    : `<div class="empty">No accounts yet. Approved clips need at least one to know where to go.</div>`;
}

const RENDERERS = {
  overview: renderOverview,
  trending: renderTrending,
  review: renderReview,
  posts: renderPosts,
  sources: renderSources,
  settings: renderSettings,
};

/* ---------- routing ---------- */

async function show(view) {
  if (!RENDERERS[view]) view = "overview";
  state.view = view;
  $$(".view").forEach((el) => (el.hidden = el.id !== `view-${view}`));
  $$("#tabs a").forEach((a) =>
    a.dataset.view === view ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current")
  );
  try {
    await RENDERERS[view]();
  } catch (err) {
    if (err.message !== "unauthorised") toast(`Could not load ${view}: ${err.message}`, "critical");
  }
}

const route = () => show((location.hash || "#/overview").replace("#/", ""));

/* ---------- gate ---------- */

function showGate(show) {
  $("#gate").hidden = !show;
  $("#app").hidden = show;
  if (show) $("#gate-token").focus();
}

async function unlock() {
  const token = $("#gate-token").value.trim();
  state.token = token;
  localStorage.setItem(TOKEN_KEY, token);
  try {
    await api("/overview");
    $("#gate-error").hidden = true;
    showGate(false);
    route();
  } catch {
    $("#gate-error").hidden = false;
  }
}

/* ---------- events ---------- */

document.addEventListener("click", async (event) => {
  const target = event.target.closest("button, tr[data-post], .js-edit-niche");
  if (!target) return;

  try {
    if (target.id === "refresh") return route();
    if (target.id === "theme") {
      const now = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      document.documentElement.dataset.theme = now;
      localStorage.setItem("clipengine.theme", now);
      return;
    }
    if (target.id === "gate-go") return unlock();

    if (target.id === "scan-now") {
      target.disabled = true;
      const result = await api("/trending/scan", { method: "POST" });
      toast(result.queued ? "Scan queued — results appear as the worker finishes." : `Scan done: ${result.discovered} videos.`);
      target.disabled = false;
      return renderTrending();
    }

    if (target.classList.contains("js-clip")) {
      const licence = prompt(
        "Licence for this source — you must have the right to clip it.\n\nown / licensed / campaign / permitted",
        "campaign"
      );
      if (!licence) return;
      await api(`/trending/${target.dataset.id}/clip`, {
        method: "POST",
        body: JSON.stringify({ license: licence.trim() }),
      });
      toast("Sent to the pipeline. Watch the Sources tab.");
      return renderTrending();
    }

    if (target.classList.contains("js-ignore")) {
      await api(`/trending/${target.dataset.id}/ignore`, { method: "POST" });
      return renderTrending();
    }

    if (target.classList.contains("js-heat")) {
      const video = await api(`/trending/${target.dataset.id}`);
      const cell = target.closest("td");
      cell.innerHTML = heatStrip(video.heatmap, video.duration_s);
      return;
    }

    if (target.classList.contains("js-save") || target.classList.contains("js-approve")) {
      const id = target.dataset.id;
      const title = $(`#title-${id}`).value;
      const hashtags = $(`#tags-${id}`).value.split(/\s+/).filter(Boolean);
      await api(`/review/${id}`, { method: "PATCH", body: JSON.stringify({ title, hashtags }) });
      if (target.classList.contains("js-approve")) {
        await api(`/review/${id}/approve`, { method: "POST" });
        toast("Approved.");
        return renderReview();
      }
      return toast("Saved.");
    }

    if (target.classList.contains("js-reject")) {
      await api(`/review/${target.dataset.id}/reject`, { method: "POST" });
      return renderReview();
    }

    if (target.id === "src-add") {
      const url = $("#src-url").value.trim();
      if (!url) return toast("Paste a video URL first.", "critical");
      await api("/sources", {
        method: "POST",
        body: JSON.stringify({
          url,
          license: $("#src-licence").value,
          kind: "youtube",
          niche: $("#src-niche").value.trim() || null,
        }),
      });
      $("#src-url").value = "";
      toast("Queued for ingest.");
      return renderSources();
    }

    if (target.id === "niche-save") {
      const name = $("#niche-name").value.trim();
      if (!name) return toast("Name the niche first.", "critical");
      await api("/settings/niches", {
        method: "POST",
        body: JSON.stringify({
          name,
          keywords: $("#niche-keywords").value.split(",").map((k) => k.trim()).filter(Boolean),
        }),
      });
      toast("Saved. Set SCOUT_KEYWORDS in Railway to make the scheduler use them.");
      return renderSettings();
    }

    if (target.classList.contains("js-edit-niche")) {
      $("#niche-name").value = target.dataset.name;
      $("#niche-keywords").value = target.dataset.keywords;
      return $("#niche-keywords").focus();
    }

    if (target.id === "acct-add") {
      const handle = $("#acct-handle").value.trim();
      if (!handle) return toast("Add the handle.", "critical");
      await api("/settings/accounts", {
        method: "POST",
        body: JSON.stringify({ platform: $("#acct-platform").value, handle }),
      });
      $("#acct-handle").value = "";
      return renderSettings();
    }

    if (target.classList.contains("js-del-account")) {
      await api(`/settings/accounts/${target.dataset.id}`, { method: "DELETE" });
      return renderSettings();
    }

    if (target.dataset.post) return showPostDetail(Number(target.dataset.post));
  } catch (err) {
    if (err.message !== "unauthorised") toast(err.message, "critical");
  }
});

document.addEventListener("change", (event) => {
  if (event.target.id === "trending-filter") renderTrending();
});

/* hover layer: heat cells, bars, line points */
document.addEventListener("mouseover", (event) => {
  const cell = event.target.closest("[data-tip]");
  if (cell) return tip(esc(cell.dataset.tip), event);

  const bar = event.target.closest(".bar");
  if (bar) {
    return tip(
      `<div class="tt-title">${esc(bar.dataset.label)}</div>
       <div class="tt-row"><span>avg views</span><span>${esc(bar.dataset.value)}</span></div>`,
      event
    );
  }

  const point = event.target.closest(".pt");
  if (point) {
    const svg = point.closest("svg");
    const crosshair = svg.querySelector(".crosshair");
    if (crosshair) {
      crosshair.setAttribute("x1", point.getAttribute("cx"));
      crosshair.setAttribute("x2", point.getAttribute("cx"));
      crosshair.setAttribute("opacity", "1");
    }
    return tip(
      `<div class="tt-title">${new Date(point.dataset.t).toLocaleString()}</div>
       <div class="tt-row"><span>views</span><span>${fmt(Number(point.dataset.v))}</span></div>
       <div class="tt-row"><span>likes</span><span>${fmt(Number(point.dataset.l))}</span></div>`,
      event
    );
  }
});

document.addEventListener("mouseout", (event) => {
  if (event.target.closest("[data-tip], .bar, .pt")) {
    untip();
    $$(".crosshair").forEach((c) => c.setAttribute("opacity", "0"));
  }
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !$("#gate").hidden) unlock();
  if (event.key === "Escape") untip();
});

window.addEventListener("hashchange", route);

/* ---------- boot ---------- */

(async function boot() {
  const savedTheme = localStorage.getItem("clipengine.theme");
  if (savedTheme) document.documentElement.dataset.theme = savedTheme;

  try {
    await api("/overview");
    showGate(false);
    route();
    // Keep the review badge live without a full re-render.
    setInterval(async () => {
      try {
        const clips = await api("/review/queue");
        $("#review-count").textContent = clips.length;
        $("#review-count").hidden = clips.length === 0;
      } catch { /* ignore polling errors */ }
    }, 60000);
  } catch {
    showGate(true);
  }
})();
