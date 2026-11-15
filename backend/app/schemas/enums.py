"""Allowed values for the Request JSON (the Intake model must choose from these lists).
Sources:
- TASKS: one task per sub-agent in architecture.mmd, plus `out_of_scope`.
- (the dataset covers 2021-11-12 to 2026-11-12,
  so there is no 2008 crash).
- BENCHMARKS: broad-index ETFs from `data/curated/assets_universe.csv`.
"""
from typing import Literal, get_args

Task = Literal[
    "risk_review",          # Risk agent
    "scenario_analysis",    # Macro and scenarios agent
    "allocation_proposal",  # Allocation agent
    "esg_screen",           # ESG agent
    "fundamentals_review",  # Fundamentals agent
    "news_review",          # News agent
    "out_of_scope",         # request outside the simulation (real trade, personalized advice...)
]
Benchmark = Literal["SPY", "XIU.TO", "QQQ", "XBB.TO", "AGG"]

TASKS = get_args(Task)
BENCHMARKS = get_args(Benchmark)
