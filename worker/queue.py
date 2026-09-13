"""Gone. This module exists only to stop a crash loop.

Railway's `queue` service is started with an explicit command, so the
friendly refusal in scripts/start.sh never runs - the container just fails on
a missing module, over and over, and the project shows two crashed services
next to a healthy one.

There is no queue and no scheduler any more. The daily harvest and the posting
slots both run on threads inside the web service, which cannot fail to be
running while its own dashboard answers.

Exits zero rather than raising, because Railway restarts on failure: a clean
exit stops the loop and leaves the service sitting quietly until it is
deleted, which is the actual fix.
"""

from __future__ import annotations

import sys

MESSAGE = """
This service is no longer part of the app and can be deleted.

  Railway -> the 'queue' service -> Settings -> Delete service

Everything runs in 'web' now: the daily harvest and the posting slots are
threads inside it. The Redis plugin can go too - nothing reads it.
"""


def main() -> int:
    print(MESSAGE.strip(), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
