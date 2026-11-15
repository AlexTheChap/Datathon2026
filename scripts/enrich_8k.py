"""Read the SEC 8-K filings of the news index and store what they say: data/external/news/news_index.csv `excerpt`.

    .venv/bin/python scripts/enrich_8k.py --email you@example.com            # every 8-K event without an excerpt
    .venv/bin/python scripts/enrich_8k.py --email you@example.com --tickers NVDA XOM --refresh
    .venv/bin/python scripts/ingest_news.py --sources macro                   # then rebuild kb/ (the KB documents)

An 8-K event used to be a title ("NVDA: Form 8-K (executive departure or appointment)"). Here the filing itself (the primary
document at `source_url`, public domain) is downloaded and the text of the reported item(s) is kept: who left or joined, what
was acquired, what was issued, what was impaired. Up to ~700 characters, cut at a sentence end, with the standard item
heading removed. The full filing is never stored. For an Item 2.02 (results) the 8-K body only announces the press release:
the excerpt says so, and the figures are in the XBRL fundamentals (scripts/ingest_fundamentals.py), not here.

Rows that cannot be read (HTTP error) keep an empty excerpt and are retried on the next run. A filing whose body only
refers to attached exhibits gets a one-line note saying so (COVER_NOTE).
SEC rule: at most 10 requests per second, contact e-mail in the User-Agent.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
import warnings
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
MAX_CHARS = 700
MIN_CHARS = 60
COVER_NOTE = ("The body of this 8-K only refers to attached exhibits (not read): it states no facts of its own.")

# Standard headings of the 8-K items kept by ingest_news.ITEMS_8K (removed from the excerpt: they say nothing).
ITEM_TITLES = {
    "1.01": "Entry into a Material Definitive Agreement",
    "1.03": "Bankruptcy or Receivership",
    "1.05": "Material Cybersecurity Incidents",
    "2.01": "Completion of Acquisition or Disposition of Assets",
    "2.02": "Results of Operations and Financial Condition",
    "2.05": "Costs Associated with Exit or Disposal Activities",
    "2.06": "Material Impairments",
    "3.01": ("Notice of Delisting or Failure to Satisfy a Continued Listing Rule or Standard; "
             "Transfer of Listing"),
    "4.02": ("Non-Reliance on Previously Issued Financial Statements or a Related Audit Report or "
             "Completed Interim Review"),
    "5.02": ("Departure of Directors or Certain Officers; Election of Directors; Appointment of Certain "
             "Officers; Compensatory Arrangements of Certain Officers"),
    "8.01": "Other Events",
}
# Other wordings of the same heading seen in the filings ("Principal" instead of "Certain" Officers, singular "Arrangement").
ALT_TITLES = {
    "5.02": [
        ("Departure of Directors or Certain Officers; Election of Directors; Appointment of Certain Officers; "
         "Compensatory Arrangement of Certain Officers"),
        "Departure of Directors or Principal Officers; Election of Directors; Appointment of Principal Officers",
        ("Departure of Directors or Principal Officers; Election of Directors; Appointment of Principal Officers; "
         "Compensatory Arrangements of Certain Officers"),
    ],
}
HEADING = re.compile(r"\bItem\s+(\d\.\d{2})\b\.?", re.IGNORECASE)
STOP = re.compile(r"\b(SIGNATURES?|Pursuant to the requirements of the Securities Exchange Act)\b")


def html_to_text(html: str) -> str:
    from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
    soup = BeautifulSoup(html, "lxml")
    for t in soup(["script", "style"]):
        t.decompose()
    for t in soup.find_all(style=re.compile(r"display:\s*none")):
        t.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ").replace("\xa0", " ")).strip()


def _strip_title(code: str, body: str) -> str:
    """Remove the standard heading words that follow `Item x.xx` (any capitalisation, current or older wording)."""
    for title in [ITEM_TITLES.get(code), *ALT_TITLES.get(code, [])]:
        if not title:
            continue
        words = [re.escape(w) for w in title.split()]
        pat = r"^[\s.:;,\-]*" + r"\s+".join(words) + r"[\s.:;,\-]*"
        pat = pat.replace("Results\\s+of", "Results\\s+(?:of|from)")        # "Results from Operations ..." is also seen
        new = re.sub(pat, "", body, count=1, flags=re.IGNORECASE)
        if new != body:
            body = new
            break
    return body.strip(" .:;-")


# A full stop that ends a sentence: not after an abbreviation ("Mr.", "Inc.", "U.S.", "No.") nor inside a number.
SENTENCE_END = re.compile(r"(?<!\bMr)(?<!\bMrs)(?<!\bMs)(?<!\bDr)(?<!\bInc)(?<!\bCorp)(?<!\bCo)(?<!\bLtd)(?<!\bNo)"
                          r"(?<!\bSt)(?<!\bJr)(?<!\bSr)(?<!\bU\.S)(?<!\bv)(?<!\bvs)(?<!\bEx)[.!?](?=\s+[A-Z“\"(])")


def _cut(text: str, limit: int = MAX_CHARS) -> str:
    """At most `limit` characters, ending on a sentence end when there is one."""
    if len(text) <= limit:
        return text
    head = text[:limit]
    ends = [m.end() for m in SENTENCE_END.finditer(head)]
    if ends and ends[-1] > limit * 0.5:
        return head[:ends[-1]].strip()
    return head.rsplit(" ", 1)[0].rstrip(",;: ") + " …"


def extract_excerpt(text: str, codes: list[str]) -> str:
    """Text of the requested items of an 8-K (plain text of the primary document), without headings.

    A filing can name an item several times: in a table of contents, in a sentence ("furnished pursuant to Item 2.02 and
    Item 7.01") and at the real heading. Each occurrence gets the text that follows it (up to the next heading or the
    signatures); an occurrence followed at once by another heading takes the text of that heading instead (except 9.01, the
    exhibit list); the longest candidate is the item's body."""
    marks = [(m.start(), m.end(), m.group(1)) for m in HEADING.finditer(text)]
    stop = STOP.search(text)
    stop_at = stop.start() if stop else len(text)

    def segment(k: int) -> str:
        end = marks[k + 1][0] if k + 1 < len(marks) else len(text)
        end = min(end, stop_at) if stop_at > marks[k][1] else end
        return _strip_title(marks[k][2], text[marks[k][1]:end])

    parts: list[str] = []
    for code in codes:
        if code == "9.01":
            continue
        best = ""
        for k, (_, _, c) in enumerate(marks):
            if c != code:
                continue
            body, m = segment(k), k
            while len(body) < MIN_CHARS and m + 1 < len(marks) and marks[m + 1][2] != "9.01":
                m += 1
                body = segment(m)
            if len(body) > len(best):
                best = body
        if len(best) >= MIN_CHARS:
            parts.append(best)
    return _cut(" ".join(parts))


def codes_of(summary: str) -> list[str]:
    m = re.search(r"Items:\s*([\d., ]+)\.", str(summary))
    return re.findall(r"\d\.\d{2}", m.group(1)) if m else []


def main() -> int:
    import requests

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--email", required=True, help="contact e-mail for the SEC User-Agent (required by the SEC)")
    ap.add_argument("--index", default=str(ROOT / "data/external/news/news_index.csv"))
    ap.add_argument("--tickers", nargs="*", help="only these tickers (as in the index, e.g. NVDA CNR.TO)")
    ap.add_argument("--refresh", action="store_true", help="read again the events that already have an excerpt")
    ap.add_argument("--limit", type=int, help="stop after this many filings (test)")
    a = ap.parse_args()

    path = Path(a.index)
    df = pd.read_csv(path, dtype=str).fillna("")
    if "excerpt" not in df.columns:
        df["excerpt"] = ""
    todo = df[(df["origin"] == "sec_8k") & (a.refresh | (df["excerpt"] == ""))]
    if a.tickers:
        todo = todo[todo["tickers"].isin(a.tickers)]
    if a.limit:
        todo = todo.head(a.limit)
    h = {"User-Agent": f"Fitfolio Datathon {a.email}"}
    session = requests.Session()
    done = failed = 0
    for n, (i, r) in enumerate(todo.iterrows(), 1):
        time.sleep(0.15)                                       # SEC: at most 10 requests per second
        try:
            resp = session.get(r["source_url"], headers=h, timeout=40)
            if resp.status_code != 200:
                raise RuntimeError(f"HTTP {resp.status_code}")
            text = html_to_text(resp.text)
            if len(text) < 200:
                raise RuntimeError("document too short to be a filing")
            excerpt = extract_excerpt(text, codes_of(r["summary"])) or COVER_NOTE
        except Exception as e:  # noqa: BLE001  one unreadable filing must not stop the batch
            failed += 1
            print(f"  [{n}/{len(todo)}] {r['tickers']} {r['published_at']}: not read ({e})", file=sys.stderr)
            continue
        base = re.sub(r"\s+Excerpt:.*$", "", r["summary"], flags=re.DOTALL)
        df.loc[i, "excerpt"] = excerpt
        df.loc[i, "summary"] = f"{base} Excerpt: {excerpt}"
        done += 1
        if done % 50 == 0:
            df.to_csv(path, index=False)
            print(f"  {n}/{len(todo)} read ...", flush=True)
    df.to_csv(path, index=False)
    print(f"{done} filings read, {failed} not read, {len(todo)} asked → {path}")
    return 0 if done or not len(todo) else 1


if __name__ == "__main__":
    raise SystemExit(main())
