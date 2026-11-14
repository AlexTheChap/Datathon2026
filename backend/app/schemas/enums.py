"""Valeurs permises pour le Request JSON (le modèle de l'Intake doit choisir dans ces listes).

Sources :
- TASKS : une tâche par sous-agent de architecture.mmd, plus `out_of_scope`.
- SCENARIOS : clés de `tools/risk_tools.py::STRESS_WINDOWS` (le jeu de données couvre 2021-11-12 à 2026-11-12,
  donc pas de krach de 2008).
- BENCHMARKS : FNB d'indices larges de `data/curated/assets_universe.csv`.
"""
from typing import Literal, get_args

Task = Literal[
    "risk_review",          # agent Risque
    "scenario_analysis",    # agent Macro et scénarios
    "allocation_proposal",  # agent Allocation
    "esg_screen",           # agent ESG
    "fundamentals_review",  # agent Fondamentaux
    "news_review",          # agent Nouvelles
    "out_of_scope",         # demande hors simulation (transaction réelle, conseil personnalisé...)
]

Scenario = Literal["2022", "2022_H1"]

Benchmark = Literal["SPY", "XIU.TO", "QQQ", "XBB.TO", "AGG"]

TASKS = get_args(Task)
SCENARIOS = get_args(Scenario)
BENCHMARKS = get_args(Benchmark)
