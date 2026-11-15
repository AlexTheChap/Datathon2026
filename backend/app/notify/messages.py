"""The text of an SMS alert: fixed wording, facts only, never advice.

No language model writes these messages. Each line is "<ticker>: <source> on <date> (<what was filed>)",
built from the SEC item labels, followed by the same closing line every time. A last check refuses any
text that contains advice vocabulary, so a future change to the wording cannot slip into a recommendation.
"""
from __future__ import annotations

import re
from datetime import date

from app.notify.news import NewsEvent

MAX_ITEMS = 3            # more than that: "+N more" (one short text, not a wall)
MAX_CHARS = 320          # two SMS segments at most
CLOSING = "Info only, not investment advice. Reply STOP to opt out."
ADVICE_WORDS = re.compile(
    r"\b(buy|sell|hold on|recommend\w*|should|must|opportunit\w*|target price|price target|upgrade\w*|downgrade\w*|"
    r"outperform\w*|underperform\w*|guarantee\w*|profit|act now|don't miss)\b", re.IGNORECASE)


class AdviceWordingError(ValueError):
    pass


def _day(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d:%b} {d.day}"


def check_wording(text: str) -> str:
    m = ADVICE_WORDS.search(text)
    if m:
        raise AdviceWordingError(f"advice wording refused in an alert: {m.group(0)!r}")
    return text


def compose(events: list[NewsEvent], app_url: str | None = None) -> str:
    """One text for one person: up to MAX_ITEMS news items on stocks they hold."""
    if not events:
        raise ValueError("no news to send")
    lines = [f"{e.ticker}: {e.source} on {_day(e.published_at)} ({e.headline})." for e in events[:MAX_ITEMS]]
    more = len(events) - MAX_ITEMS
    head = "AlphaForge: news on stocks you hold."
    tail = ([f"+{more} more in the app."] if more > 0 else []) + ([app_url] if app_url else []) + [CLOSING]
    text = " ".join([head, *lines, *tail])
    while len(text) > MAX_CHARS and len(lines) > 1:   # keep it to two segments: drop items, say how many
        lines.pop()
        more = len(events) - len(lines)
        tail = [f"+{more} more in the app."] + ([app_url] if app_url else []) + [CLOSING]
        text = " ".join([head, *lines, *tail])
    return check_wording(text)
