"""The reader's words in what the user reads: no verdict codes, rule ids or bare tickers.

The Explainer is told to write this way (rule 9 of its templates) and gets a ticker -> name table. This module is
the deterministic side of that rule:
- `problems(text, names)` lists what a sentence still contains (fed back to the model for its one retry);
- `plainify(text, names)` is the last resort after the retry: codes, rule ids and tickers become plain words.
"""
from __future__ import annotations

import re
from functools import lru_cache

# Verdict codes as the tools write them (upper case only: "pass" or "block" in a sentence are ordinary words).
CODES = {"BLOCKED": "flagged", "BLOCKS": "flags", "BLOCK": "a red flag", "WARN": "a point to watch",
         "WARNING": "a point to watch", "PASS": "a green light", "PASSES": "gets a green light",
         "UNVERIFIED": "not confirmed"}
_CODE = re.compile(r"\b(" + "|".join(sorted(CODES, key=len, reverse=True)) + r")\b")

# Rule ids (governance checks), by what they protect. Same wording as the app's check names (portfolio-data.js).
RULES = {
    "SINGLE_POSITION": "the limit on any single company",
    "SECTOR": "the limit on any one sector",
    "CRYPTO": "your crypto limit",
    "SHORT_HORIZON_VOLATILITY": "the cap on big-swing assets for a short horizon",
    "SHORT_HORIZON_DEFENSIVE": "the rule that keeps enough steady assets for a short horizon",
    "EXCLUSION_CONTROVERSY": "your exclusion of companies with major controversies",
    "ESG_INDEX_PREFERENCE": "your ESG preference",
}
_RULE = re.compile(r"\b[A-Z][A-Z0-9]+(?:_[A-Z0-9]+)+\b")
_GOVERNANCE = re.compile(r"\b(?:[Tt]he )?[Gg]overnance(?: check| verdict| tool)?\b")
_ACTIVE = re.compile(r"\b(?:active|binding) constraints?\b", re.I)


@lru_cache(maxsize=1)
def universe_names() -> dict[str, str]:
    """Every ticker of the universe (curated + S&P 500 extension) -> the name a reader knows."""
    from app.intake.universe import COMMON_WORDS, display_name, load_universe

    return {t: display_name(a.name) for t, a in load_universe().items()
            if len(t.split(".")[0].split("-")[0]) > 1 and t not in COMMON_WORDS}   # "A", "ALL"…: ordinary words


def _ticker_re(tickers) -> re.Pattern | None:
    if not tickers:
        return None
    alts = "|".join(re.escape(t) for t in sorted(tickers, key=len, reverse=True))
    return re.compile(rf"(?<![\w.&/-])({alts})(?![\w&/-]|\.\w)")


def names_in(text: str, names: dict[str, str] | None = None) -> dict[str, str]:
    """The tickers that appear in `text`, with their names (the table given to the Explainer)."""
    names = universe_names() if names is None else names
    rx = _ticker_re(names)
    return {} if rx is None else {t: names[t] for t in sorted(set(rx.findall(text)))}


def problems(text: str, names: dict[str, str]) -> list[str]:
    found = [f'the code "{m}"' for m in dict.fromkeys(_CODE.findall(text))]
    found += [f'the rule id "{m}"' for m in dict.fromkeys(_RULE.findall(text))]
    rx = _ticker_re(names)
    found += [f'the ticker "{t}" (write "{names[t]}")' for t in dict.fromkeys(rx.findall(text) if rx else [])]
    if _GOVERNANCE.search(text):
        found.append('the word "governance" (say "your profile\'s limits")')
    if _ACTIVE.search(text):
        found.append('the phrase "active constraint"')
    return found


def plainify(text: str | None, names: dict[str, str]) -> str | None:
    """Last resort: the same sentence with plain words in place of codes, rule ids and tickers."""
    if not text:
        return text
    rx = _ticker_re(names)
    if rx is not None:
        text = re.sub(rf"\s*\(\s*{rx.pattern}\s*\)", "", text)        # "Archer Daniels Midland (ADM)" -> the name
        text = rx.sub(lambda m: names[m.group(1)], text)
    text = _RULE.sub(lambda m: RULES.get(m.group(0), "one of your profile's limits"), text)
    text = _CODE.sub(lambda m: CODES[m.group(0)], text)
    text = _ACTIVE.sub("limit that shaped the result", text)
    text = _GOVERNANCE.sub(lambda m: "The check against your profile" if m.group(0)[0].isupper()
                           else "the check against your profile", text)
    return re.sub(r"\ba (an?) ", r"\1 ", text)                         # "returns a a red flag" -> "a red flag"
