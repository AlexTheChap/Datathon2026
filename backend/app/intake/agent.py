"""Intake agent (boîte ③) : texte libre → Profile JSON + Request JSON validés.

Étapes de `run_intake` :
1. lire le profil déjà enregistré (store) et le formulaire de profil ;
2. demander un brouillon au backend (Bedrock Haiku ou mock), réessayer une fois si la sortie est invalide ;
3. fusionner : profil enregistré < texte libre < formulaire (le formulaire, explicite, gagne toujours) ;
4. ajouter les champs serveur (actor_id, request_id, as_of) et valider avec Pydantic ;
5. contrôles : tickers hors univers, poids > 100 %, conflit avec une exclusion, date après les données ;
6. décider le statut : ready, needs_clarification (une question au plus) ou out_of_scope ;
7. enregistrer le profil s'il est complet.

L'Intake n'analyse rien et ne bloque rien : la gouvernance (boîte ⑤) tranche. Il signale seulement.
"""
from __future__ import annotations

import uuid
from datetime import date
from typing import Any, Optional

from pydantic import ValidationError

from app.schemas import Parameters, PortfolioRef, Position, Profile, Request, Toggles
from app.store.profiles import ProfileStore

from .backends import IntakeBackend, IntakeRefused, get_backend
from .schemas import IntakeDraft, IntakeResult
from .universe import load_universe, normalize_ticker

DATA_END = date(2026, 11, 12)  # dernière date du jeu de données (voir schemas/enums.py)
MIN_CONFIDENCE = 0.6
REQUIRED_PROFILE_FIELDS = ("risk_band", "experience", "language")

QUESTIONS = {
    "risk_band": {
        "fr": "Quel niveau de risque acceptez-vous : faible, faible à modéré, modéré ou élevé ?",
        "en": "What level of risk are you comfortable with: low, low-to-moderate, moderate or high?",
    },
    "experience": {
        "fr": "Comment décririez-vous votre expérience en placement : débutante, intermédiaire ou avancée ?",
        "en": "How would you describe your investing experience: beginner, intermediate or advanced?",
    },
    "language": {
        "fr": "Préférez-vous que je vous réponde en français ou en anglais ?",
        "en": "Would you like answers in French or in English?",
    },
    "task": {
        "fr": "Que voulez-vous que j'analyse : le risque de votre portefeuille, un scénario de crise, une allocation, un filtre ESG, les fondamentaux ou les nouvelles ?",
        "en": "What would you like me to analyse: portfolio risk, a crisis scenario, an allocation, an ESG screen, fundamentals or news?",
    },
}


def _question(key: str, lang: str) -> Optional[str]:
    q = QUESTIONS.get(key, {})
    return q.get(lang) or q.get("en")


class IntakeError(RuntimeError):
    """Le backend n'a pas produit de brouillon valide, même après un nouvel essai."""


def _extract_with_retry(backend: IntakeBackend, text: str, known: dict) -> IntakeDraft:
    try:
        return backend.extract(text, known)
    except (ValidationError, ValueError) as first_error:
        try:
            return backend.extract(text, known, feedback=str(first_error))
        except (ValidationError, ValueError) as second_error:
            raise IntakeError(f"Sortie invalide après 2 essais : {second_error}") from second_error


def _dedupe(items: list) -> list:
    out = []
    for item in items:
        if item not in out:
            out.append(item)
    return out


def _known_profile(stored: Optional[Profile], form: Optional[dict]) -> dict[str, Any]:
    """Ce qu'on sait déjà avant de lire le texte (envoyé au modèle comme contexte)."""
    known: dict[str, Any] = stored.model_dump(mode="json", exclude={"actor_id", "schema_version"}) if stored else {}
    known.update({k: v for k, v in (form or {}).items() if v is not None})
    return known


def _merge_profile(stored: Optional[Profile], draft: IntakeDraft, form: Optional[dict]) -> dict[str, Any]:
    merged: dict[str, Any] = stored.model_dump(exclude={"actor_id", "schema_version"}) if stored else {}
    d = draft.profile

    for field in ("risk_band", "experience", "language", "objective", "loss_capacity", "capital"):
        value = getattr(d, field)
        if value is not None:
            merged[field] = value
    if d.horizon_min_years is not None or d.horizon_max_years is not None:
        merged["horizon_years"] = {"min": d.horizon_min_years, "max": d.horizon_max_years}
    # Les listes s'accumulent : le texte peut ajouter une exclusion, pas en retirer une en silence.
    for field in ("preferences", "constraints"):
        merged[field] = _dedupe(list(merged.get(field, [])) + list(getattr(d, field)))
    merged["exclusions"] = _dedupe(list(merged.get("exclusions", [])) + [e.model_dump() for e in d.exclusions])

    for key, value in (form or {}).items():  # formulaire : explicite, il gagne
        if value is not None:
            merged[key] = value
    return merged


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
        if universe[p.ticker].asset_class in excluded_classes:
            warnings.append(
                f"{p.ticker} est de classe « {universe[p.ticker].asset_class} », exclue par le profil : "
                "la gouvernance tranchera."
            )


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
    stored = store.get(actor_id) if store else None
    known = _known_profile(stored, profile_form)

    try:
        draft = _extract_with_retry(backend, text, known)
    except IntakeRefused as refused:
        return IntakeResult(status="out_of_scope", warnings=[str(refused)], backend=backend.name)

    warnings: list[str] = []

    # --- Profil ---------------------------------------------------------------------------------
    merged = _merge_profile(stored, draft, profile_form)
    lang = merged.get("language") or "en"
    missing = [f for f in REQUIRED_PROFILE_FIELDS if not merged.get(f)]
    profile: Optional[Profile] = None
    if not missing:
        try:
            profile = Profile(actor_id=actor_id, **merged)
        except ValidationError as e:
            missing = sorted({str(err["loc"][0]) for err in e.errors()})

    # --- Demande --------------------------------------------------------------------------------
    r = draft.request
    positions = _clean_positions(r.positions, warnings)
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
        parameters=Parameters(scenarios=r.scenarios, benchmark=r.benchmark),
        toggles=Toggles(stress_tests=r.stress_tests, include_rag=r.include_rag),
        as_of=analysis_date,
        intake_confidence=max(0.0, min(1.0, r.intake_confidence)),
    )

    # --- Statut ---------------------------------------------------------------------------------
    question = None
    if request.task == "out_of_scope":
        status = "out_of_scope"
    elif missing:
        status = "needs_clarification"
        question = _question(missing[0], lang) or draft.clarification_question
    elif request.intake_confidence < MIN_CONFIDENCE:
        status = "needs_clarification"
        question = draft.clarification_question or _question("task", lang)
    else:
        status = "ready"

    if profile is not None and store is not None:
        store.put(profile)

    return IntakeResult(
        status=status,
        profile=profile,
        request=request,
        clarification_question=question,
        missing_fields=missing,
        warnings=warnings,
        backend=backend.name,
    )
