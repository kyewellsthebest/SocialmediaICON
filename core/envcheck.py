"""Say why a connection variable is unusable, not merely that it is.

"X is not set" covers three different faults with three different fixes, and
they are indistinguishable from the outside: never added; added but the
Railway ${{Service.VAR}} reference resolved to an empty string; or pasted as
text so the braces are still in it. Guessing between them cost an evening on
REDIS_URL, and then cost part of another on DATABASE_URL.

Run directly to print the diagnosis for one variable:

    python -m core.envcheck REDIS_URL

Names only, never values - these are credentials.
"""

from __future__ import annotations

import os
import sys

#: Prefixes of the connection variables worth listing, so the log shows what
#: really arrived. Prefixes rather than substrings: "PG" appears inside plenty
#: of unrelated names (USE_BUILTIN_RIPGREP, for one) and a noisy list is a
#: list nobody reads.
RELATED = ("REDIS", "DATABASE", "POSTGRES", "PG")


def explain(name: str, *, service_hint: str = "") -> str:
    raw = os.environ.get(name)
    lines: list[str] = []

    if raw is None:
        lines += [
            f"{name} is absent from this process's environment entirely.",
            "The variable was never added to THIS service - Railway does not",
            "share variables between services, so setting it on web does not",
            "give it to worker or scheduler.",
        ]
    elif not raw.strip():
        lines += [
            f"{name} is present but EMPTY.",
            "That is what a Railway reference looks like when it cannot resolve.",
            "Delete it and re-add it with Railway's variable picker rather than",
            "typing the reference: a typed one breaks silently if the service",
            f"name does not match exactly{f' (expected {service_hint})' if service_hint else ''},",
            "capitals included, or if the service was ever renamed.",
        ]
    elif raw.strip().startswith("${{"):
        lines += [
            f"{name} is the literal text {raw.strip()!r}.",
            "Railway did not expand it. Re-add it with the variable picker",
            "instead of pasting the reference as text.",
        ]
    else:
        lines += [
            f"{name} *is* set and looks usable, so this is our bug rather than",
            "a configuration one. Please send this line to the developer.",
        ]

    related = sorted(
        key for key in os.environ if key.upper().startswith(RELATED)
    )
    lines += ["", f"Connection variables this process can see: {', '.join(related) or 'none'}"]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: python -m core.envcheck VARNAME", file=sys.stderr)
        return 2
    hint = {"REDIS_URL": "Redis", "DATABASE_URL": "Postgres"}.get(argv[0], "")
    print(explain(argv[0], service_hint=hint))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
