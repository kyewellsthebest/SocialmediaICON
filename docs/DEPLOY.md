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

**If your Facebook Page is linked to the wrong Instagram account**, don't
relink anything. Set `INSTAGRAM_ACCESS_TOKEN` instead: that switches Instagram
onto **Instagram Login**, which reaches the account directly and ignores Pages
entirely. Facebook Reels still post through the Page as normal. Get the token
from the app dashboard under *Instagram → API setup with Instagram business
login → Generate token*, picking the account you actually post from.

Run `python scripts/meta_token.py` afterwards — it prints the **username**
behind each configured id, so a wrong account shows up before the queue posts
to it.

**Instagram and Threads tokens from the app dashboard are already long-lived**
— 60 days, use them exactly as they come. Do not exchange them; that fails
with an error blaming the token, and sends you round in circles regenerating
a token that was fine.

Only the **Facebook** token needs exchanging, because the Graph API Explorer
issues a short-lived one:

```bash
python scripts/meta_token.py --exchange facebook=EAA...
```

If you ever do need to exchange an Instagram or Threads token — one obtained
through the OAuth code flow rather than the dashboard — it is signed with
**that platform's own** app secret (`INSTAGRAM_APP_SECRET`, `THREADS_APP_SECRET`,
from their own use case pages), never the Meta app's.

All three are refreshed the same way once they are in place, and the scheduler
does that for you.

The **Facebook Page** token is a two-step job, because a Page token inherits
the expiry of whatever it was derived from:

```bash
python scripts/meta_token.py --exchange facebook=EAA...   # user token -> 60 days
# put META_ACCESS_TOKEN in the environment, then:
python scripts/meta_token.py --page-token                 # -> a token with no timer
```

Derived from a long-lived user token, the Page token carries no expiry of its
own — the one credential here that never needs renewing.

**You only do the exchange once.** With `DATABASE_URL` set, the scheduler
refreshes every Meta token fortnightly and stores the new value, so nothing
lapses at 60 days — the environment variable is only the seed, and the way you
rotate a token by hand.

Then `python scripts/meta_token.py` shows what is configured, which account each
id resolves to, and when the tokens expire; `--refresh` extends them by another
60 days. Diary it, or posting stops two months after it last worked.

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


## When YouTube says "confirm you're not a bot"

Nothing is wrong with the request. YouTube challenges whole datacenter IP
ranges, and every cloud host — Railway included — sits in one. The same URL
downloads fine from your laptop.

Three fixes, cheapest first:

| Lever | Cost | Reliability |
|---|---|---|
| `YTDLP_PLAYER_CLIENTS` — reorder the list | free | sometimes, and it changes every few months |
| `YTDLP_COOKIES` — a logged-in session | free | usually |
| `YTDLP_PROXIES` — a block of residential proxies | ~$6/mo for 20 | often, if any in the block are unburned |

The pipeline already walks the client list on every download, so a challenge on
one client falls through to the next before anything fails.

**Cookies.** Export `cookies.txt` from a browser logged into YouTube (any
"Get cookies.txt" extension), open it in a text editor, and paste the whole
contents into `YTDLP_COOKIES`. No terminal needed — the file is tab-separated
and pasting usually turns those tabs into spaces, which is repaired on read.

`YTDLP_COOKIES_B64` takes the same file base64-encoded, for when a paste is
mangled beyond repair.

**Use a throwaway Google account.**
Signing in from a datacenter IP is precisely the pattern account bans look for,
and losing a burner account costs nothing.

**Or skip the download entirely.** The Sources tab takes a video file
directly: save it however you like and upload it. Everything downstream -
transcription, segment detection, ranking, rendering - is identical, and no
part of it depends on a platform's goodwill. Cheap proxies get flagged for
YouTube specifically, so this is often the shortest path to a finished clip.

**Use the whole block, not one IP.** Providers sell proxies in blocks and
those IPs are shared with their other customers, so they are not equally
burned — one being challenged says nothing about the next. Paste the provider's
whole exported list into `YTDLP_PROXIES` (the `ip:port:user:pass` lines work
as-is) and each download walks through them until one is served. Testing a
single IP and concluding the provider is useless is how a paid block gets
written off.

**The fix that removes the problem rather than working around it** is not
downloading from YouTube at all. Source footage from a clipping campaign comes
to you as a file, with permission attached — which is also the answer to the
copyright question.


## Downloading from a machine YouTube will talk to

If the bot check cannot be beaten from the host — and on a cloud host it often
cannot, at any price you would want to pay — move only the download to a
machine with an ordinary home connection. Everything else stays deployed.

1. On the deployment, set `INGEST_MODE=agent`. Sources then wait at
   `registered` instead of the worker trying a download that will fail.
2. On your own computer, once:

```bash
pip install yt-dlp httpx
```

3. Then leave this running:

```bash
python scripts/local_agent.py --url https://your-app.up.railway.app --token YOUR_DASHBOARD_TOKEN
```

It polls for sources waiting on a file, downloads each with yt-dlp over your
own connection, and posts it back. Transcription, detection, ranking, rendering
and publishing all continue in the cloud, unchanged.

Cost: nothing. Limitation: downloads only happen while it is running, so the
pipeline is as continuous as your computer is. Everything downstream stays 24/7
— a source fetched at midnight is still rendered and posted on schedule.

**What it does to your connection.** It uses your home internet exactly as your
browser does, and signs in to nothing — downloads are anonymous, so there is no
account to lose. It paces itself: 90 seconds between downloads and at most 12 an
hour by default, which is fewer videos than a person watching casually. Adjust
with `--min-gap` and `--max-per-hour`.

The realistic worst case is YouTube briefly rate-limiting the connection, which
would show as a captcha on Google searches for anyone in the building and clears
by itself within hours. Nothing is permanent, nothing is billed, and no
household device is affected beyond that. Bandwidth is the one real cost:
roughly 300 MB per source at 1080p, so `INGEST_MAX_HEIGHT=720` halves it if the
line is metered.

`--once` does a single pass and exits, which is what you want from a scheduled
task rather than a terminal left open.
