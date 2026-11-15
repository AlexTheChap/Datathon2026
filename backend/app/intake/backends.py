"""Intake backends: what turns free text into an `IntakeDraft`.

- `BedrockBackend`: Claude Haiku on Amazon Bedrock with structured outputs (the architecture.mmd path, link L2).
- `MockBackend`: deterministic English keyword / regex rules, NO LLM. Used for tests and offline demos.
  It is deliberately simple and does not replace the model in production.

Selected with the `AF_INTAKE_MODE` environment variable: `mock` (default) or `bedrock`.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional, Protocol, get_args

from app.schemas import Exclusion, Position
from app.schemas.profile import Language

from .schemas import AgentToggleChange, IntakeDraft, ProfileDraft, RequestDraft
from .universe import Asset, aliases, load_universe, universe_for_prompt

PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "intake.md"
# Inference profile ("us." prefix): on Bedrock, Haiku 5.5 can only be called through an inference profile.
DEFAULT_BEDROCK_MODEL = "us.anthropic.claude-haiku-5-5"
ALLOWED_LANGUAGES = get_args(Language)  # ("en",) today: the team chose an English-only product


class IntakeBackend(Protocol):
    name: str

    def extract(self, text: str, known_profile: Optional[dict], feedback: Optional[str] = None) -> IntakeDraft: ...


class IntakeRefused(RuntimeError):
    """The model or the guardrail refused the input."""


# --------------------------------------------------------------------------------------------------
# Bedrock (Claude Haiku, structured outputs)
# --------------------------------------------------------------------------------------------------
class BedrockBackend:
    name = "bedrock"

    def __init__(self, model: Optional[str] = None, region: Optional[str] = None, guardrail_id: Optional[str] = None,
                 guardrail_version: Optional[str] = None, client=None):
        self.region = region or os.environ.get("AWS_REGION", "us-east-1")
        self.model = model or os.environ.get("AF_INTAKE_MODEL", DEFAULT_BEDROCK_MODEL)
        if client is None:
            # Lazy import: not needed in mock mode. We use AnthropicBedrock (InvokeModel API), not
            # AnthropicBedrockMantle: in the workshop account (us-east-1, Nov 2026) the Mantle endpoint returns
            # 404 for every model, while InvokeModel accepts the "us." inference profiles.
            from anthropic import AnthropicBedrock

            client = AnthropicBedrock(aws_region=self.region)
        self.client = client  # injectable for tests (same messages.parse interface)
        self.guardrail_id = guardrail_id or os.environ.get("AF_GUARDRAIL_ID")
        self.guardrail_version = guardrail_version or os.environ.get("AF_GUARDRAIL_VERSION", "DRAFT")
        self.system_prompt = PROMPT_PATH.read_text(encoding="utf-8").replace(
            "{universe}", universe_for_prompt(load_universe())
        )

    def _check_guardrail(self, text: str) -> None:
        """Bedrock Guardrails on the input (prompt attacks, personal data), when configured."""
        if not self.guardrail_id:
            return
        import boto3

        try:  # on Windows, botocore does not always see the system certificates (CERTIFICATE_VERIFY_FAILED)
            import truststore

            truststore.inject_into_ssl()
        except ImportError:
            pass

        rt = boto3.client("bedrock-runtime", region_name=self.region)
        out = rt.apply_guardrail(
            guardrailIdentifier=self.guardrail_id, guardrailVersion=self.guardrail_version,
            source="INPUT", content=[{"text": {"text": text}}],
        )
        if out.get("action") == "GUARDRAIL_INTERVENED":
            raise IntakeRefused("Input blocked by Bedrock Guardrails.")

    def extract(self, text: str, known_profile: Optional[dict], feedback: Optional[str] = None) -> IntakeDraft:
        self._check_guardrail(text)
        user = (
            f"Known profile (may be empty):\n{json.dumps(known_profile or {}, ensure_ascii=False)}\n\n"
            f"<user_message>\n{text}\n</user_message>"
        )
        if feedback:
            user += f"\n\nYour previous answer was invalid: {feedback}\nReturn a corrected JSON."
        response = self.client.messages.parse(
            model=self.model,
            max_tokens=4096,
            system=self.system_prompt,
            messages=[{"role": "user", "content": user}],
            output_format=IntakeDraft,
        )
        if response.stop_reason == "refusal":
            raise IntakeRefused("The model refused the request.")
        return response.parsed_output


# --------------------------------------------------------------------------------------------------
# Mock (deterministic English rules, offline)
# --------------------------------------------------------------------------------------------------
_RISK = [  # most specific first
    ("low_moderate", r"low[- ]to[- ]moderate|low-moderate|cautious|careful|preserv\w*\s+(?:my\s+)?capital|conservative"),
    ("low", r"low\s+risk|no\s+risk|very\s+cautious|can't\s+afford\s+(?:any\s+)?loss"),
    ("high", r"high\s+risk|aggressive|bold|high\s+growth|strong\s+growth"),
    ("moderate", r"moderate|balanced"),
]
_EXPERIENCE = [
    ("beginner", r"beginner|novice|never\s+invested|new\s+to\s+investing"),
    ("advanced", r"advanced|experienced|follow\s+the\s+markets?\s+(?:closely|actively)|actively\s+follow"),
    ("intermediate", r"intermediate|some\s+experience"),
]
_TASKS = [  # priority: the first match wins
    ("allocation_proposal", r"allocation|rebalanc|(?:put\w*|invest\w*|allocat\w*|mov\w*|add\w*)\s+(?:\w+\s+){0,2}\d+(?:\.\d+)?\s*%|"
                            r"build\s+(?:me\s+)?a\s+portfolio|how\s+should\s+i\s+split"),
    ("esg_screen", r"\besg\b|controvers|ethical|sustainab"),
    ("risk_review", r"\brisk|volatil|drawdown|diversif|concentrat"),
    ("scenario_analysis", r"crash|stress|scenario|recession|rate\s+hike"),
    ("fundamentals_review", r"fundamental|balance\s+sheet|10-k|financial\s+statements|earnings|valuation"),
    ("news_review", r"\bnews\b|headlines"),
]
_OUT_OF_SCOPE = re.compile(
    r"buy\s+(?:it\s+|them\s+|[\w\s]{0,30}?)for\s+me|sell\s+(?:it\s+|them\s+|[\w\s]{0,30}?)for\s+me|"
    r"place\s+(?:an|the)\s+order|execute\s+(?:the\s+|a\s+)?trade|guaranteed\s+return",
    re.I,
)
_STRESS = re.compile(r"crash|stress|scenario|recession|2022|rate\s+hike", re.I)
_NEWS = re.compile(r"\bnews\b|headlines|hype|buzz|everyone\s+is\s+talking|everybody\s+talks", re.I)
_NO_CRYPTO = re.compile(r"(?:no|avoid|without|exclude|not\s+interested\s+in)\W+(?:\w+\W+){0,3}?(?:crypto|bitcoin)", re.I)
_CONTROVERSY = re.compile(r"(?:avoid|exclude|no)\W+(?:\w+\W+){0,8}?controvers", re.I)
_FOSSIL = re.compile(r"(?:no|avoid|exclude)\W+(?:\w+\W+){0,3}?(?:oil|fossil)", re.I)
_BENCH = re.compile(
    r"(?:vs\.?|versus|compared?\s+(?:to|with)|benchmark(?:ed)?\s+(?:against|to)?)\s*(?:the\s+)?"
    r"(SPY|XIU(?:\.TO)?|QQQ|XBB(?:\.TO)?|AGG|S&P\s*500|TSX(?:\s*60)?|Nasdaq(?:-100)?)",
    re.I,
)
_BENCH_MAP = {"spy": "SPY", "s&p500": "SPY", "xiu": "XIU.TO", "xiu.to": "XIU.TO", "tsx": "XIU.TO", "tsx60": "XIU.TO",
              "qqq": "QQQ", "nasdaq": "QQQ", "nasdaq-100": "QQQ", "xbb": "XBB.TO", "xbb.to": "XBB.TO", "agg": "AGG"}
_MONEY = re.compile(
    r"\$\s*(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(k|m|million)?"
    r"|(?<![\d.,])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(k|m|million)?\s*(?:\$|cad|dollars)",
    re.I,
)
_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_LOSS = re.compile(r"(?:lose|loss)\D{0,30}?(\d+(?:\.\d+)?)\s*%", re.I)
_TICKER_TOKEN = re.compile(r"\b[A-Z]{2,5}(?:\.TO|-USD)?\b")

# Sub-agent toggles said in chat ("I don't care about the news", "bring back the ESG check").
_AGENT_WORD = (r"(?P<word>news|headlines|esg|fundamentals|financials|scenarios?|stress[- ]tests?|macro|"
               r"allocations?|risk(?:\s+analysis)?)\b")
_TOGGLE_OFF = re.compile(
    r"(?:(?:don'?t|do\s+not|never)\s+(?:really\s+)?(?:care|want|need)\s+(?:about\s+|for\s+|to\s+see\s+)?"
    r"|not\s+interested\s+in\s+|(?:skip|ignore|stop\s+showing(?:\s+me)?|turn\s+off|disable|no\s+more|drop)\s+)"
    rf"(?:the\s+|any\s+|me\s+)?{_AGENT_WORD}", re.I)
_TOGGLE_ON = re.compile(
    r"(?:turn\s+on|enable|bring\s+back|include|start\s+showing(?:\s+me)?|i\s+(?:do\s+)?(?:care|want)\s+(?:about\s+)?)"
    rf"\s*(?:the\s+)?{_AGENT_WORD}", re.I)


def _agent_for(word: str) -> str:
    w = word.lower()
    if w in ("news", "headlines"):
        return "news"
    if w == "esg":
        return "esg"
    if w in ("fundamentals", "financials"):
        return "fundamentals"
    if w.startswith(("scenario", "stress", "macro")):
        return "macro_scenarios"
    if w.startswith("allocation"):
        return "allocation"
    return "risk"


def _toggle_changes(text: str) -> tuple[list[AgentToggleChange], str]:
    """Toggle changes said in the text + the text with the 'switch off' phrases removed, so that
    "I don't care about the news" does not also request news (include_rag) or pick the news task."""
    changes = [AgentToggleChange(agent=_agent_for(m.group("word")), enabled=False, quote=m.group(0))
               for m in _TOGGLE_OFF.finditer(text)]
    changes += [AgentToggleChange(agent=_agent_for(m.group("word")), enabled=True, quote=m.group(0))
                for m in _TOGGLE_ON.finditer(text)]
    return changes, _TOGGLE_OFF.sub(" ", text)


def _first(table: list[tuple[str, str]], text: str) -> Optional[str]:
    for value, pattern in table:
        if re.search(pattern, text, re.I):
            return value
    return None


def _to_number(num: str, unit: Optional[str]) -> float:
    n = float(num.replace(",", ""))  # "50,000" -> 50000 ; "1.5" -> 1.5
    unit = (unit or "").lower()
    if unit == "k":
        n *= 1_000
    elif unit.startswith("m"):
        n *= 1_000_000
    return n


def _horizon(text: str) -> tuple[Optional[int], Optional[int]]:
    t = text.lower()
    if m := re.search(r"(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})\s*(?:years|yrs)", t):
        return int(m.group(1)), int(m.group(2))
    if m := re.search(r"(\d{1,2})\s*\+?\s*years\s*(?:or\s+more|and\s+more|\+)", t) or \
            re.search(r"(\d{1,2})\s*\+\s*years", t) or re.search(r"(?:more\s+than|over)\s+(\d{1,2})\s*years", t):
        return int(m.group(1)), None
    if m := re.search(r"(?:horizon|in|within|for)\s+(?:of\s+)?(\d{1,2})\s*years", t):
        return int(m.group(1)), int(m.group(1))
    return None, None


def _language(known: Optional[str]) -> str:
    """Only the values the Profile schema allows: keep the known one if valid, else the first allowed value."""
    return known if known in ALLOWED_LANGUAGES else ALLOWED_LANGUAGES[0]


class MockBackend:
    name = "mock"

    def __init__(self, universe: Optional[dict[str, Asset]] = None):
        self.universe = universe or load_universe()
        self._aliases = sorted(aliases(self.universe).items(), key=lambda kv: -len(kv[0]))

    def _positions(self, text: str, benchmark: Optional[str]) -> list[Position]:
        found: dict[str, int] = {}  # ticker -> character offset in the text
        for tok in _TICKER_TOKEN.finditer(text):
            t = tok.group(0)
            ticker = t if t in self.universe else next((t + s for s in (".TO", "-USD") if t + s in self.universe), None)
            if ticker and ticker != benchmark:
                found.setdefault(ticker, tok.start())
        low = text.lower()
        for alias, ticker in self._aliases:
            if ticker == benchmark or ticker in found:
                continue
            if m := re.search(rf"(?<![\w-]){re.escape(alias)}(?![\w-])", low):
                found[ticker] = m.start()
        positions = []
        for ticker, pos in sorted(found.items(), key=lambda kv: kv[1]):
            start = max(0, pos - 30)
            window = text[start: pos + 30]
            pcts = [(abs(m.start() - (pos - start)), m.group(1)) for m in _PCT.finditer(window)]
            weight = float(min(pcts)[1]) / 100 if pcts else None  # closest percentage to the asset name
            positions.append(Position(ticker=ticker, weight=weight))
        return positions

    def extract(self, text: str, known_profile: Optional[dict], feedback: Optional[str] = None) -> IntakeDraft:
        kp = known_profile or {}
        toggle_changes, request_text = _toggle_changes(text)
        h_min, h_max = _horizon(text)

        exclusions: list[Exclusion] = []
        if _NO_CRYPTO.search(text):
            exclusions.append(Exclusion(type="asset_class", value="crypto"))
        if _CONTROVERSY.search(text):
            exclusions.append(Exclusion(type="controversy", min_severity="major"))
        if _FOSSIL.search(text):
            exclusions.append(Exclusion(type="esg_activity", value="fossil_fuels"))

        capital = None
        if m := _MONEY.search(text):
            capital = _to_number(m.group(1), m.group(2)) if m.group(1) else _to_number(m.group(3), m.group(4))
        loss = float(m.group(1)) / 100 if (m := _LOSS.search(text)) else None

        profile = ProfileDraft(
            risk_band=_first(_RISK, text),
            horizon_min_years=h_min,
            horizon_max_years=h_max,
            experience=_first(_EXPERIENCE, text),
            language=_language(kp.get("language")),
            exclusions=exclusions,
            loss_capacity=loss,
            capital=capital,
            agent_toggle_changes=toggle_changes,
        )

        task = "out_of_scope" if _OUT_OF_SCOPE.search(text) else _first(_TASKS, request_text)
        bench = None
        if m := _BENCH.search(text):
            bench = _BENCH_MAP.get(re.sub(r"\s+", "", m.group(1).lower()))
        stress = bool(_STRESS.search(request_text))
        request = RequestDraft(
            task=task or "risk_review",
            positions=self._positions(text, bench),
            benchmark=bench,
            stress_tests=stress,
            include_rag=bool(_NEWS.search(request_text)),
            intake_confidence=0.9 if task else 0.4,
        )
        return IntakeDraft(profile=profile, request=request)


def get_backend(mode: Optional[str] = None) -> IntakeBackend:
    mode = (mode or os.environ.get("AF_INTAKE_MODE", "mock")).lower()
    if mode == "bedrock":
        return BedrockBackend()
    if mode == "mock":
        return MockBackend()
    raise ValueError(f"Unknown AF_INTAKE_MODE: {mode!r} (expected: mock or bedrock)")
