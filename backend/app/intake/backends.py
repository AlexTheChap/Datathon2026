"""Backends de l'Intake : qui transforme le texte libre en `IntakeDraft`.

- `BedrockBackend` : Claude Haiku sur Amazon Bedrock, structured outputs (le chemin de architecture.mmd, lien L2).
- `MockBackend` : règles déterministes (mots-clés, regex), SANS LLM. Sert aux tests et à la démo hors ligne.
  Il est volontairement simple : il ne remplace pas le modèle en production.

Choix par variable d'environnement `AF_INTAKE_MODE` = `mock` (défaut) ou `bedrock`.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Optional, Protocol, get_args

from app.schemas import Exclusion, Position
from app.schemas.profile import Language

from .schemas import IntakeDraft, ProfileDraft, RequestDraft
from .universe import Asset, aliases, load_universe, universe_for_prompt

PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "intake.md"
# Profil d'inférence (préfixe « us. ») : sur Bedrock, Haiku 5.5 n'est appelable que par un profil d'inférence.
DEFAULT_BEDROCK_MODEL = "us.anthropic.claude-haiku-5-5"


class IntakeBackend(Protocol):
    name: str

    def extract(self, text: str, known_profile: Optional[dict], feedback: Optional[str] = None) -> IntakeDraft: ...


class IntakeRefused(RuntimeError):
    """Le modèle ou le garde-fou a refusé l'entrée."""


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
            # Import paresseux : inutile en mode mock. On utilise AnthropicBedrock (API InvokeModel) et non
            # AnthropicBedrockMantle : dans le compte du workshop (us-east-1, nov. 2026), le point d'accès Mantle
            # renvoie 404 pour tous les modèles, alors qu'InvokeModel accepte les profils d'inférence « us. ».
            from anthropic import AnthropicBedrock

            client = AnthropicBedrock(aws_region=self.region)
        self.client = client  # injectable pour les tests (même interface messages.parse)
        self.guardrail_id = guardrail_id or os.environ.get("AF_GUARDRAIL_ID")
        self.guardrail_version = guardrail_version or os.environ.get("AF_GUARDRAIL_VERSION", "DRAFT")
        self.system_prompt = PROMPT_PATH.read_text(encoding="utf-8").replace(
            "{universe}", universe_for_prompt(load_universe())
        )

    def _check_guardrail(self, text: str) -> None:
        """Bedrock Guardrails sur l'entrée (attaque d'invite, données personnelles), si configuré."""
        if not self.guardrail_id:
            return
        import boto3

        try:  # sous Windows, botocore ne voit pas toujours les certificats du système (CERTIFICATE_VERIFY_FAILED)
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
# Mock (règles déterministes, hors ligne)
# --------------------------------------------------------------------------------------------------
_FR_HINTS = re.compile(r"(?<!\w)(je|j'|moi|mon|ma|mes|est-ce|portefeuille|ans|pas|veux|dans|risqué|placement|actions|de|des|les|et|maintenant)(?!\w)", re.I)
_EN_HINTS = re.compile(r"\b(i|i'm|my|me|is|the|and|portfolio|years|want|should|what|too)\b", re.I)


ALLOWED_LANGUAGES = get_args(Language)  # aujourd'hui ("en",) : l'équipe a choisi un produit en anglais


def _language(text: str, known: Optional[str]) -> Optional[str]:
    """Message language, limited to the values the Profile schema allows.

    If the text is too short to tell, or its language is not supported, keep the known profile's language,
    else fall back to the first allowed value.
    """
    fr, en = len(_FR_HINTS.findall(text)), len(_EN_HINTS.findall(text))
    detected = "fr" if fr >= 2 and fr > en else "en" if en >= 2 and en > fr else None
    for candidate in (detected, known):
        if candidate in ALLOWED_LANGUAGES:
            return candidate
    return ALLOWED_LANGUAGES[0] if text.strip() else known

_RISK = [  # du plus spécifique au plus général
    ("low_moderate", r"faible\s+à\s+modéré|low[- ]to[- ]moderate|low-moderate|prudent|préserv\w*\s+(?:mon\s+)?capital|preserv\w*\s+(?:my\s+)?capital|conservat"),
    ("low", r"risque\s+faible|aucun\s+risque|low\s+risk|no\s+risk|très\s+prudent"),
    ("high", r"risque\s+élevé|audacieu|agressi|aggressive|high\s+risk|forte\s+croissance|high\s+growth"),
    ("moderate", r"modéré|équilibr|moderate|balanced"),
]
_EXPERIENCE = [
    ("beginner", r"débutant|novice|beginner|jamais\s+investi|never\s+invested|new\s+to\s+investing"),
    ("advanced", r"avancé|expériment|experienced|advanced|suis\s+activement|follow\s+the\s+markets?\s+closely"),
    ("intermediate", r"intermédiaire|intermediate|un\s+peu\s+d'expérience|some\s+experience"),
]
_TASKS = [  # priorité : la première qui correspond gagne
    ("allocation_proposal", r"allocation|répartir|répartition|rééquilibr|rebalanc|(?:mett\w*|met|plac\w*|invest\w*|put|allocat\w*)\s+(?:\w+\s+){0,2}\d+(?:[.,]\d+)?\s*%|construi\w*\s+(?:un\s+)?portefeuille|build\s+(?:me\s+)?a\s+portfolio|how\s+should\s+i\s+split"),
    ("esg_screen", r"\besg\b|controvers|éthique|ethical|sustainab|durable"),
    ("risk_review", r"risqu|\brisk|volatil|drawdown|diversifi|concentr"),
    ("scenario_analysis", r"krach|crash|stress|scénario|scenario|récession|recession|hausse\s+des\s+taux|rate\s+hike"),
    ("fundamentals_review", r"fondament|fundamental|bilan|balance\s+sheet|10-k|états\s+financiers|earnings|valorisation|valuation"),
    ("news_review", r"nouvelles|actualit|\bnews\b"),
]
_OUT_OF_SCOPE = re.compile(
    r"\bachète|\bachete-moi|\bvends?\s+(?:mes|mon|pour)|passe[rz]?\s+(?:l'|un\s+)?ordre|exécute[rz]?\s+(?:la\s+)?transaction|"
    r"buy\s+(?:it\s+|them\s+|\w+\s+)?for\s+me|sell\s+(?:it\s+)?for\s+me|place\s+(?:an|the)\s+order|execute\s+(?:the\s+)?trade|"
    r"rendement\s+garanti|guaranteed\s+return",
    re.I,
)
_STRESS = re.compile(r"krach|crash|stress|scénario|scenario|récession|recession|2022|hausse\s+des\s+taux|rate\s+hike", re.I)
_H1 = re.compile(r"premier\s+semestre|first\s+half|\bH1\b", re.I)
_NEWS = re.compile(r"nouvelles|actualit|\bnews\b|en\s+parle|hype|buzz|everyone\s+is\s+talking", re.I)
_NO_CRYPTO = re.compile(
    r"(?:pas\s+de|aucune?|sans|évite\w*|exclu\w*|peu\s+intéress\w*\s+par|pas\s+intéress\w*\s+par|no|avoid|without|not\s+interested\s+in)"
    r"\W+(?:\w+\W+){0,3}?(?:crypto|bitcoin)",
    re.I,
)
_CONTROVERSY = re.compile(r"(?:évite\w*|exclu\w*|pas\s+de|avoid|exclude|no)\W+(?:\w+\W+){0,6}?controvers", re.I)
_FOSSIL = re.compile(r"(?:pas\s+de|sans|évite\w*|exclu\w*|no|avoid|exclude)\W+(?:\w+\W+){0,3}?(?:pétrol|petrol|oil|fossil|combustibles?\s+fossiles?)", re.I)
_BENCH = re.compile(
    r"(?:vs\.?|versus|compar\w*\s+(?:à|au|avec|to|with)|benchmark|indice\s+de\s+référence)\s+(?:le\s+|la\s+|the\s+|l')?"
    r"(SPY|XIU(?:\.TO)?|QQQ|XBB(?:\.TO)?|AGG|S&P\s*500|TSX(?:\s*60)?|Nasdaq(?:-100)?)",
    re.I,
)
_BENCH_MAP = {"spy": "SPY", "s&p500": "SPY", "xiu": "XIU.TO", "xiu.to": "XIU.TO", "tsx": "XIU.TO", "tsx60": "XIU.TO",
              "qqq": "QQQ", "nasdaq": "QQQ", "nasdaq-100": "QQQ", "xbb": "XBB.TO", "xbb.to": "XBB.TO", "agg": "AGG"}
_MONEY = re.compile(
    r"(?<![\d.,])(\d{1,3}(?:[   ,]\d{3})+|\d+(?:[.,]\d+)?)\s*(k|m|mille|millions?)?\s*(?:\$|cad|dollars)"
    r"|\$\s*(\d{1,3}(?:[,  ]\d{3})+|\d+(?:\.\d+)?)\s*(k|m)?",
    re.I,
)
_PCT = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")
_LOSS = re.compile(r"(?:perdre|perte|lose|loss)\D{0,30}?(\d+(?:[.,]\d+)?)\s*%", re.I)
_TICKER_TOKEN = re.compile(r"\b[A-Z]{2,5}(?:\.TO|-USD)?\b")


def _first(table: list[tuple[str, str]], text: str) -> Optional[str]:
    for value, pattern in table:
        if re.search(pattern, text, re.I):
            return value
    return None


def _to_number(num: str, unit: Optional[str]) -> float:
    if re.fullmatch(r"\d{1,3}(?:[   ,]\d{3})+", num):  # « 400 000 » ou « 50,000 » : séparateurs de milliers
        n = float(re.sub(r"\D", "", num))
    else:  # « 1,5 » ou « 1.5 »
        n = float(num.replace(",", "."))
    unit = (unit or "").lower()
    if unit in ("k", "mille"):
        n *= 1_000
    elif unit.startswith("m"):
        n *= 1_000_000
    return n


def _horizon(text: str) -> tuple[Optional[int], Optional[int]]:
    t = text.lower()
    if m := re.search(r"(\d{1,2})\s*(?:à|a|-|–|to)\s*(\d{1,2})\s*(?:ans|years|yrs)", t):
        return int(m.group(1)), int(m.group(2))
    if m := re.search(r"(\d{1,2})\s*(?:\+\s*)?(?:ans|years)\s*(?:et\s+plus|ou\s+plus|or\s+more|and\s+more|\+)", t) or \
            re.search(r"(\d{1,2})\s*\+\s*(?:ans|years)", t) or re.search(r"(?:plus\s+de|more\s+than|over)\s+(\d{1,2})\s*(?:ans|years)", t):
        return int(m.group(1)), None
    if m := re.search(r"(?:horizon|dans|d'ici|in|within|pour|for)\s+(?:de\s+|of\s+)?(\d{1,2})\s*(?:ans|years)", t):
        return int(m.group(1)), int(m.group(1))
    return None, None


class MockBackend:
    name = "mock"

    def __init__(self, universe: Optional[dict[str, Asset]] = None):
        self.universe = universe or load_universe()
        self._aliases = sorted(aliases(self.universe).items(), key=lambda kv: -len(kv[0]))

    def _positions(self, text: str, benchmark: Optional[str]) -> list[Position]:
        found: dict[str, int] = {}  # ticker -> position dans le texte
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
            window = text[max(0, pos - 30): pos + 30]
            pcts = [(abs(m.start() - (pos - max(0, pos - 30))), m.group(1)) for m in _PCT.finditer(window)]
            weight = float(min(pcts)[1].replace(",", ".")) / 100 if pcts else None
            positions.append(Position(ticker=ticker, weight=weight))
        return positions

    def extract(self, text: str, known_profile: Optional[dict], feedback: Optional[str] = None) -> IntakeDraft:
        kp = known_profile or {}
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
        loss = float(m.group(1).replace(",", ".")) / 100 if (m := _LOSS.search(text)) else None

        profile = ProfileDraft(
            risk_band=_first(_RISK, text),
            horizon_min_years=h_min,
            horizon_max_years=h_max,
            experience=_first(_EXPERIENCE, text),
            language=_language(text, kp.get("language")),
            exclusions=exclusions,
            loss_capacity=loss,
            capital=capital,
        )

        task = "out_of_scope" if _OUT_OF_SCOPE.search(text) else _first(_TASKS, text)
        bench = None
        if m := _BENCH.search(text):
            bench = _BENCH_MAP.get(re.sub(r"\s+", "", m.group(1).lower()))
        stress = bool(_STRESS.search(text))
        request = RequestDraft(
            task=task or "risk_review",
            positions=self._positions(text, bench),
            scenarios=(["2022_H1"] if _H1.search(text) else ["2022"]) if stress else [],
            benchmark=bench,
            stress_tests=stress,
            include_rag=bool(_NEWS.search(text)),
            intake_confidence=0.9 if task else 0.4,
        )
        return IntakeDraft(profile=profile, request=request)


def get_backend(mode: Optional[str] = None) -> IntakeBackend:
    mode = (mode or os.environ.get("AF_INTAKE_MODE", "mock")).lower()
    if mode == "bedrock":
        return BedrockBackend()
    if mode == "mock":
        return MockBackend()
    raise ValueError(f"AF_INTAKE_MODE inconnu : {mode!r} (attendu : mock ou bedrock)")
