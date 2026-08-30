"""Kick chat, live, over the websocket the site itself uses.

core.chat can already read a VOD's chat replay. That is the wrong shape for
this: replay is a request per minute of a stream that already finished, and
the whole point here is to know what chat is doing *now*, seconds after it
happens, while the video is still in the buffer.

Kick's web player subscribes to a Pusher channel per chatroom and receives
each message as it is sent. This does the same thing - no key, no account,
because it is the same public feed a logged-out viewer gets.

Two things it is careful about.

**It never blocks the supervisor.** The socket runs on its own thread and
pushes into a LiveLog; a channel whose chat is broken produces a bot with no
chat signal for that channel, not a stalled bot.

**It forgets on the same timer as the video.** LiveLog expires messages
older than the buffer window, so the chat held always describes video that
still exists to be cut.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from core.chat import LiveLog, Message, clean

log = logging.getLogger(__name__)

#: Kick's public Pusher app key, the one its own web client uses. It is not a
#: secret - it is served in the page to every anonymous visitor.
PUSHER_KEY = "32cbd69e4b950bf97679"
PUSHER_CLUSTER = "us2"
SOCKET_URL = (
    f"wss://ws-{PUSHER_CLUSTER}.pusher.com/app/{PUSHER_KEY}"
    "?protocol=7&client=js&version=8.4.0&flash=false"
)
#: The event carrying a chat line. Kick namespaces it with a PHP class name.
MESSAGE_EVENT = "App\\Events\\ChatMessageEvent"
RECONNECT_S = 5.0


class LiveChatError(RuntimeError):
    pass


def chatroom_id(channel: str) -> int:
    """The chatroom behind a channel slug. Needs a browser fingerprint."""
    from curl_cffi import requests as cffi

    response = cffi.get(
        f"https://kick.com/api/v2/channels/{channel}", impersonate="chrome", timeout=30.0
    )
    if response.status_code >= 400:
        raise LiveChatError(f"HTTP {response.status_code} looking up {channel}")
    payload = response.json()
    room = (payload.get("chatroom") or {}).get("id")
    if not room:
        raise LiveChatError(f"{channel} has no chatroom id in its channel payload")
    return int(room)


@dataclass
class LiveChat:
    """A background reader for one channel's chat."""

    channel: str
    log: LiveLog
    #: Wall-clock the stream buffer started, so message offsets line up with
    #: the video rather than with the epoch.
    origin: float = field(default_factory=time.time)
    chatroom: int | None = None
    connected: bool = False
    messages_seen: int = 0
    last_error: str = ""
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _thread: threading.Thread | None = field(default=None, repr=False)

    def start(self) -> LiveChat:
        if self._thread is not None:
            raise LiveChatError(f"chat for {self.channel} is already running")
        self._thread = threading.Thread(
            target=self._run, name=f"chat-{self.channel}", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        self.connected = False

    def status(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "connected": self.connected,
            "chatroom": self.chatroom,
            "messages_seen": self.messages_seen,
            "held": self.log.status(),
            "error": self.last_error,
        }

    # --- the socket ---------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self._session()
            except Exception as exc:  # noqa: BLE001 - a broken socket is not fatal
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.warning("livechat %s: %s", self.channel, self.last_error)
            self.connected = False
            # A channel that keeps failing must not become a reconnect storm.
            self._stop.wait(RECONNECT_S)

    def _session(self) -> None:
        from websockets.sync.client import connect

        if self.chatroom is None:
            self.chatroom = chatroom_id(self.channel)

        with connect(SOCKET_URL, open_timeout=20, close_timeout=5) as socket:
            socket.send(
                json.dumps(
                    {
                        "event": "pusher:subscribe",
                        "data": {"auth": "", "channel": f"chatrooms.{self.chatroom}.v2"},
                    }
                )
            )
            self.connected = True
            self.last_error = ""
            log.info("livechat %s: subscribed to chatroom %s", self.channel, self.chatroom)

            while not self._stop.is_set():
                try:
                    raw = socket.recv(timeout=30)
                except TimeoutError:
                    # Pusher sends its own pings; a quiet chat is normal and
                    # is not a reason to tear the connection down.
                    continue
                self._handle(raw)

    def _handle(self, raw: str | bytes) -> None:
        try:
            envelope = json.loads(raw)
        except (TypeError, ValueError):
            return
        if envelope.get("event") != MESSAGE_EVENT:
            return

        # Pusher double-encodes: the payload is a JSON string inside the frame.
        data = envelope.get("data")
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except ValueError:
                return
        if not isinstance(data, dict):
            return

        message = self._to_message(data)
        if message is not None:
            self.log.add(message)
            self.messages_seen += 1

    def _to_message(self, data: dict[str, Any]) -> Message | None:
        text = data.get("content")
        if not text:
            return None
        sender = data.get("sender") or {}
        stamp = data.get("created_at")

        # Prefer the server's own timestamp; fall back to arrival time, which
        # is within a second of it and keeps a message rather than dropping it.
        at = time.time()
        if stamp:
            try:
                at = datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
            except ValueError:
                pass
        return Message(
            at_s=round(at - self.origin, 2),
            text=clean(text),
            user=str(sender.get("username", "")) if isinstance(sender, dict) else "",
        )


def watch(channel: str, *, window_s: float, origin: float | None = None) -> LiveChat:
    """Start reading a channel's chat into a bounded log."""
    return LiveChat(
        channel=channel,
        log=LiveLog(window_s=window_s),
        origin=origin if origin is not None else time.time(),
    ).start()
