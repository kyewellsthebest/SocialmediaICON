"""Who is this streamer, and is this a stream we want at all?

The bot was watching Battlegrounds Mobile India tournaments in Hindi. Not
because anything was broken exactly - because it was choosing from a list
sorted by viewers, and the two filters it had were both fiction. The
directory's language tag is optional, so a row that simply does not say gets
kept; and there was no category filter at all.

The first fix over-corrected. "No gaming" threw out xQc playing anything,
LosPollosTV in co-op, and a GTA channel - all of them people being funny with
a game on screen, which is most of what a clipping bot should want. What is
actually unwanted is the *event*: a tournament, a league fixture, a qualifier,
an organisation's roster competing to a scoreboard. Nobody clips a bracket.

So the question is not "is a game on screen" but "did the audience come for a
person or for a result". Guessing that from a listing row is hopeless - the
category is the name of the game either way, and "Counter-Strike 2" is the
same string for a man messing about with friends and for the grand final of a
major. So this builds a small dossier per channel and keeps it: what they
actually do, what they are known for, what language they broadcast in, and
whether this is a person or a fixture.

Three layers, cheapest first, and most channels never reach the third:

* **The listing row itself.** A title or category naming a competitive event
  is a rejection and costs nothing. So is a title in a script the audience
  cannot read.
* **Kick's own channel record** - bio, follower count, the categories they
  usually stream. One HTTP call, cached.
* **A model with web search**, once per channel, for the question nothing
  structured can answer: who is this person, what is this stream, and would an
  English-speaking audience follow it.

The dossier is cached hard. A streamer does not change who they are between
polls, and the whole point of paying for the third layer once is not paying
for it every five minutes.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from core.config import settings

log = logging.getLogger(__name__)

#: The rule this dossier was decided under. Bumping it retires every cached
#: answer at once, which is the only thing that makes a rule change take
#: effect today rather than next week: LosPollosTV, kaneljoseph and a GTA
#: channel were all cached as ineligible for seven days under "no gaming",
#: and without this they would stay refused long after the rule that refused
#: them was gone.
#:
#: v2: gaming is fine; competitive events are not.
#: v3: the romanised-Hindi list no longer contains English words. An ordinary
#:     English chat - bro, gg, op, brother, sun - scored 92 markers against a
#:     threshold of 22 and was refused, so every channel turned away by that
#:     has a wrong answer cached and has to be asked again.
KEY = "clipengine:profile:v3:{channel}"
#: A week. Long enough that the bill is a rounding error, short enough that a
#: streamer who changes what they do is noticed within one.
TTL_S = 7 * 24 * 3600

#: What is actually unwanted is a competitive event, not a video game. A
#: streamer playing something while talking to chat is a person being
#: entertaining and is exactly the material this exists for; a tournament
#: broadcast is a sports fixture, and the draw is the result rather than
#: anybody's reaction to it.
#:
#: These are the phrases that say "event" on their own, and the list is
#: deliberately short. A false positive here throws away a good streamer for a
#: week; a false negative costs one model call, because the third layer asks
#: the same question properly. So nothing ambiguous belongs here - not
#: "finals" (a music stream can say that), not "major", not "1v1", not "vs".
EVENT_PHRASES = (
    "esports", "e-sports", "grand final", "group stage", "qualifier",
    "playoffs", "tournament", "tourney", "championship", "invitational",
    "scrims", "watch party", "watchparty",
)
#: League and circuit names, matched as whole words so "esl" cannot be found
#: inside another one. A channel broadcasting one of these is broadcasting a
#: fixture whoever is holding the microphone.
EVENT_ACRONYMS = re.compile(
    r"\b(vct|lcs|lec|lck|lpl|owcs|cdl|rlcs|pgl|iem|esl|blast|dreamhack|"
    r"bgis|bmps|bgms|msi|ewc|ti\d*)\b",
    re.IGNORECASE,
)

#: Scripts an English-speaking audience cannot read. A title or a chat full of
#: these is a decided question, and it is decided without spending anything.
FOREIGN_SCRIPT = re.compile(
    "["
    "ऀ-ॿ"   # Devanagari - Hindi, Marathi, Nepali
    "؀-ۿ"   # Arabic
    "Ѐ-ӿ"   # Cyrillic
    "฀-๿"   # Thai
    "぀-ヿ"   # Japanese kana
    "一-鿿"   # CJK
    "가-힯"   # Hangul
    "֐-׿"   # Hebrew
    "ঀ-৿"   # Bengali
    "஀-௿"   # Tamil
    "ఀ-౿"   # Telugu
    "]"
)

#: Romanised Hindi and Urdu do not trip the script test - "bhai kya kar raha
#: hai" is all Latin letters. These are the words that carry the register.
#:
#: Every one of them has to be a word an English chat does not use, and that
#: is a harder list than it looks. The first version contained "gg", "op",
#: "sun", "mast" and "brother", and an ordinary English Kick chat - bro, gg,
#: op, brother, sun - scored 92 markers against a threshold of 22 and was
#: refused as Hindi. An English-speaking streamer was being turned away by the
#: English filter.
#:
#: Gone with them: "bol", "sahi", "mast", "sun". "hai" and "kar" stayed - they
#: are not English words, and "bhai kya kar raha hai" loses most of itself
#: without them - which is safe because no single marker decides anything any
#: more. That is what the rule below is for.
ROMANISED = re.compile(
    r"\b(bhai|bhaiya|kya|kyu|kyun|hai|hain|nahi|nahin|haan|acha|accha|yaar|"
    r"mera|tera|aap|tum|kar|karo|karna|raha|rahe|rhe|matlab|paisa|"
    r"bahut|bohot|thoda|zyada|abhi|chalo|dekho|bolo|jaldi|"
    r"kaise|kahan|kaun|kitna|galat|jhakaas|bakchodi|bhosdi|chutiya|"
    r"kitne|sabse|dost|pagal|majaa|maza|dekh|suno|arre|arey)\b",
    re.IGNORECASE,
)
#: How many *different* markers have to appear before a chat counts as Hindi.
#:
#: Different, not many. One word repeated is one person, or one borrowing, or
#: one emote - "bhai" forty times is a single fact stated forty times. Language
#: shows up as vocabulary: several unrelated words from the same register in
#: the same chat is a thing that does not happen by accident. Four, because
#: "bhai kya raha yaar" is four and is unmistakably Hindi - and because with
#: no English word left on the list, this is a second lock rather than the
#: only one.
ROMANISED_KINDS = 4


class ProfileError(RuntimeError):
    pass


@dataclass
class Profile:
    """What is known about a channel, and whether it is worth watching."""

    channel: str
    #: The decision. False means never attach, and say why.
    eligible: bool = True
    reason: str = ""
    #: What the stream is, in a sentence, for the verdict prompt to read.
    about: str = ""
    known_for: str = ""
    language: str = ""
    #: A broadcast of a competitive fixture rather than a person playing.
    is_esports: bool = False
    #: How the decision was reached, so a wrong one can be argued with.
    decided_by: str = ""
    confidence: float = 0.0
    at: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "eligible": self.eligible,
            "reason": self.reason,
            "about": self.about,
            "known_for": self.known_for,
            "language": self.language,
            "is_esports": self.is_esports,
            "decided_by": self.decided_by,
            "confidence": round(self.confidence, 2),
            "at": self.at,
        }

    def summary(self) -> str:
        """A paragraph the verdict prompt can use as context."""
        parts = [p for p in (self.about, self.known_for) if p]
        return " ".join(parts)


def foreign_share(text: str) -> float:
    """How much of a piece of text is in a script the audience cannot read."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if FOREIGN_SCRIPT.match(c)) / len(letters)


def romanised_hits(lines: list[str]) -> int:
    """Hindi and Urdu written in Latin letters, which the script test misses."""
    return sum(len(set(ROMANISED.findall(line))) for line in lines)


def romanised_kinds(lines: list[str]) -> set[str]:
    """Which distinct markers appeared, which is the part that means anything."""
    return {m.lower() for line in lines for m in ROMANISED.findall(line)}


def event_marker(*texts: str) -> str:
    """The competitive-event phrase these texts carry, or empty.

    Reads the title as well as the category, because Kick's category is the
    name of the game either way - "Counter-Strike 2" is the same string
    whether it is a man playing with his friends or the grand final of a
    major. The title is where the difference shows.
    """
    lowered = " ".join(t for t in texts if t).lower()
    for phrase in EVENT_PHRASES:
        if phrase in lowered:
            return phrase
    found = EVENT_ACRONYMS.search(lowered)
    return found.group(0).lower() if found else ""


# --- the three layers -------------------------------------------------------


def from_listing(channel: str, *, category: str = "", title: str = "",
                 language: str = "") -> Profile | None:
    """A decision from the listing row alone, or None if it cannot be made.

    Only ever returns a rejection. Nothing in a directory row is enough to say
    yes - the two streams this exists for were both listed with an English
    title and no language at all.
    """
    marker = event_marker(category, title)
    if marker:
        return Profile(
            channel=channel, eligible=False, is_esports=True, confidence=1.0,
            reason=f"a competitive event, not a person - the listing says {marker!r}",
            decided_by="listing", language=language,
        )

    share = foreign_share(title)
    if share > 0.25:
        return Profile(
            channel=channel, eligible=False, confidence=1.0,
            reason=f"the title is {round(share * 100)}% non-Latin script",
            decided_by="title", language=language,
        )

    spoken = (language or "").lower()
    if spoken and not (spoken.startswith("en") or spoken == "english"):
        return Profile(
            channel=channel, eligible=False, confidence=0.9,
            reason=f"the directory says this stream is in {language}",
            decided_by="listing", language=language,
        )
    return None


def from_chat(channel: str, lines: list[str]) -> Profile | None:
    """A decision from what chat is typing. Also only ever a rejection.

    Chat is the most honest language signal a stream has: the directory tag is
    optional and the title is marketing, but an audience types in the language
    it thinks in. Non-Latin script settles it outright; romanised Hindi needs a
    handful of hits, because one "bhai" proves nothing on any chat on Kick.
    """
    if len(lines) < 25:
        return None

    joined = " ".join(lines)
    share = foreign_share(joined)
    if share > 0.12:
        return Profile(
            channel=channel, eligible=False, confidence=1.0,
            reason=f"chat is {round(share * 100)}% non-Latin script",
            decided_by="chat script",
        )

    kinds = romanised_kinds(lines)
    hits = romanised_hits(lines)
    # Both: several different words from the register, *and* enough of them to
    # be the chat rather than one person in it.
    if len(kinds) >= ROMANISED_KINDS and hits >= max(12, len(lines) // 6):
        said = ", ".join(sorted(kinds)[:6])
        return Profile(
            channel=channel, eligible=False, confidence=0.8,
            reason=(
                f"chat is largely romanised Hindi - {len(kinds)} different "
                f"markers ({said}), {hits} times in {len(lines)} lines"
            ),
            decided_by="chat words",
        )
    return None


def from_kick(channel: str) -> dict[str, Any]:
    """Kick's own record of the channel. One call, and it answers a lot."""
    from curl_cffi import requests as cffi

    response = cffi.get(
        f"https://kick.com/api/v2/channels/{channel}", impersonate="chrome", timeout=20.0
    )
    if response.status_code >= 400:
        raise ProfileError(f"HTTP {response.status_code} looking up {channel}")
    payload = response.json()
    stream = payload.get("livestream") or {}
    categories = stream.get("categories") or payload.get("recent_categories") or []
    return {
        "bio": ((payload.get("user") or {}).get("bio") or "")[:600],
        "followers": payload.get("followers_count") or 0,
        "verified": bool(payload.get("verified")),
        "title": stream.get("session_title") or "",
        "language": stream.get("language") or "",
        "categories": [
            str(c.get("name", "")) for c in categories if isinstance(c, dict)
        ][:5],
    }


SYSTEM = """You decide whether a live streamer belongs on a clipping bot's watch
list, and you write the one paragraph of context everything downstream reads.

The bot cuts short vertical clips for an English-speaking audience. It wants
people: reactions, arguments, jokes, things going wrong, real life happening on
camera.

A video game on screen is not a problem. A streamer playing something while
talking to chat is a person being entertaining, and that is exactly the
material this exists for - rage, a scare, a friend betraying them, a win they
did not expect. Casual play, co-op with friends, story games, variety gaming,
speedruns, ranked grinding on their own channel: all wanted, as long as the
person is the show.

Two decisions and one description.

Is this stream in English? Not "does the streamer speak some English" - is the
broadcast in English. A Hindi-language broadcast with an English title is not.
If you are unsure, say so with a low confidence rather than guessing; being
wrong here wastes days of a watch slot.

Is this a competitive event rather than a person? That is the one thing that
is out. An event is a fixture: a tournament, a league match, a qualifier, a
group stage, a scrim block, a showmatch, an official circuit broadcast, or a
team organisation's channel showing its roster compete. The draw is the result
and the commentary, and there is usually no single face the clip could be
about. Casters and observer channels are events. So is a watch party of one.

The test is what the audience came for. They came for a person: not an event,
however competitive the game is. They came for who wins a bracket: an event,
however charming the caster is. Someone grinding ranked alone and talking to
chat is a person. The same game with a team, a coach and a scoreboard overlay
is an event.

Then describe them: who they are, what they actually do on stream, what they
are known for, and anything notable that has happened around them. Two or
three sentences, factual, no marketing. This is read by a model deciding
whether a clip of them is worth posting, so it should say what a regular viewer
would already know.

Search the web if the name is one you do not recognise. If you cannot find out
who they are, say so - an unknown streamer is not a rejection, it is an unknown,
and the confidence should show it."""


SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["is_english", "is_esports", "confidence", "about", "reason"],
    "properties": {
        "is_english": {"type": "boolean"},
        "is_esports": {
            "type": "boolean",
            "description": (
                "Is this a broadcast of a competitive event - a tournament, "
                "league match, qualifier, scrim block or organisation's "
                "roster competing - rather than a person playing? A streamer "
                "playing a game while talking to chat is NOT this."
            ),
        },
        # No minimum/maximum here. The structured-output endpoint rejects
        # both on a number outright - "For 'number' type, properties
        # maximum, minimum are not supported" - with a 400, which fails the
        # whole call rather than the one field. The range goes in the
        # description, and _clamp below enforces it on the way in.
        "confidence": {
            "type": "number",
            "description": "How sure you are, from 0.0 to 1.0.",
        },
        "about": {
            "type": "string",
            "description": "Two or three factual sentences: who they are and what they do.",
        },
        "known_for": {"type": "string"},
        "language": {"type": "string", "description": "The language broadcast in."},
        "reason": {"type": "string", "description": "Why this decision, in one sentence."},
    },
}


def research(channel: str, *, facts: dict[str, Any] | None = None) -> Profile:
    """Ask a model who this is. Once per channel, then cached for a week."""
    import anthropic

    from core import llm

    facts = facts or {}
    told = "\n".join(
        f"- {k}: {v}" for k, v in facts.items() if v not in (None, "", [], 0)
    ) or "- (nothing beyond the name)"

    try:
        client = llm.get_client()
        response = client.messages.create(
            model=settings.profile_model,
            max_tokens=3000,
            system=SYSTEM,
            messages=[{
                "role": "user",
                "content": (
                    f"Kick streamer: https://kick.com/{channel}\n\n"
                    f"What Kick says about them:\n{told}\n\n"
                    "Should the bot watch this channel, and who are they?"
                ),
            }],
            tools=[{
                "type": "web_search_20260209",
                "name": "web_search",
                "max_uses": 4,
            }],
            thinking={"type": "adaptive"},
            output_config={
                "format": {"type": "json_schema", "schema": SCHEMA},
                "effort": settings.profile_effort,
            },
        )
        payload = llm.extract_json(
            "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
        )
    except anthropic.APIStatusError as exc:
        raise ProfileError(f"the model would not answer: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - the caller decides what unknown means
        raise ProfileError(f"{type(exc).__name__}: {exc}") from exc

    english = bool(payload.get("is_english"))
    event = bool(payload.get("is_esports"))
    # Clamped here rather than in the schema, which cannot express a range.
    confidence = min(1.0, max(0.0, float(payload.get("confidence") or 0.0)))
    reason = str(payload.get("reason") or "")

    if event:
        eligible, why = False, f"a competitive event, not a person - {reason}"
    elif not english:
        eligible, why = False, f"not in English - {reason}"
    elif confidence < settings.profile_min_confidence:
        eligible, why = False, f"too unsure to spend a slot on - {reason}"
    else:
        eligible, why = True, reason

    return Profile(
        channel=channel,
        eligible=eligible,
        reason=why,
        about=str(payload.get("about") or ""),
        known_for=str(payload.get("known_for") or ""),
        language=str(payload.get("language") or ""),
        is_esports=event,
        decided_by="research",
        confidence=confidence,
    )


# --- the cache and the decision --------------------------------------------


def _redis():  # noqa: ANN202
    from core import livestate

    return livestate._redis()


def remember(profile: Profile) -> None:
    import json
    import time

    profile.at = profile.at or time.time()
    client = _redis()
    payload = json.dumps(profile.as_dict())
    if client is None:
        _local[profile.channel] = payload
        return
    try:
        client.set(KEY.format(channel=profile.channel), payload, ex=TTL_S)
    except Exception as exc:  # noqa: BLE001 - a lost cache is a repeated question
        log.debug("profile: could not cache %s (%s)", profile.channel, exc)
        _local[profile.channel] = payload


def recall(channel: str) -> Profile | None:
    import json

    client = _redis()
    raw = _local.get(channel)
    if client is not None:
        try:
            raw = client.get(KEY.format(channel=channel)) or raw
        except Exception:  # noqa: BLE001
            pass
    if not raw:
        return None
    try:
        found = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return Profile(**{k: v for k, v in found.items() if k in Profile.__dataclass_fields__})


def forget(channel: str) -> None:
    _local.pop(channel, None)
    client = _redis()
    if client is None:
        return
    try:
        client.delete(KEY.format(channel=channel))
    except Exception:  # noqa: BLE001
        pass


#: Used when Redis is absent, so a laptop run still asks each question once.
_local: dict[str, str] = {}


def _unreachable(
    channel: str, exc: Exception, *, facts: dict[str, Any], language: str
) -> Profile:
    """What to do when the research call itself never happened.

    Unknown is not approval, and a bad key must not send the bot back to
    watching whatever is biggest. But refusing everyone is how a night of
    clipping becomes a night of nothing: one 400 on a schema, and every
    channel on Kick reads "could not find out who this is". That is not
    safety, it is an outage with a polite message.

    So the question is not "did the model answer" but "is anything still
    saying no". Every cheap rejection has already run and passed by the time
    we get here - the listing category, the title script, the chat script,
    and Kick's own category history. When the directory *positively* says
    English and Kick's record shows what they stream and none of it is a
    game, that is enough to keep watching provisionally. It is never cached
    and never confident, so the next poll asks again and the real answer
    replaces it the moment the model is reachable.

    A stream that merely fails to say what language it is does not qualify.
    Silence is what the two Hindi gaming channels looked like.
    """
    spoken = (language or "").lower()
    says_english = spoken.startswith("en") or spoken == "english"
    known_categories = [c for c in (facts.get("categories") or []) if c]

    if says_english and known_categories:
        return Profile(
            channel=channel,
            eligible=True,
            confidence=0.3,
            language=language,
            about=str(facts.get("bio") or "")[:300],
            reason=(
                f"could not reach the research ({exc}); the directory says English "
                f"and none of {', '.join(known_categories[:3])} is a game, so "
                "watching provisionally until it answers"
            ),
            decided_by="unreachable fallback",
        )

    return Profile(
        channel=channel,
        eligible=not settings.profile_required,
        confidence=0.0,
        language=language,
        reason=f"could not find out who this is ({exc})",
        decided_by="unreachable",
    )


def decide(
    channel: str,
    *,
    category: str = "",
    title: str = "",
    language: str = "",
    chat: list[str] | None = None,
    refresh: bool = False,
) -> Profile:
    """Should the bot watch this channel? Cheapest question first.

    A cached answer is used as-is - a streamer does not become a different
    person between polls, and the point of paying for the research once is not
    paying for it every five minutes. A cheap rejection is cached too: there is
    no sense re-deciding that Battlegrounds Mobile India is a game.
    """
    if not refresh:
        known = recall(channel)
        if known is not None:
            return known

    quick = from_listing(channel, category=category, title=title, language=language)
    if quick is None and chat:
        quick = from_chat(channel, chat)
    if quick is not None:
        remember(quick)
        return quick

    if not settings.profile_enabled:
        return Profile(channel=channel, eligible=True, reason="research is switched off",
                       decided_by="default", confidence=0.0)

    facts: dict[str, Any] = {"category": category, "title": title, "language": language}
    try:
        facts |= from_kick(channel)
    except Exception as exc:  # noqa: BLE001 - the model can work without it
        log.info("profile: no Kick record for %s (%s)", channel, exc)

    # An event named on the channel record rather than in the listing row - a
    # stream that has just switched category, or one the directory mislabelled.
    # Only an event: a game here is not a rejection, because the categories a
    # channel usually streams say nothing about whether the person is worth
    # watching. xQc's are half games and he is the point of his own stream.
    for name in facts.get("categories") or []:
        marker = event_marker(name)
        if marker:
            found = Profile(
                channel=channel, eligible=False, is_esports=True, confidence=0.9,
                reason=f"they broadcast events ({name})", decided_by="kick categories",
            )
            remember(found)
            return found

    try:
        found = research(channel, facts=facts)
    except ProfileError as exc:
        found = _unreachable(channel, exc, facts=facts, language=language)
        if not found.eligible:
            log.warning("profile: refusing %s - %s", channel, found.reason)
        return found  # deliberately not cached: ask again next time

    remember(found)
    log.info(
        "profile: %s - %s (%s)",
        channel, "watch" if found.eligible else "skip", found.reason,
    )
    return found
