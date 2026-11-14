"""Outils quantitatifs exposés à l'agent (L12 : Gateway MCP en prod, appel direct ici).

Adaptateur mince autour de risk_tools.py et governance_check.py : les fonctions
restent déterministes et renvoient leur PREUVE (evidence_id, say, hypothèses) —
le LLM ne calcule jamais, il cite les preuves. Le profil investisseur transit
par une ContextVar (une par exécution), car un outil Strands est sans état.
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from strands import tool

from app.agents.governance import governance_check as _gov  # alias : le @tool ci-dessous s'appelle governance_check
from app.core.config import get_settings
from app.tools.risk_tools import RiskTools

# Profil validé par l'Intake, posé par l'orchestrateur au début de l'exécution.
current_profile: ContextVar[dict[str, Any] | None] = ContextVar("af_current_profile", default=None)

_rt: RiskTools | None = None


def _tools() -> RiskTools:
    global _rt
    if _rt is None:
        s = get_settings()
        _rt = RiskTools(s.abs_path(s.curated_dir))
    return _rt


def _guard(fn, *args, **kwargs):
    """Erreur d'entrée (ticker inconnu, fenêtre vide…) -> dict d'erreur pour que le modèle réagisse."""
    try:
        return fn(*args, **kwargs)
    except (KeyError, ValueError) as e:
        return {"erreur": str(e), "note": "Corrige les arguments et rappelle l'outil."}


@tool
def metrics(tickers: list[str], start: str | None = None, end: str | None = None,
            as_of: str | None = None) -> dict:
    """Rendement total, volatilité annualisée, pire baisse (max drawdown), VaR/CVaR 95 %
    quotidiennes par actif, en CAD, preuve (evidence_id) à l'appui.

    Args:
        tickers: Actifs de l'univers (ex. ["NVDA", "XBB.TO"]).
        start/end: Fenêtre optionnelle "YYYY-MM-DD".
        as_of: Date de référence point-in-time (rien après cette date).
    """
    return _guard(_tools().metrics, tickers, start=start, end=end, as_of=as_of)


@tool
def stress(weights: dict[str, float], window: str = "2022", capital: float | None = None) -> dict:
    """Rejeu historique d'une allocation sur une fenêtre de crise (2022 par défaut :
    actions et obligations en baisse ensemble). Renvoie perte totale, pire moment
    (max drawdown) et, si `capital` est donné, les montants en dollars CAD.

    Args:
        weights: Pondérations, ex. {"XIU.TO": 0.3, "NVDA": 0.2} (normalisées au besoin).
        window: "2022" ou "2022_H1".
        capital: Capital fictif en CAD (optionnel).
    """
    return _guard(_tools().stress, weights, window=window, capital=capital)


@tool
def concentration(weights: dict[str, float], look_through: bool = True) -> dict:
    """Expositions par société, secteur, classe d'actifs et devise, avec indice de
    concentration (HHI) et nombre effectif de positions. Décompose les FNB d'actions
    (SPY, QQQ, XIU…) via leurs avoirs publiés.

    Args:
        weights: Pondérations du portefeuille.
        look_through: True (défaut) pour décomposer les FNB d'actions.
    """
    return _guard(_tools().concentration, weights, look_through=look_through)


@tool
def correlation(tickers: list[str], start: str | None = None, end: str | None = None,
                as_of: str | None = None) -> dict:
    """Matrice de corrélation des rendements quotidiens en CAD entre actifs.

    Args:
        tickers: Au moins deux actifs de l'univers.
        start/end/as_of: Fenêtre optionnelle (comparer avec 2022 est instructif).
    """
    return _guard(_tools().correlation, tickers, start=start, end=end, as_of=as_of)


@tool
def risk_contribution(weights: dict[str, float], start: str | None = None,
                      end: str | None = None, as_of: str | None = None) -> dict:
    """Part du risque total du portefeuille générée par chaque position et ratio de
    diversification (identifie qui « pèse » dans le risque au-delà du poids).

    Args:
        weights: Pondérations du portefeuille.
        start/end/as_of: Fenêtre optionnelle.
    """
    return _guard(_tools().risk_contribution, weights, start=start, end=end, as_of=as_of)


@tool
def max_position(base_weights: dict[str, float], asset: str, max_drawdown_limit: float,
                 window: str = "2022") -> dict:
    """Plus grande part de `asset` (autres positions réduites au prorata) qui respecte
    la perte maximale tolérée `max_drawdown_limit` dans le scénario de crise `window`.
    Outil clé pour répondre « jusqu'à combien ? » en restant dans le profil.

    Args:
        base_weights: Portefeuille actuel (sans la part à tester).
        asset: Ticker à tester, ex. "NVDA".
        max_drawdown_limit: Perte maximale tolérée, ex. 0.15 pour -15 %.
        window: "2022" par défaut.
    """
    return _guard(_tools().max_position, base_weights, asset, max_drawdown_limit, window=window)


@tool
def governance_check(weights: dict[str, float]) -> dict:
    """Vérifie une allocation contre le profil de l'investisseur et les règles
    documentées (rules.yaml) : limites de perte en scénario de crise, position
    unique, secteur, crypto, horizon, exclusions (ESG, controverses).
    Renvoie un verdict global PASS / WARN / BLOCK avec chaque contrôle détaillé.

    Args:
        weights: Pondérations à vérifier (le profil de l'exécution en cours est utilisé).
    """
    profile = current_profile.get()
    if profile is None:
        return {"erreur": "Aucun profil actif pour cette exécution."}
    s = get_settings()
    return _guard(_gov.check, weights, profile,
                  s.abs_path(s.rules_path), _tools(),
                  str(s.abs_path(s.curated_dir).parent))


ALL_TOOLS = [metrics, stress, concentration, correlation, risk_contribution, max_position, governance_check]
