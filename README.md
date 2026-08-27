# clip-engine

Takes a long source video **you have the right to use**, finds the best
moments, and cuts them into captioned 9:16 clips.

**Built: the full loop.** Finds source videos, clips them, queues them for a
human, posts the approved ones, and pulls the numbers back in — with a dashboard
over the top.

```
scout → ingest → transcribe → detect moments → rank → render → review → publish → metrics
```

Deploying it: **[docs/DEPLOY.md](docs/DEPLOY.md)** — every service you need to
sign up to, what each key is for, and the monthly bill (~$47–73, inside a $100
budget).

---

## The studio

As well as clipping other people's videos, this makes its own: real
public-record audio — Apollo 13's flight loop, the Nixon tapes, the Pentagon's
UAP releases — with an AI narrator over the top, free stock footage underneath,
and a drawn instrument overlay that is identical on every post. That overlay is
the point: the footage changes every video, the scanlines and telemetry and
timecode do not, and that is what makes a post recognisable in a feed.

Press **Generate video** in the dashboard's Studio tab. Nothing posts on its
own. Two optional keys (`OPENAI_API_KEY`, `PEXELS_API_KEY`) add the narration
and the footage; without them a render still happens and tells you what is
missing. See [docs/DEPLOY.md](docs/DEPLOY.md#the-studio).

## Quickstart (Phase 1, local, no cloud accounts)

Needs Python 3.11 and **ffmpeg** (a system binary, not a pip package).

```bash
# 1. ffmpeg
brew install ffmpeg          # macOS
sudo apt install ffmpeg      # Debian/Ubuntu

# 2. project
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'

# 3. keys — only two are needed for Phase 1
cp .env.example .env
#   ANTHROPIC_API_KEY=...
#   ASSEMBLYAI_API_KEY=...        (or TRANSCRIBE_PROVIDER=deepgram + DEEPGRAM_API_KEY)

# 4. run it on one source
python scripts/run_pipeline.py "https://www.youtube.com/watch?v=YOUR_VIDEO" --license own
```

Top 3 clips land in `./out` as `01-<slug>.mp4`, each with a `.txt` sidecar
(title, caption, hashtags, timestamps, why it was picked) plus a combined
`out/clips.json`.

With `DATABASE_URL`, `REDIS_URL` and `R2_*` unset the CLI writes no rows and
keeps files under `.storage/` — that is the intended Phase 1 setup.

### Useful flags

```bash
python scripts/run_pipeline.py URL --niche fitness -n 5     # 5 clips, niche context
python scripts/run_pipeline.py --file ./local.mp4           # skip the download
python scripts/run_pipeline.py URL --retranscribe           # ignore the cached transcript
python scripts/run_pipeline.py URL --min-s 20 --max-s 45    # clip length bounds
python scripts/run_pipeline.py URL --no-metadata            # skip title/hashtag generation
python scripts/run_pipeline.py URL --keep-work -v           # keep intermediates, debug logs
```

The transcript is cached in `.work/<slug>/transcript.json`, so re-running while
tuning the prompts in `core/llm.py` costs Claude calls only — you pay the
transcription bill once per source.

### The go/no-go

Watch all three clips. **If you would not post at least 3 of them without
editing, stop and fix the detect/rank prompts in `core/llm.py` before building
anything else.** That judgment is the whole project; everything downstream is
worthless until it passes.

---

## Phase status

| Phase | What | Status |
|---|---|---|
| 0 | Scaffold: schema, migrations, api + worker, R2/Postgres/Redis wiring | done |
| 1 | Core clip quality, CLI only — **the go/no-go** | done |
| 2 | Review dashboard: watch, edit metadata, approve/reject | done |
| 3 | Publishing (YouTube Shorts, or a reseller) + metric snapshots | done |
| 4 | Trend scouting: what's winning, and which moment inside it | done |

Publishing stays **off** until you turn it on (`PUBLISHER=manual`,
`AUTOPOST_ENABLED=false`). Nothing is ever posted without a human approving the
clip in the review queue first.

## The dashboard

Six tabs, served by the same FastAPI service — no second deploy, no build step.

| Tab | What's on it |
|---|---|
| **Overview** | Clips awaiting review, posts in 24h, views tracked, YouTube quota used, spend vs your $100 budget, average views per platform, live activity feed |
| **Trending** | What the scout found: score, views, views/hour, like rate, a views sparkline, the most-replayed curve and the hot moment inside each video. One click sends a video into the pipeline |
| **Review** | Play each clip, edit title and hashtags, approve or reject |
| **Posts** | Every post with its metric snapshots; click a row for views-over-time |
| **Sources** | Add a URL with a licence, watch it move through the pipeline |
| **Settings** | Which services are connected, the operating config, niche keywords, and the accounts clips get posted to |

Gated behind `DASHBOARD_TOKEN` — set it, or anyone with the URL can drive your
pipeline. Charts are hand-rolled SVG on a colourblind-validated palette, and the
whole thing works in light and dark.

## Where the trend data comes from

All free, no scrapers, no vendor:

- **YouTube Data API** — search and public counters. 10,000 units/day; a search
  costs 100, so the scout batches keywords and runs every 6 hours by default.
  `core/youtube.py` refuses a call that would blow the daily budget.
- **yt-dlp `heatmap`** — YouTube's most-replayed curve, ~100 segments with a 0–1
  intensity, for any public video. This is what "which bit do I clip" runs on.
- **Your own posts** — metric snapshots at 5m/15m/30m/1h/3h/6h/12h/24h/48h.
  The best signal you will ever have, because it is measured on your audience.

Other people's retention and watch time are not obtainable at any price — they
are never sent to a viewer's browser. That is the ceiling, and it is not a
skill problem.

---

## Non-negotiables baked into the code

- **Source rights.** Every source carries a `license` of
  `own | licensed | campaign | permitted | none`. `check_license()` refuses
  `none` when `ENV=prod` — in ingest and in `POST /sources` alike. Scraping a
  big streamer and reposting is the thing that kills the whole operation, so
  the schema makes it the hard path.
- **Posting is gated.** Official APIs need app review and mostly
  Business/Creator accounts. YouTube Data API is the easiest and is built
  first; IG Content Publishing needs Meta app review; TikTok's Content Posting
  API needs an app audit and **unaudited apps can only post drafts**; Snapchat
  has no practical public posting API — manual only.
- **Anti-ban.** One brand, one or two platforms. No mass multi-account
  automation from one machine.
- **Metrics.** Official insights APIs only. Never scrape the app UI — that is
  exactly what flags accounts.

---

## Deploying Phase 0 (Railway)

Services: `web`, `worker`, `scheduler`, Postgres, Redis — all three app
services build from the same `Dockerfile`, which installs ffmpeg (render and
ingest need it at runtime).

```bash
alembic upgrade head          # create the schema
```

- `web`: `./scripts/start.sh web` — migrations, then the API and dashboard
- `worker`: `./scripts/start.sh worker`
- `scheduler`: `./scripts/start.sh scheduler`

One entrypoint, three roles. Only `web` migrates; each role checks the
variables it needs and exits with a one-line reason rather than a traceback.

Acceptance check — writes and reads a throwaway Postgres row and R2 object:

```bash
python scripts/check_infra.py
curl -s https://<your-app>/health
```

`/health` reports which subsystems are actually wired up:

```json
{"status":"ok","env":"prod","db":true,"db_configured":true,
 "redis_configured":true,"storage":"r2"}
```

---

## API (used by the Phase 2 dashboard)

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | liveness + subsystem readout |
| POST | `/sources` | register a source (`url`, `license`, `kind`, `niche`), enqueue ingest |
| GET | `/sources`, `/sources/{id}` | ingest status |
| GET | `/clips?status=` | rendered clips with signed URLs |
| GET | `/review/queue` | clips waiting on a human |
| PATCH | `/review/{id}` | edit title / hashtags / caption style |
| POST | `/review/{id}/approve`, `/review/{id}/reject` | decide |
| GET | `/analytics/summary`, `/analytics/posts` | counts now, real metrics in Phase 3 |

Routes needing Postgres or Redis return **503** with the missing variable named
when they are not configured.

---

## Layout

```
api/          FastAPI app, JSON API under /api
  static/         the dashboard (vanilla JS + SVG charts, no build step)
  routes/         overview, trending, sources, clips, review, analytics, settings
core/
  config.py       env (everything optional; each subsystem is probed)
  db.py           SQLAlchemy session, engine created lazily
  models.py       the 9 tables
  storage.py      R2 via boto3, local-directory fallback
  llm.py          Claude client + the detect / rank / metadata prompts
  selection.py    windowing, clamping, overlap dedupe, top-N  (pure, tested)
  captions.py     word timestamps -> .ass karaoke captions    (pure, tested)
  ffmpeg_ops.py   probe, audio extract, smart center crop, render
  transcription.py  AssemblyAI / Deepgram word-level timestamps
  youtube.py      YouTube Data API + daily quota accounting
  heatmap.py      most-replayed curve via yt-dlp
  scoring.py      velocity, like-rate, composite score, hot-segment finder
  publishers/     manual | youtube (native OAuth) | upload_post (reseller)
worker/
  queue.py        RQ queues, one per stage
  scheduler.py    the 24/7 heartbeat: scout, metrics, autopost
  tasks/          scout, ingest, transcribe, detect_moments, rank, render,
                  publish, collect_metrics
migrations/     alembic
scripts/        run_pipeline.py (one video, end to end)
                check_infra.py (Postgres + R2 + ffmpeg + Redis)
                check_publisher.py (publishing credentials)
                seed_demo.py (fake data so the dashboard has something to draw)
tests/          pure-logic tests + ffmpeg-gated render/CLI integration tests
```

Each task module exposes a plain function (used by the CLI) and an RQ
entrypoint `run(id)` that reads/writes Postgres and enqueues the next stage —
so the same code path serves both the weekend CLI and the deployed worker.

### How the pieces work

- **Reframing** is a *smart* center crop: a handful of frames are sampled from
  the segment, per-column energy (motion between frames + horizontal contrast)
  gives a focus point, and the 1080-wide window is placed there, pulled back
  towards centre so one bright edge cannot shove the crop into a corner. Face /
  active-speaker tracking is a later upgrade and must not block Phase 1.
- **Captions** are burned from an `.ass` file, one Dialogue event per word (the
  line redrawn with a different word lit) rather than `\k` tags — identical
  across libass versions. Text stays out of the top 15% and bottom 20% where
  the platform UI sits, and a finished line holds briefly into a pause so it
  does not blink.
- **Windowing** splits the transcript into overlapping ~6 minute windows so a
  moment on a boundary is still seen whole; overlapping candidates are removed
  by greedy non-max suppression on the time axis.
- **Structured output** — detect/rank/metadata all request a strict JSON
  schema, and fall back to parsing JSON out of prose if the model or SDK does
  not support it.

---

## Env vars

| Var | Needed for |
|---|---|
| `ANTHROPIC_API_KEY` | Phase 1 (detection, ranking, metadata) |
| `ASSEMBLYAI_API_KEY` *or* `DEEPGRAM_API_KEY` | Phase 1 (transcription) |
| `ANTHROPIC_MODEL`, `ANTHROPIC_EFFORT`, `TRANSCRIBE_PROVIDER` | optional overrides |
| `YOUTUBE_API_KEY` | trend scouting (free) |
| `DASHBOARD_TOKEN` | **required in prod** — the dashboard is public without it |
| `DATABASE_URL`, `REDIS_URL` | deploy / queued pipeline |
| `SCOUT_KEYWORDS`, `SCOUT_INTERVAL_MINUTES` | what the scout looks for, how often |
| `PUBLISHER`, `AUTOPOST_ENABLED` | `manual` / `youtube` / `meta` / `upload_post` |
| `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET` | object storage (else local dir) |
| `ENV` | `prod` refuses `license=none` on ingest |
| `YOUTUBE_CLIENT_ID/SECRET/REFRESH_TOKEN` | posting Shorts yourself (free) |
| `META_ACCESS_TOKEN`, `INSTAGRAM_USER_ID`, `THREADS_*`, `FACEBOOK_PAGE_ID` | posting Reels / Threads yourself (free) |
| `UPLOAD_POST_API_KEY`, `UPLOAD_POST_USER` | TikTok / Snapchat via a reseller |

Sources can also be **uploaded as files** rather than fetched by URL — the
download is the only stage that depends on a platform allowing it, and the
rest of the pipeline does not care where the video came from.

Full setup, including how to get each one: **[docs/DEPLOY.md](docs/DEPLOY.md)**.

---

## Tests

```bash
pytest          # 66 tests; render/CLI integration tests skip without ffmpeg
ruff check .
```

The integration tests build a synthetic source with ffmpeg and stub the model
calls, so the suite needs no API keys and no network.

---

## Cost per source

Transcription ~$0.15–0.40 per hour of audio, a few cents of Claude calls, R2
storage cheap with no egress, Railway ~$5–20/mo — **under ~$1 per source**. The
expensive resource is your attention on Phase 1 clip quality. Spend it there.
