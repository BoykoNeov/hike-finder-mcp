"""The double-click start: ask who you are once, then open the web UI.

``start-hike-finder.cmd`` runs ``python -m hike_finder.launch``. The OpenStreetMap
servers ask every program for a contact address, and the repo ships none — so this
asks the person at the keyboard, remembers the answer in their per-user folder (never
in the checkout, which may be a public repo), and starts ``web.main(... --open)``.

The question is asked HERE rather than in the batch file because a typed ``&``, ``|``
or ``"`` is inert to Python's ``input()`` but becomes shell syntax in ``set /p``.

Order: ``HIKE_OVERPASS_UA`` if already set (never overridden — same promise as the
scripts/ launchers used to make), else the saved answer, else ask. Enter skips: the
tool still works under its generic identifier, nothing is saved, and it asks again
next time.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from .paths import user_cache_dir

_ENV = "HIKE_OVERPASS_UA"

# Loose on purpose — "email or URL", as the page's Contact box says. The one hard rule
# is that it becomes an HTTP User-Agent, so no whitespace or control characters.
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_URL = re.compile(r"https?://\S+")


def contact_file() -> Path:
    return user_cache_dir() / "contact.txt"


def valid_contact(text: str) -> bool:
    if any(ord(c) < 32 or ord(c) == 127 for c in text):
        return False
    return bool(_EMAIL.fullmatch(text) or _URL.fullmatch(text))


def _saved() -> str | None:
    try:
        text = contact_file().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text if valid_contact(text) else None


def _ask() -> str | None:
    print(
        "The OpenStreetMap servers this uses ask every program for a contact address,\n"
        "so they can reach you if something goes wrong. It is sent only to them.\n"
    )
    while True:
        try:
            answer = input("Your email (or a web address), then Enter - or just Enter to skip: ")
        except EOFError:  # no keyboard attached (piped / closed stdin)
            print()
            return None
        answer = answer.strip()
        if not answer:
            return None
        if valid_contact(answer):
            return answer
        print("That doesn't look like an email or a web address - try again.")


def resolve_contact() -> str | None:
    """The contact to use, asking (and saving) only when nothing is set or saved."""
    if os.environ.get(_ENV):
        return os.environ[_ENV]
    saved = _saved()
    if saved:
        print(f"Contact: {saved}   (to change it, delete {contact_file()})")
        return saved
    answer = _ask()
    if answer is None:
        print("Continuing without a contact. You can also type one into the page's Contact box.")
        return None
    try:
        path = contact_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(answer + "\n", encoding="utf-8")
        print(f"Saved - you won't be asked again. (To change it, delete {path})")
    except OSError as exc:
        print(f"Couldn't save it ({exc}); it will be used for this run only.")
    return answer


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if not ({"-h", "--help"} & set(args)):
        contact = resolve_contact()
        if contact:
            # Config reads the environment per request, so this reaches every search.
            os.environ[_ENV] = contact
        print()
    from . import web

    web.main([*args, "--open"])


if __name__ == "__main__":
    main()
