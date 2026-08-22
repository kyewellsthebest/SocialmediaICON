# Deploy to Railway

Everything below fits inside **$100/month**. Work top to bottom — the order is
"cheapest thing that proves the next step is worth paying for".

---

## 1. What to sign up to

### Required to run at all

| # | Service | What it does | What to copy | Cost |
|---|---|---|---|---|
| 1 | **Railway** — [railway.app](https://railway.app) | Hosts the API, worker, scheduler, Postgres and Redis | nothing (it injects `DATABASE_URL` and `REDIS_URL`) | ~$10–20/mo |
| 2 | **Anthropic** — [console.anthropic.com](https://console.anthropic.com) | Picks the moments, ranks them, writes titles | `ANTHROPIC_API_KEY` | ~$20/mo at 3 videos/day |
| 3 | **AssemblyAI** — [assemblyai.com](https://www.assemblyai.com) | Word-level transcripts for the karaoke captions | `ASSEMBLYAI_API_KEY` | ~$0.17/hr of audio ≈ $15/mo |

### Required for the trending tab (free)

| # | Service | What it does | What to copy | Cost |
|---|---|---|---|---|
| 4 | **Google Cloud** — [console.cloud.google.com](https://console.cloud.google.com) | YouTube Data API v3: what's winning in your niche | `YOUTUBE_API_KEY` | **free** (10,000 units/day) |

Create a project → **APIs & Services → Library** → enable *YouTube Data API v3*
→ **Credentials → Create credentials → API key**. That is the whole process.
Restrict the key to the YouTube Data API while you're there.

The most-replayed heatmaps need no signup at all — yt-dlp reads them.

### Pick one when you're ready to post

| # | Service | What it does | What to copy | Cost |
|---|---|---|---|---|
| 5a | **Google Cloud OAuth** (same project as #4) | Posts Shorts to your own channel | `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN` | free |
| 5b | **Upload-Post** — [upload-post.com](https://www.upload-post.com) | TikTok, Instagram, Facebook without the TikTok audit | `UPLOAD_POST_API_KEY`, `UPLOAD_POST_USER` | from ~$16/mo |
| 5c | **Zernio / Mallary** | Only route to Snapchat Spotlight | — | ~$50+/mo |

Start with 5a. It's free, it's the platform that also gives you trend data, and
it proves the loop before you pay a reseller.

### Optional

| Service | What it does | Cost |
|---|---|---|
| **Cloudflare R2** — [dash.cloudflare.com](https://dash.cloudflare.com) | Clip storage with zero egress fees | ~$2/mo |
| **Deepgram** | Alternative transcriber | ~$0.58/hr |
| **HeyGen** | AI presenter stitched onto clips | $1–4/min — measure before committing |

Without R2 the clips live on the container's disk. That's fine to start, but
Railway containers are replaceable — add R2 before you care about the archive.

---

## 2. Getting the YouTube refresh token (the fiddly one)

Only needed for 5a. Ten minutes, once.

1. Google Cloud → **APIs & Services → OAuth consent screen** → External →
   fill in the basics → add yourself as a **test user**.
2. **Credentials → Create credentials → OAuth client ID → Desktop app.**
   Copy the client ID and secret.
3. Visit this URL in a browser (substitute your client ID):

   ```
   https://accounts.google.com/o/oauth2/v2/auth
     ?client_id=YOUR_CLIENT_ID
     &redirect_uri=urn:ietf:wg:oauth:2.0:oob
     &response_type=code
     &scope=https://www.googleapis.com/auth/youtube.upload
     &access_type=offline
     &prompt=consent
   ```

4. Approve, copy the code, then exchange it:

   ```bash
   curl -s https://oauth2.googleapis.com/token \
     -d client_id=YOUR_CLIENT_ID \
     -d client_secret=YOUR_CLIENT_SECRET \
     -d code=THE_CODE \
     -d grant_type=authorization_code \
     -d redirect_uri=urn:ietf:wg:oauth:2.0:oob
   ```

5. Save `refresh_token` from the response as `YOUTUBE_REFRESH_TOKEN`.

`access_type=offline` and `prompt=consent` are both required — without them you
get an access token that expires in an hour and no refresh token.

---

## 3. Railway setup

```bash
railway login
railway init                     # or link an existing project
railway add --database postgres
railway add --database redis
```

Then in the Railway dashboard, create **three services from this repo**:

| Service | Start command | What it is |
|---|---|---|
| `web` | `alembic upgrade head && uvicorn api.main:app --host 0.0.0.0 --port $PORT` | API + dashboard |
| `worker` | `python -m worker.queue` | Runs the pipeline jobs |
| `scheduler` | `python -m worker.scheduler` | Fires scout, metrics and autopost on time |

`nixpacks.toml` installs ffmpeg — without it, ingest and render fail at runtime.
Only `web` needs a public domain.

### Environment variables

Set these on **all three** services (Railway shared variables are easiest):

```bash
ENV=prod
DASHBOARD_TOKEN=<a long random string>     # or the dashboard is open to anyone

ANTHROPIC_API_KEY=...
ANTHROPIC_MODEL=claude-sonnet-5
ASSEMBLYAI_API_KEY=...
YOUTUBE_API_KEY=...

DEFAULT_NICHE=your-niche
SCOUT_KEYWORDS=keyword one,keyword two,keyword three
SCOUT_INTERVAL_MINUTES=360
SCOUT_VIDEO_DURATION=medium

PUBLISHER=manual                            # switch to youtube / upload_post later
AUTOPOST_ENABLED=false
```

`DATABASE_URL` and `REDIS_URL` come from the Postgres and Redis plugins — 
reference them as `${{Postgres.DATABASE_URL}}` and `${{Redis.REDIS_URL}}`.

**Generate the token with something like:**

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

---

## 4. First run

1. Open your Railway URL. Enter the dashboard token.
2. **Settings** → check every connection you expect shows *connected*.
3. **Trending** → *Scan now*. Give it a minute, then refresh — you should see
   videos with scores, velocity and hot moments.
4. Pick a video **you have the right to clip** → *Clip* → choose the licence.
5. **Sources** shows it moving through download → transcribe → detect → render.
6. **Review** — watch the clips. This is the go/no-go. If you wouldn't post
   them, fix the prompts in `core/llm.py` before doing anything else.
7. Approve the good ones, download them, post by hand for the first ~20.

Only once that's working:

```bash
PUBLISHER=youtube          # or upload_post
AUTOPOST_ENABLED=true
AUTOPOST_PER_DAY=10
```

Add your accounts in **Settings → Accounts** first — the publisher uses them to
decide where a clip goes.

Check the wiring before you trust the queue:

```bash
railway run python scripts/check_infra.py       # Postgres, R2, ffmpeg, Redis
railway run python scripts/check_publisher.py   # credentials for your backend
```

---

## 5. The monthly bill

| Line | Cost |
|---|---|
| Railway (web + worker + scheduler + Postgres + Redis) | $10–20 |
| Anthropic (Sonnet for picking, Haiku for captions) | ~$20 |
| AssemblyAI (~90 hrs of audio) | ~$15 |
| YouTube Data API | free |
| Most-replayed heatmaps (yt-dlp) | free |
| Cloudflare R2 | ~$2 |
| Upload-Post (when you add the other platforms) | $16 |
| **Total** | **$47–73** |

That leaves $25–50 of headroom inside the $100 cap for a Snapchat-capable
reseller or an AI presenter — add whichever you can prove earns its money.

**What would blow the budget:** polling TikTok data every minute (~$32,000/mo),
an AI presenter on every clip ($60–360/mo), and running many accounts through a
per-profile reseller. The dashboard's spend tile tracks the estimate against
your $100 line so it can't creep up unnoticed.

---

## 6. Local development

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
cp .env.example .env                  # add ANTHROPIC_API_KEY + ASSEMBLYAI_API_KEY

# Just the clip pipeline, no server needed:
python scripts/run_pipeline.py "<youtube url>" --license own

# Or the whole app against a local Postgres:
createdb clipengine
DATABASE_URL=postgresql://localhost/clipengine alembic upgrade head
DATABASE_URL=postgresql://localhost/clipengine python scripts/seed_demo.py   # fake data
DATABASE_URL=postgresql://localhost/clipengine uvicorn api.main:app --reload
```

`scripts/seed_demo.py --clear` removes the demo rows when you're done with them.
