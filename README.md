# PutItUpp

Finds short gym video on Reddit, puts the mark on it, posts it with the
author's own caption.

```
25 rooms → top of the week → best 15 kept → badge → 8 posted a day
```

Nothing is cut, transcribed, scored by a model or re-framed. Somebody already
decided the moment was worth posting, framed it, chose where it starts and
stops, and several thousand people agreed by upvoting it. The only change made
to the video is a small circular badge in the top-left corner.

---

## How it works

**The queue is a leaderboard, not a pipeline.** Every run reads every room, and
anything better than the weakest thing waiting takes its place — so a video
found on Tuesday can still be beaten on Thursday and never go out. The fifteen
slots always hold the fifteen best videos anyone has seen, rather than the
fifteen oldest.

Nothing is deleted from the table. A posted reel stays a posted reel and a
beaten one stays a beaten one, because both answer the same question — *have we
already dealt with this?* — and a queue that forgets reposts itself the first
time a video comes round again.

**One daily run** does the whole thing: read the rooms, re-rank, send the top
eight out. Find fifteen, post eight, seven carry over and compete against
tomorrow's.

A reel is downloaded when it is about to be posted, not when it joins the
queue. Most of what enters is beaten before its turn, and downloading fifteen a
day to post eight pays twice to throw half away.

## Getting to Reddit

Reddit refuses unauthenticated reads from datacenter ranges, which is every
cloud host. There are six ways in and the code tries them in order until one
answers:

| route | what it is | fails when |
| --- | --- | --- |
| `oauth` | a free script app over `oauth.reddit.com` | no app configured |
| `json` | `reddit.com/….json`, no app at all | the address is a datacenter (403) |
| `proxied` | the same JSON from `YTDLP_PROXIES` | no proxy configured |
| `reader` | a public reader service fetches it | the service is down |
| `mirror` | a Redlib front-end, different domain | every instance is down |
| `rss` | Reddit's own Atom feed | refused too |

They fail for different reasons, which is the point — one failing says nothing
about the next. Measured on Railway, `json` is refused and **`rss` works**.

`mirror` and `rss` return a title and a link and nothing else. Duration and the
adult flag are looked up with yt-dlp — the same tool that has to open the post
to download it anyway, so a post it cannot read was never postable. Vote counts
come back through the same lookup, which is what the queue ranks on.

Press **Test every route** in the dashboard's Setup tab to measure this from
wherever the app is actually running. A route that answers from your laptop
says nothing about the server, and that gap is the whole problem.

## The two rules that never bend

Both live in `core/reddit.py: postable()`, the one function every path goes
through, because there is no editor downstream to catch either:

- **Nothing adult.** `include_over_18=false` on a listing is a preference, not
  a guarantee, and it does not cover a crosspost out of a quarantined room.
- **Nothing over 60 seconds**, which is where Reels, Shorts and TikTok all stop
  treating a video as short-form. And nothing under 4, which is not a post.

## Attribution

Every reel keeps its permalink, subreddit and author for as long as the row
exists, so a takedown request months later can be answered with *which post was
this?* The caption is the author's own words, verbatim — a repost page that
rewrites them is doing something meaningfully worse than one that copies them.

Put the disclaimer and a contact address in the account bio.

## Running it

Three Railway services from one Dockerfile:

```
web        ./scripts/start.sh web         # migrations, then the API + dashboard
worker     ./scripts/start.sh worker      # downloads, branding, posting
scheduler  ./scripts/start.sh scheduler   # fires the daily run
```

Only `web` migrates. Every variable the app reads is in
[`.env.example`](.env.example); anything not on that list is read by nothing.

```bash
alembic upgrade head
curl -s https://<your-app>/health
```

## The dashboard

Gated behind `DASHBOARD_TOKEN` — set it, or anyone with the URL can drive it.

| Tab | What's on it |
| --- | --- |
| **Queue** | The fifteen, ranked. What a new video has to beat to get in. Prepare and watch one before it goes out, or drop it |
| **Posted** | What went out, and where. Plus what was beaten, and by how much |
| **Setup** | Accounts, the route test, the download-and-check-the-sound test, and the live config |

## Layout

```
core/
  config.py       every variable, and nothing else is read
  reddit.py       what a video is, and whether it may be posted
  reddit_routes.py  six ways in, tried until one answers
  brand.py        the badge, and nothing else touched
  models.py       four tables
  publishers/     manual | youtube | meta | upload_post
  storage.py      R2, with a local-directory fallback
worker/
  tasks/harvest.py  discover, rank, evict, download, brand
  tasks/publish.py  send the top of the queue out
  scheduler.py      the daily heartbeat
api/
  routes/app.py     everything the dashboard reads
  static/           the dashboard, no build step
```

## Tests

```bash
pytest         # 149; the branding tests skip without ffmpeg
ruff check .
```

The branding tests build a real video with ffmpeg, brand it, and compare frames
before and after — the corner must change and the middle must not. That is the
one that catches a "small overlay" quietly becoming a re-frame.
