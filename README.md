# clip-engine

Takes a long source video **you have the right to use**, finds the best
moments, and cuts them into captioned 9:16 clips.

**Built so far: Phase 0 (scaffold) + Phase 1 (core clip quality, CLI only).**
No dashboard, no auto-posting, no ranking/ML layer — those are Phases 2, 3 and
4 and are deliberately not started. See [Phase status](#phase-status).

```
ingest → transcribe → detect moments → rank → render → ./out/*.mp4
```

---

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
| 2 | Review dashboard (Next.js): watch, edit metadata, approve/reject | not started (`dashboard/`) |
| 3 | Publishing (YouTube Shorts → IG Reels) + metric snapshots | not started (stubs only) |
| 4 | Intelligence layer — only after ~100 posted clips | not started |

`worker/tasks/publish.py` and `worker/tasks/collect_metrics.py` raise
`NotImplementedError` on purpose. Phases 1–2 are human-in-the-loop: the bot
renders and writes the metadata, a human approves and posts.

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

Services: `api`, `worker`, Postgres, Redis. `nixpacks.toml` installs ffmpeg —
without it, render and ingest fail at runtime.

```bash
alembic upgrade head          # create the schema
```

- `web`: `uvicorn api.main:app --host 0.0.0.0 --port $PORT`
- `worker`: `python -m worker.queue` (all queues; pass names to narrow)

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
api/          FastAPI app + routes (sources, clips, review, analytics)
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
worker/
  queue.py        RQ queues, one per stage
  tasks/          ingest, transcribe, detect_moments, rank, render,
                  publish (Phase 3 stub), collect_metrics (Phase 3 stub)
migrations/     alembic
scripts/        run_pipeline.py (Phase 1 CLI), check_infra.py (Phase 0 check)
dashboard/      Phase 2, not started
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
| `DATABASE_URL`, `REDIS_URL` | Phase 0 deploy / queued pipeline |
| `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET` | object storage (else local dir) |
| `ENV` | `prod` refuses `license=none` on ingest |
| `YOUTUBE_CLIENT_ID/SECRET/REFRESH`, `IG_APP_ID/SECRET/TOKEN` | Phase 3 |

---

## Tests

```bash
pytest          # 46 tests; render/CLI integration tests skip without ffmpeg
ruff check .
```

The integration tests build a synthetic source with ffmpeg and stub the model
calls, so the suite needs no API keys and no network.

---

## Cost per source

Transcription ~$0.15–0.40 per hour of audio, a few cents of Claude calls, R2
storage cheap with no egress, Railway ~$5–20/mo — **under ~$1 per source**. The
expensive resource is your attention on Phase 1 clip quality. Spend it there.
