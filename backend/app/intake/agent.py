"""Intake agent (box 3): free text -> validated Profile JSON + Request JSON.

Steps of `run_intake`:
1. read the already stored profile (store) and the profile form;
2. ask the backend (Bedrock Haiku or mock) for a draft, retrying once if the output is invalid;
3. merge: stored profile < free text < form (the explicit form always wins); sub-agent toggles the user
   switches on/off in the text are kept only if their quote is really in the message, and are saved;
4. add the server-side fields (actor_id, request_id, as_of) and validate with Pydantic;
5. checks: tickers outside the universe, weights > 100%, conflict with an exclusion, date after the data;
6. pick the status: ready, needs_clarification (at most one question), out_of_scope, or profile_updated (the
   message only changed settings: they are saved and confirmed, no analysis runs);
7. store the profile if it is complete.

The Intake analyses nothing and blocks nothing: governance (box 5) decides. It only flags.
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import date
from typing import Any, Optional

from pydantic import ValidationError

from app.schemas import Parameters, PortfolioRef, Position, Profile, Request, Toggles
from app.store.profiles import ProfileStore

from .backends import IntakeBackend, IntakeRefused, IntakeUnavailable, get_backend, split_preferences_by_rules
from .schemas import AgentToggleChange, IntakeDraft, IntakeResult
from .universe import load_universe, normalize_ticker

log = logging.getLogger(__name__)
DATA_END = date(2026, 11, 12)  # last date of the dataset (see schemas/enums.py)
MIN_CONFIDENCE = 0.6
REQUIRED_PROFILE_FIELDS = ("risk_band", "experience", "language")

QUESTIONS = {
    "risk_band": "What level of risk are you comfortable with: low, low-to-moderate, moderate or high?",
    "experience": "How would you describe your investing experience: beginner, intermediate or advanced?",
    "language": "Which language would you like the answers in?",
    "task": "What would you like me to analyse: portfolio risk, a rate or market scenario, an allocation, an ESG screen, "
            "fundamentals, news, or the range of what your portfolio could be worth later?",
}


class IntakeError(RuntimeError):
    """The backend did not produce a valid draft, even after a retry."""


def _extract_with_retry(backend: IntakeBackend, text: str, known: dict) -> IntakeDraft:
    try:
        try:
            return backend.extract(text, known)
        except (ValidationError, ValueError) as first_error:
            try:
                return backend.extract(text, known, feedback=str(first_error))
            except (ValidationError, ValueError) as second_error:
                raise IntakeError(f"Invalid output after 2 attempts: {second_error}") from second_error
    except IntakeUnavailable as e:  # the intake Lambda is down: same visible error for the caller (API 502)
        raise IntakeError(str(e)) from e
    except (IntakeRefused, IntakeError):
        raise
    except Exception as e:  # the model could not be reached (expired AWS credentials, throttling, network...)
        raise IntakeError(f"The language model could not be reached: {type(e).__name__}: {str(e)[:300]}") from e


def derive_preferences(text: str, backend: IntakeBackend) -> tuple[list[str], list[str]]:
    """The profile form's free text -> (preferences, constraints), with the same backend as the chat intake
    (AF_INTAKE_MODE): the model's extraction in bedrock / lambda mode, simple rules in mock mode.
    Raises IntakeRefused when the model or guardrail refuses the text. When the backend is unreachable the rules
    are used instead, so saving the profile never fails because of the language model."""
    if not text.strip():
        return [], []
    splitter = getattr(backend, "split_preferences", None)
    if splitter is not None:
        return splitter(text)
    try:
        draft = _extract_with_retry(backend, text, {})
    except IntakeError as e:
        log.warning("Preferences split by rules, the intake backend failed: %s", e)
        return split_preferences_by_rules(text)
    return list(draft.profile.preferences), list(draft.profile.constraints)
def _dedupe(items: list) -> list:
    out = []
    for item in items:
        if item not in out:
            out.append(item)
    return out


def _known_profile(stored: Optional[Profile], form: Optional[dict]) -> dict[str, Any]:
    """What we already know before reading the text (sent to the model as context). The saved portfolio is left out:
    the model extracts only what the message says; code adds the portfolio for "my portfolio" questions."""
    known: dict[str, Any] = stored.model_dump(mode="json", exclude={"actor_id", "schema_version", "portfolio",
                                                                   "portfolio_run_id"}) if stored else {}
    if stored is not None and stored.portfolio:
        known["has_saved_portfolio"] = True  # the model knows "my portfolio" is clear, not what is in it
    known.update({k: v for k, v in (form or {}).items() if v is not None})
    return known


def _normalize(text: str) -> str:
    return " ".join(text.replace("\u2019", "'").lower().split())


def _verified_toggle_changes(text: str, changes: list[AgentToggleChange], warnings: list[str]) -> list[AgentToggleChange]:
    """Keep a toggle change only if its quote is really in the message (the model cannot invent one).
    One change per agent: the last one wins."""
    message, kept = _normalize(text), {}
    for change in changes:
        quote = _normalize(change.quote)
        if not quote or quote not in message:
            warnings.append(f"Ignored a change to the '{change.agent}' analysis: its quoted words are not in the message.")
            continue
        kept[change.agent] = change
    return list(kept.values())


def _merge_profile(stored: Optional[Profile], draft: IntakeDraft, form: Optional[dict],
                   toggle_changes: Optional[list[AgentToggleChange]] = None) -> dict[str, Any]:
    merged: dict[str, Any] = stored.model_dump(exclude={"actor_id", "schema_version"}) if stored else {}
    d = draft.profile

    for field in ("risk_band", "experience", "language", "objective", "loss_capacity", "capital", "wants_to_learn"):
        value = getattr(d, field)
        if value is not None:
            merged[field] = value
    if d.horizon_min_years is not None or d.horizon_max_years is not None:
        merged["horizon_years"] = {"min": d.horizon_min_years, "max": d.horizon_max_years}
    # Lists accumulate: the text can add an exclusion, never silently remove one.
    for field in ("preferences", "constraints"):
        merged[field] = _dedupe(list(merged.get(field, [])) + list(getattr(d, field)))
    merged["exclusions"] = _dedupe(list(merged.get("exclusions", [])) + [e.model_dump() for e in d.exclusions])

    # Sub-agent toggles: unlike exclusions, the text can switch an analysis back on. Saved with the user's words.
    toggles = dict(merged.get("agent_toggles") or {})
    reasons = dict(merged.get("agent_toggle_reasons") or {})
    for change in toggle_changes or []:
        toggles[change.agent] = change.enabled
        reasons[change.agent] = change.quote

    for key, value in (form or {}).items():  # the form is explicit: it wins
        if value is None:
            continue
        if key == "agent_toggles":  # per agent: a partial form must not silently re-enable the others
            toggles.update(value)
            for agent in value:
                reasons.pop(agent, None)  # set in the form, not said in chat: no quote to keep
        elif key == "agent_toggle_reasons":
            continue  # only the chat sets reasons
        else:
            merged[key] = value
    if toggles:
        merged["agent_toggles"] = toggles
    merged["agent_toggle_reasons"] = reasons
    return merged


_ANALYSIS_NAMES = {"risk": "Portfolio risk", "macro_scenarios": "Macro and rates", "esg": "ESG screening",
                   "fundamentals": "Company fundamentals", "news": "News", "allocation": "Allocation proposal"}
_RISK_WORDS = {"low": "low", "low_moderate": "low to moderate", "moderate": "moderate", "high": "high"}
# The fields that shape a portfolio: changing one means the portfolio is rebuilt (the dashboard's SHAPING list).
_SHAPING = ("risk_band", "horizon_years", "capital", "loss_capacity", "exclusions", "preferences", "constraints")


def _limit_words(e: dict) -> str:
    if e.get("type") == "asset_class" and e.get("value") == "crypto":
        return "no cryptocurrencies"
    if e.get("type") == "controversy":
        return "avoid companies with major controversies (the ESG check runs)"
    return f"nothing from {str(e.get('value') or e.get('type')).replace('_', ' ')}"


def _settings_changes(stored: Optional[Profile], profile: Profile,
                      toggle_changes: list[AgentToggleChange]) -> tuple[list[str], bool]:
    """What this message changed in the saved profile, one plain sentence each, and whether it reshapes the portfolio.
    Used to confirm a settings-only message ("I don't care about the news anymore") without running an analysis."""
    before = stored.model_dump(mode="json") if stored else {}
    after = profile.model_dump(mode="json")
    said: list[str] = []
    for c in toggle_changes:
        name = _ANALYSIS_NAMES.get(c.agent, c.agent)
        said.append(f"{name} is now switched on: AlphaForge may use it when a question needs it." if c.enabled
                    else f"{name} is now switched off: AlphaForge won't use it unless a question can't be answered without it.")
    added = [e for e in after.get("exclusions") or [] if e not in (before.get("exclusions") or [])]
    said += [f"New hard limit: {_limit_words(e)}." for e in added]
    if after.get("risk_band") != before.get("risk_band"):
        said.append(f"Your risk level is now {_RISK_WORDS.get(after['risk_band'], after['risk_band'])}.")
    if after.get("horizon_years") != before.get("horizon_years") and before:
        h = after.get("horizon_years") or {}
        span = f"{h.get('min')} to {h.get('max')} years" if h.get("min") and h.get("max") and h["min"] != h["max"] \
            else f"{h.get('max') or h.get('min')} years" if (h.get("max") or h.get("min")) else "not set"
        said.append(f"Your horizon is now {span}.")
    if after.get("experience") != before.get("experience"):
        said.append(f"Your experience is now {after['experience']}: answers will be written for that level.")
    if after.get("wants_to_learn") != before.get("wants_to_learn") and before:
        said.append("Answers will now explain the ideas behind them." if after.get("wants_to_learn")
                    else "Answers will now go straight to the point.")
    if after.get("capital") != before.get("capital") and after.get("capital"):
        said.append(f"The simulated amount is now ${after['capital']:,.0f}.")
    if after.get("loss_capacity") != before.get("loss_capacity") and after.get("loss_capacity") is not None:
        said.append(f"The largest loss you accept is now {after['loss_capacity']:.0%}.")
    reshapes = any(after.get(k) != before.get(k) for k in _SHAPING)
    return said, reshapes


def _settings_message(said: list[str], reshapes: bool, has_portfolio: bool) -> str:
    if not said:
        return "Nothing to change: your settings already say that. Ask me anything about your portfolio."
    end = ("Your portfolio will be rebuilt to match when you open My portfolio." if reshapes and has_portfolio
           else "Your portfolio stays the same." if has_portfolio else "")
    return " ".join(["Done.", *said, end]).strip()


_OWN_PORTFOLIO = re.compile(r"\bmy\s+(?:current\s+|whole\s+|own\s+)?(?:portfolio|holdings|investments|mix|allocation)\b", re.I)


_POSSESSIVES = {"my", "our", "mon", "ma", "mes", "notre", "nos"}
_FILLERS = {"current", "whole", "own", "entire", "actuel", "actuelle", "actuels", "propre", "propres"}
_OWN_NOUNS = ["portfolio", "holdings", "investments", "allocation", "portefeuille", "placements",
              "investissements", "positions"]


def _says_my_portfolio(text: str) -> bool:
    """"my portfolio" in English or French, typos tolerated ("my portofolio", "mon portefeuile"): a possessive,
    at most one filler word ("current", "actuel"), then a word close to one of _OWN_NOUNS (difflib ratio >= 0.8)."""
    if _OWN_PORTFOLIO.search(text):
        return True
    from difflib import get_close_matches

    words = re.findall(r"[a-zà-ÿ]+", _normalize(text))
    for i, w in enumerate(words):
        if w not in _POSSESSIVES:
            continue
        nxt = words[i + 1:i + 3]
        if nxt and nxt[0] in _FILLERS:
            nxt = nxt[1:]
        if nxt and len(nxt[0]) >= 6 and get_close_matches(nxt[0], _OWN_NOUNS, n=1, cutoff=0.8):
            return True
    return False


def _about_own_portfolio(text: str, task: str, model_says: bool = False) -> bool:
    """The question is about the user's own portfolio: the model read it so (any wording, language, typo), the text
    says so (safety net when the model misses it), or it is a risk / scenario / projection question naming no asset.
    Building a new portfolio ("a portfolio that fits my profile") is not: it starts from the profile, not the old mix."""
    return task != "out_of_scope" and (model_says or _says_my_portfolio(text)
                                       or task in ("risk_review", "scenario_analysis", "projection"))


def _clean_positions(positions: list[Position], warnings: list[str]) -> list[Position]:
    universe = load_universe()
    out: dict[str, Position] = {}
    for p in positions:
        ticker = normalize_ticker(p.ticker, universe)
        if ticker is None:
            warnings.append(f"Asset '{p.ticker}' is not in the data universe: ignored.")
            continue
        out[ticker] = Position(ticker=ticker, weight=p.weight)
    total = sum(p.weight or 0 for p in out.values())
    if total > 1.0 + 1e-9:
        warnings.append(f"The weights given add up to {total:.0%}, more than 100%.")
    return list(out.values())


def _conflicts(profile: Optional[Profile], positions: list[Position], warnings: list[str]) -> None:
    if profile is None:
        return
    universe = load_universe()
    excluded_classes = {e.value for e in profile.exclusions if e.type == "asset_class" and e.value}
    for p in positions:
        asset_class = universe[p.ticker].asset_class
        if asset_class in excluded_classes:
            warnings.append(f"{p.ticker} is in asset class '{asset_class}', which the profile excludes: "
                            "governance will decide.")


def run_intake(
    text: str,
    actor_id: str,
    *,
    profile_form: Optional[dict] = None,
    portfolio_ref: Optional[dict] = None,
    as_of: Optional[date] = None,
    backend: Optional[IntakeBackend] = None,
    store: Optional[ProfileStore] = None,
) -> IntakeResult:
    backend = backend or get_backend()
    # A request just reached the intaker: warm the market path (cache sync + Neptune wake via one
    # probe invoke) on a daemon thread while the intake LLM call runs, so the sub-agents' first
    # tool calls land warm. Idempotent per process; never raises.
    try:
        from app.tools.market_store import kick_off_market_warmup

        kick_off_market_warmup()
    except Exception:  # noqa: BLE001  packaging without app.tools: warm-up is optional
        pass
    stored = store.get(actor_id) if store else None
    known = _known_profile(stored, profile_form)

    try:
        draft = _extract_with_retry(backend, text, known)
    except IntakeRefused as refused:
        return IntakeResult(status="out_of_scope", warnings=[str(refused)], backend=backend.name)

    warnings: list[str] = []

    # --- Profile --------------------------------------------------------------------------------
    toggle_changes = _verified_toggle_changes(text, draft.profile.agent_toggle_changes, warnings)
    merged = _merge_profile(stored, draft, profile_form, toggle_changes)
    missing = [f for f in REQUIRED_PROFILE_FIELDS if not merged.get(f)]
    profile: Optional[Profile] = None
    if not missing:
        try:
            profile = Profile(actor_id=actor_id, **merged)
        except ValidationError as e:
            missing = sorted({str(err["loc"][0]) for err in e.errors()})

    # --- Request --------------------------------------------------------------------------------
    r = draft.request
    positions = _clean_positions(r.positions, warnings)
    used_saved = False
    if not positions and profile is not None and profile.portfolio and _about_own_portfolio(text, r.task, r.about_own_portfolio):
        used_saved = True
        # "How exposed is my portfolio to a rate hike?": the analysis runs on the portfolio saved in the profile.
        positions = [Position(ticker=t, weight=w) for t, w in sorted(profile.portfolio.items(), key=lambda kv: -kv[1])]
        warnings.append("Analysed on your saved portfolio: "
                        + ", ".join(f"{p.ticker} {p.weight:.0%}" for p in positions) + ".")
    _conflicts(profile, positions, warnings)

    analysis_date = as_of or r.as_of or DATA_END
    if analysis_date > DATA_END:
        warnings.append(f"Analysis date {analysis_date} is after the data ends: moved back to {DATA_END}.")
        analysis_date = DATA_END

    request = Request(
        request_id=f"req-{uuid.uuid4().hex[:8]}",
        actor_id=actor_id,
        task=r.task,
        portfolio_ref=PortfolioRef(**portfolio_ref) if portfolio_ref else None,
        user_message=text,
        positions=positions,
        capital=r.capital,
        parameters=Parameters(benchmark=r.benchmark),
        toggles=Toggles(include_rag=r.include_rag),
        as_of=analysis_date,
        intake_confidence=max(0.0, min(1.0, r.intake_confidence)),
    )

    # --- Status ---------------------------------------------------------------------------------
    question = None
    message = None
    if r.settings_only and stored is not None and profile is not None and request.task != "out_of_scope":
        # "I don't care about the news anymore": a setting, not a question. Confirm it; run no analysis (an answer
        # about something else would read as unrelated). Only an update: a first message describing oneself is
        # still asked what to analyse.
        status = "profile_updated"
        said, reshapes = _settings_changes(stored, profile, toggle_changes)
        message = _settings_message(said, reshapes, bool(profile.portfolio))
    elif request.task == "out_of_scope":
        status = "out_of_scope"
    elif missing:
        status = "needs_clarification"
        question = QUESTIONS.get(missing[0]) or draft.clarification_question
    elif request.intake_confidence < MIN_CONFIDENCE and not used_saved:
        # (with the saved portfolio, "which holdings?" is already answered: never ask it back)
        status = "needs_clarification"
        question = draft.clarification_question or QUESTIONS["task"]
    else:
        status = "ready"

    if profile is not None and store is not None:
        store.put(profile)

    return IntakeResult(
        status=status,
        profile=profile,
        request=request,
        clarification_question=question,
        message=message,
        missing_fields=missing,
        toggle_changes=toggle_changes if profile is not None else [],  # only reported once saved in a profile
        warnings=warnings,
        backend=backend.name,
    )
