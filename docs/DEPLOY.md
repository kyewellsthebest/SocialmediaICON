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
| 5b | **Meta for Developers** — [developers.facebook.com](https://developers.facebook.com/apps/) | Instagram Reels, Threads and Facebook Reels from your own app | `META_APP_ID`, `META_APP_SECRET`, `META_ACCESS_TOKEN`, `INSTAGRAM_USER_ID`, `THREADS_USER_ID`, `THREADS_ACCESS_TOKEN`, `FACEBOOK_PAGE_ID` | free |
| 5c | **Upload-Post** — [upload-post.com](https://www.upload-post.com) | TikTok, Instagram, Facebook without the TikTok audit | `UPLOAD_POST_API_KEY`, `UPLOAD_POST_USER` | from ~$16/mo |
| 5d | **Zernio / Mallary** | Only route to Snapchat Spotlight | — | ~$50+/mo |

Start with 5a. It's free, it's the platform that also gives you trend data, and
it proves the loop before you pay a reseller. 5b is the other free one — three
more platforms for the cost of an afternoon in Meta's dashboard.

**Meta, in short.** Create an app at *developers.facebook.com* → use cases
**Manage messaging & content on Instagram** and **Access the Threads API**
(add **Manage everything on your Page** for Facebook Reels). Your Instagram
account must be **Business or Creator** and linked to a Facebook Page, or every
publish call fails on account type. In **Development mode** you can post to
accounts you hold an app role on — that is enough for your own accounts,
indefinitely, with no App Review.

Tokens last 60 days. `python scripts/meta_token.py` shows what is configured and
when it expires; `--refresh` extends it. Diary it, or posting stops two months
after it last worked.

Meta downloads the clip from a URL rather than accepting an upload, so **R2 must
be configured** before `PUBLISHER=meta` can work.

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
| `web` | `./scripts/start.sh web` | Migrations, then the API + dashboard |
| `worker` | `./scripts/start.sh worker` | Runs the pipeline jobs |
| `scheduler` | `./scripts/start.sh scheduler` | Fires scout, metrics and autopost on time |

On **web** only, also set **Settings → Deploy → Healthcheck Path** to `/health`, so a
web deploy that cannot be reached fails loudly instead of going live and serving
502s. Leave it empty on worker and scheduler — they have no HTTP server.

Set the start commands under **Settings → Deploy → Custom Start Command** on each service.
`web` also works with **no start command at all** — it is the image's default,
and leaving it empty is the safer choice.

> **Never put shell syntax in a Railway start command.** A command containing
> `${PORT:-8000}` is handed to the program literally, not expanded, and uvicorn
> dies with `Invalid value for '--port'` before it ever listens. Anything that
> needs a variable belongs inside `scripts/start.sh`, which does run in a shell.

Only `web` runs migrations. Three services racing on `alembic upgrade head` at
boot is how you end up with a half-applied schema.

All three build from the same **Dockerfile** (Railway picks it up automatically;
if a service was created before the Dockerfile existed, set
**Settings → Build → Builder = Dockerfile**). The image installs ffmpeg, which
ingest and render need at runtime.

Only `web` needs a public domain — `worker` and `scheduler` stay private.

> **Build failing with `pip: command not found`?** That's the Nixpacks builder,
> not the Dockerfile. Nixpacks' setup phase replaces its own Python provider
> package list when you add a system package like ffmpeg, which takes pip off
> PATH. Switch the service's builder to Dockerfile.

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

PUBLISHER=manual                            # switch to youtube / meta / upload_post later
AUTOPOST_ENABLED=false
```

`DATABASE_URL` and `REDIS_URL` come from the databases you added in step 3.
They are **not** injected automatically — you have to reference them on each
service, under **Variables**:

```
DATABASE_URL=${{Postgres.DATABASE_URL}}
REDIS_URL=${{Redis.REDIS_URL}}
```

All three services need both. Without them the container exits immediately and
tells you which one is missing.

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
PUBLISHER=youtube          # or meta / upload_post
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

## 5. When a deploy fails

| What the logs say | What it means |
|---|---|
| `pip: command not found` (exit 127) | The service is still on the Nixpacks builder. **Settings → Build → Builder → Dockerfile.** |
| `FATAL: DATABASE_URL is not set` | Add a Postgres database to the project, then set `DATABASE_URL=${{Postgres.DATABASE_URL}}` on this service. |
| `FATAL: REDIS_URL is not set` | Same for Redis: `REDIS_URL=${{Redis.REDIS_URL}}`. Only `worker` and `scheduler` need it to run, but set it everywhere. |
| `could not reach the database within 60s` | Postgres exists but this service cannot see it — check the variable reference points at the right database service. |
| A long `alembic` traceback on `worker` or `scheduler` | Those services are running the web start command. Give each one its own (table above). |
| `Invalid value for '--port': '${PORT:-8000}'` | Something is passing shell syntax as a start command. Railway reads the repo's **`Procfile` first, and it overrides the Dockerfile CMD** — check there before the UI. Changing the target port will not help; the process dies before it listens. |
| Deploy says **successful** but the URL shows **"Application failed to respond"** | Nothing is listening on the port Railway targets. Find the `binding 0.0.0.0:NNNN` line in the deploy log — if it is absent the app never started, so read further up. If it is present, point **Settings → Networking → Public Networking** at that port. |
| `Healthcheck failed` on **worker** or **scheduler** | Those are background processes with no HTTP server, so they can never answer a health probe. The healthcheck belongs on `web` only — set it per service in the UI, never in `railway.json`, which applies to all of them. |
| `Healthcheck failed` on **web** | The app never answered `/health` in time. Look further up the log — usually the database wait timing out. |

The container exits on a missing variable rather than crash-looping with a
stack trace, so the first line of the deploy log tells you what to fix.

## 6. The monthly bill

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

## 7. Local development

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
