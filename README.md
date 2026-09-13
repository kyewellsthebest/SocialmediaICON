# PutItUpp

Finds short gym video on Reddit, puts the mark on it, posts it with the
author's own caption.

```
20 rooms → top of the week → best 15 kept → badge → 5 a day, hourly from 7am
                                                    → square carousel, +30 min
```

Nothing is cut, transcribed, scored by a model or re-framed. Somebody already
decided the moment was worth posting, framed it, chose where it starts and
stops, and several thousand people agreed by upvoting it. The only change made
to the video is a small circular badge at the top centre.

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

**Two clocks, because filling the queue and emptying it are different jobs.**
The harvest runs once a day: read every room, re-rank, keep the best fifteen.
Posting runs on slots — one reel an hour, five a day, from 7am local, so the
last lands at 11am and the window shuts at noon.

One per slot rather than a batch: eight reels arriving at once is a burst
every platform notices, and it spends a day's queue in a minute. Missed slots
stay missed — a service that comes back at half past ten posts one reel, not
the three it owes, because catching up by dumping the backlog is the burst
this exists to avoid.

The timezone is named rather than assumed. Railway runs in UTC, so "7am" means
seven in the morning *somewhere*, and posting at 07:00 UTC to an audience in
Brisbane puts every reel out at five in the afternoon.

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

## The second post

Every reel goes up again half an hour later as a square Instagram carousel:
slide one a cover still with the mark on it and a reason to swipe, slide two
the video itself. A reel lands on the Reels surface; a carousel sits in the
grid and the feed, so the same clip reaches two different sets of eyes without
either looking like a repeat.

Nothing is cropped to make it square. A 9:16 gym video cut to a square loses
either the barbell or the lifter depending on where the crop falls, so the
whole frame is fitted and the gap filled with a blurred, darkened copy of
itself — pillarboxed, since a portrait video in a square is narrow and full
height.

The cover still is taken a second in rather than from frame zero, which is
very often black, a fade, or a hand reaching for the phone.

Instagram's carousel flow is not a variation on the reel flow: each slide gets
its own container marked `is_carousel_item`, a third container ties them
together, and that is what gets published. The video slide is `VIDEO` and
never `REELS` — a reel cannot be a carousel item, and asking for one is
refused in a way that reads like a bad URL.

**R2 is required for this**, unlike for a reel: Instagram fetches each slide
from a URL rather than accepting an upload.

## Which rooms

The room list is the biggest lever on what this posts, and it is the one thing
nobody can get right from first principles: a room of personal progress clips
gives almost nothing usable, one of PR attempts and dropped bars carries the
whole queue, and the difference is invisible until a week of evidence exists.

So the default is chosen for **PR attempts, heavy singles, skills and fails** —
the things people post *as video* — and progress, motivation and physique rooms
are deliberately absent, being mostly stills and text.

Then the Setup tab shows **what each room actually gave back on the last run**:
read, postable, new, and the error if it could not be read at all. A name that
does not exist reports against itself rather than breaking the run. Edit the
list there — it takes effect on the next run, with no redeploy — and clearing
it hands control back to `REDDIT_SUBREDDITS`.

## The two rules that never bend

Both live in `core/reddit.py: postable()`, the one function every path goes
through, because there is no editor downstream to catch either:

- **Nothing adult.** `include_over_18=false` on a listing is a preference, not
  a guarantee, and it does not cover a crosspost out of a quarantined room.
- **Nothing over 60 seconds**, which is where Reels, Shorts and TikTok all stop
  treating a video as short-form. And nothing under 4, which is not a post.

## Where reels go

There is no list of accounts to fill in. The destinations are derived from
whichever credentials are set — `INSTAGRAM_USER_ID` *is* the Instagram
account — because two records of one fact is one too many, and the way the
second one goes wrong is by naming an account the credentials cannot reach.
That reads as "the post failed" rather than "those are two different
accounts".

The Setup tab asks Meta to name each account rather than reporting that a
variable is non-empty, since posting to the wrong Instagram is the failure
nothing else catches until after it has happened.

It also names any platform that was **started but cannot be reached**, and
which variable it is waiting on. Instagram, Threads and Facebook are three
separate products behind one brand and they do not share credentials —
Threads has its own API on `graph.threads.net` and its own login, and
`META_ACCESS_TOKEN` does not work for it. Without that readout a page posts
happily to two of the three and nothing anywhere says the third was ever
meant to be included.

Two switches gate everything: `PUBLISHER` picks the backend, and
`AUTOPOST_ENABLED` has to be on. An accidental deploy that starts posting is
not a mistake you can take back.

## Attribution

Every reel keeps its permalink, subreddit and author for as long as the row
exists, so a takedown request months later can be answered with *which post was
this?* The caption is the author's own words, verbatim — a repost page that
rewrites them is doing something meaningfully worse than one that copies them.

Put the disclaimer and a contact address in the account bio.

## Running it

**One Railway service.** There used to be three — a web one, a worker taking
jobs off a Redis queue, and a scheduler putting them on it — and two of them
existed to carry a clipping pipeline that no longer does. What they left behind
was a failure with no symptom: the dashboard accepted a run, Redis accepted the
job, and nothing picked it up, because the worker was not deployed. "Queued"
and "queued and abandoned" look identical from a browser.

This is one job, once a day, for about ten minutes. It runs on a thread inside
the web service, which cannot fail to be running while its own dashboard
answers. Every run is written to `run_log` — finished or failed, with the
traceback, because a thread has nobody waiting on it and no response to fail.

```
web   ./scripts/start.sh web   # migrations, the API, the dashboard, the daily run
```

If you still have `worker` or `scheduler` services in Railway, delete them.
There is no Redis any more either. Every variable the app reads is in
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
| **Setup** | Where reels go (and what those credentials resolve to), the route test, the download-and-check-the-sound test, and the live config |

## Layout

```
core/
  config.py       every variable, and nothing else is read
  reddit.py       what a video is, and whether it may be posted
  reddit_routes.py  six ways in, tried until one answers
  brand.py        the badge, and nothing else touched
  carousel.py     the two square slides: cover, then video
  models.py       four tables
  publishers/     manual | youtube | meta | upload_post
  storage.py      R2, with a local-directory fallback
  jobs.py         the daily run, on a thread, one at a time, always recorded
worker/
  tasks/harvest.py  discover, rank, evict, download, brand
  tasks/publish.py  send the top of the queue out
api/
  routes/app.py     everything the dashboard reads
  static/           the dashboard, no build step
```

## Tests

```bash
pytest         # 200; the branding tests skip without ffmpeg
ruff check .
```

The branding tests build a real video with ffmpeg, brand it, and compare frames
before and after — the corner must change and the middle must not. That is the
one that catches a "small overlay" quietly becoming a re-frame.
