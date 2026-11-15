"""Suitability report (PDF): the portfolio, the profile it is judged against, the fit score with every point lost,
every governance check, the risk measures and how the portfolio was built. Deterministic, no LLM.

    pdf_bytes = suitability_pdf(profile, weights, fit, universe, as_of, run=None, generated_at=None)

Every number comes from `fit` (fit_score.py, computed at download time on the same rules and data as the app); the
report only lays it out. A report id (sha256 of the content's numbers) lets anyone check two copies are the same.
Core PDF fonts are Latin-1: text goes through `_t`, which writes the few other characters used here in plain ASCII.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional

from fpdf import FPDF

from app.agents.governance.fit_score import RISK_WORDS

CHECK_NAMES = {
    "SINGLE_POSITION": "Largest single company", "SECTOR": "Largest sector", "CRYPTO": "Crypto weight",
    "SHORT_HORIZON_VOLATILITY": "High-volatility weight (short horizon)",
    "SHORT_HORIZON_DEFENSIVE": "Defensive weight (short horizon)", "EXCLUSION_CONTROVERSY": "Major controversies excluded",
    "ESG_INDEX_PREFERENCE": "ESG index preference",
}
CLASS_WORDS = {"equity": "Stock", "etf_equity": "Equity fund", "etf_bond": "Bond fund", "etf_commodity": "Gold fund",
               "crypto": "Crypto"}
ANALYSES = {"risk": "Portfolio risk", "news": "News", "esg": "ESG screening", "fundamentals": "Company fundamentals",
            "macro_scenarios": "Macro and rates", "allocation": "Allocation proposal"}
_ASCII = {"–": "-", "—": "-", "→": "->", "‘": "'", "’": "'", "“": '"', "”": '"',
          "≥": ">=", "≤": "<=", "…": "...", " ": " ", "−": "-", "·": "-", "≈": "~",
          "Σ": "Sum ", "√": "sqrt "}
INK, MUTED, RULE, ACCENT = (33, 37, 41), (96, 102, 110), (214, 218, 222), (24, 92, 160)
TRACK = (236, 238, 240)
# The dashboard's light-mode chart colors (app.css --viz-1..5, validated for colour-blind separation); every bar also
# carries its value in text, and the tables stay as the table view (three slots are under 3:1 against white).
VIZ = [(42, 120, 214), (235, 104, 52), (27, 175, 122), (237, 161, 0), (232, 123, 164)]
ON_FILL = {0: (255, 255, 255), 1: (255, 255, 255), 2: (255, 255, 255), 3: (11, 11, 11), 4: (11, 11, 11)}
CLASS_GROUPS = [("Stocks", ("equity", "etf_equity")), ("Bonds", ("etf_bond",)), ("Gold", ("etf_commodity",)),
                ("Crypto", ("crypto",))]
STATUS_COLOR = {"PASS": (31, 122, 69), "WARN": (176, 112, 0), "UNVERIFIED": (96, 102, 110), "BLOCK": (179, 38, 30)}


def _t(text: Any) -> str:
    s = str(text if text is not None else "")
    for k, v in _ASCII.items():
        s = s.replace(k, v)
    return s.encode("latin-1", "replace").decode("latin-1")


def _pct(x: Optional[float], d: int = 1) -> str:
    return "-" if x is None else f"{x * 100:.{d}f}%".replace(".0%", "%")


def _money(x: Optional[float]) -> str:
    return "-" if x is None else f"${x:,.0f}"


def _horizon(h: dict) -> str:
    lo, hi = (h or {}).get("min"), (h or {}).get("max")
    if lo and hi and lo != hi:
        return f"{lo} to {hi} years"
    if lo or hi:
        return f"{lo or hi} years" + (" or more" if lo and not hi else "")
    return "not set"


def _limit_words(e: dict) -> str:
    if e.get("type") == "asset_class":
        return f"no {str(e.get('value')).replace('_', ' ')}"
    if e.get("type") == "controversy":
        return f"no company with a {e.get('min_severity') or 'major'} controversy"
    return f"no {str(e.get('value') or e.get('type')).replace('_', ' ')}"


def _check_value(v: Any, limit: Any) -> tuple[str, str]:
    fmt = lambda x: _pct(x) if isinstance(x, (int, float)) else (", ".join(map(str, x)) or "none") if isinstance(x, list) else ("-" if x is None else str(x))  # noqa: E731
    return fmt(v), fmt(limit)


def chart_data(weights: dict, rt, max_sector: float) -> dict:
    """What the charts draw, measured with the same tools as the app: share by asset class, risk contribution
    (share of the money vs share of the ups and downs), and stock sectors with equity funds looked through."""
    uni = rt.universe
    split = []
    for label, classes in CLASS_GROUPS:
        v = sum(w for t, w in weights.items() if t in uni.index and uni.loc[t, "asset_class"] in classes)
        if v > 0.0005:
            split.append((label, v))
    rc = rt.risk_contribution(weights)["output"]
    sectors: dict = {}
    lt = rt.lookthrough(weights) if getattr(rt, "holdings", None) else None
    if lt is not None:
        eq = lt[lt["asset_class"] == "equity"]
        sectors = {str(k): float(v) for k, v in eq.groupby("sector")["weight"].sum().sort_values(ascending=False).items()}
    return {"split": split, "risk": {t: (rc["weights"].get(t, 0), rc["risk_contribution"].get(t, 0)) for t in weights},
            "sectors": sectors, "max_sector": max_sector}


def report_id(profile: dict, weights: dict, fit: dict) -> str:
    core = {"weights": weights, "score": fit["score"], "parts": [(p["key"], p["points"]) for p in fit["parts"]],
            "profile": {k: profile.get(k) for k in ("risk_band", "horizon_years", "loss_capacity", "exclusions")},
            "period": fit.get("period"), "rules": fit.get("rules_version"), "fit": fit.get("fit_version")}
    return hashlib.sha256(json.dumps(core, sort_keys=True, default=str).encode()).hexdigest()[:16]


class _Doc(FPDF):
    def __init__(self, rid: str, stamp: str):
        super().__init__(format="Letter")
        self.rid, self.stamp = rid, stamp
        self.set_margins(18, 18, 18)
        self.set_auto_page_break(True, margin=18)
        self.set_title("AlphaForge suitability report")
        self.set_author("AlphaForge (simulation)")

    def footer(self):
        self.set_y(-12)
        self.set_font("Helvetica", "", 7.5)
        self.set_text_color(*MUTED)
        self.cell(0, 4, _t(f"AlphaForge suitability report - {self.stamp} - report {self.rid} - simulation, not advice"),
                  align="L")
        self.cell(0, 4, f"Page {self.page_no()}/{{nb}}", align="R")

    # ----- building blocks -----
    def h1(self, text):
        self.set_font("Helvetica", "B", 18); self.set_text_color(*INK)
        self.multi_cell(0, 8, _t(text), new_x="LMARGIN", new_y="NEXT")

    def h2(self, text):
        if self.get_y() > self.h - 50:
            self.add_page()
        self.ln(4)
        self.set_font("Helvetica", "B", 12); self.set_text_color(*INK)
        self.cell(0, 7, _t(text), new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(*RULE); self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(2)

    def para(self, text, size=9.5, color=INK, style=""):
        self.set_font("Helvetica", style, size); self.set_text_color(*color)
        self.multi_cell(0, size * 0.5, _t(text), new_x="LMARGIN", new_y="NEXT")
        self.ln(1)

    def facts(self, rows: list[tuple[str, str]]):
        for k, v in rows:
            self.set_font("Helvetica", "", 9); self.set_text_color(*MUTED)
            self.cell(52, 5.2, _t(k))
            self.set_font("Helvetica", "B", 9); self.set_text_color(*INK)
            self.multi_cell(0, 5.2, _t(v), new_x="LMARGIN", new_y="NEXT")

    # ----- charts (vector, drawn with the PDF's own shapes) -----
    def chart_title(self, title: str, note: str = "", need: float = 30):
        """A chart's title and note, kept on the same page as the whole chart (`need`: the chart's height, in mm)."""
        self.room(12 + need)
        self.set_font("Helvetica", "B", 9.5); self.set_text_color(*INK)
        self.cell(0, 5, _t(title), new_x="LMARGIN", new_y="NEXT")
        if note:
            self.set_font("Helvetica", "", 8); self.set_text_color(*MUTED)
            self.cell(0, 4, _t(note), new_x="LMARGIN", new_y="NEXT")
        self.ln(1.5)

    def room(self, height: float):
        if self.get_y() + height > self.h - 20:
            self.add_page()

    def _bar(self, x, y, w, h, color):
        if w <= 0.05:
            return
        self.set_fill_color(*color)
        self.rect(x, y, w, h, style="F", round_corners=w > 1.6, corner_radius=min(0.8, h / 2))

    def bars(self, rows: list[tuple[str, float, str]], scale: float, color=VIZ[0], label_w=46.0,
             ref: Optional[tuple[str, float]] = None, row_h=6.0, bar_h=3.2):
        """Ranked horizontal bars, one series: the label on the left, the value written after the bar."""
        area = self.w - self.l_margin - self.r_margin - label_w - 22
        self.room(len(rows) * row_h + 6)
        top = self.get_y()
        for label, value, shown in rows:
            y = self.get_y()
            self.set_font("Helvetica", "", 8.5); self.set_text_color(*INK)
            self.set_xy(self.l_margin, y)
            self.cell(label_w - 2, row_h, _t(label)[:34])
            x0 = self.l_margin + label_w
            self.set_fill_color(*TRACK); self.rect(x0, y + (row_h - bar_h) / 2, area, bar_h, style="F")
            self._bar(x0, y + (row_h - bar_h) / 2, area * min(1.0, value / scale) if scale else 0, bar_h, color)
            self.set_xy(x0 + area + 2, y); self.set_font("Helvetica", "B", 8.5)
            self.cell(20, row_h, _t(shown))
            self.set_y(y + row_h)
        if ref is not None:  # a dashed limit line across the rows, labelled under them
            x = self.l_margin + label_w + area * min(1.0, ref[1] / scale)
            self.set_draw_color(*INK); self.set_line_width(0.3); self.set_dash_pattern(dash=1, gap=1)
            self.line(x, top, x, self.get_y())
            self.set_dash_pattern(); self.set_line_width(0.2)
            self.set_font("Helvetica", "", 7.5); self.set_text_color(*MUTED)
            self.set_xy(x - 20, self.get_y()); self.cell(40, 4, _t(ref[0]), align="C")
            self.ln(4)
        self.ln(2)

    def paired(self, rows: list[tuple[str, float, float]], names: tuple[str, str], label_w=46.0):
        """Two thin bars per row (share of the money, share of the ups and downs), with a legend; values written."""
        area = self.w - self.l_margin - self.r_margin - label_w - 22
        row_h, bar_h, gap = 9.0, 2.8, 0.6
        self.legend([(names[0], VIZ[0]), (names[1], VIZ[1])])
        self.room(len(rows) * row_h + 4)
        scale = max([max(a, b) for _, a, b in rows] + [0.01])
        for label, a, b in rows:
            y = self.get_y()
            self.set_font("Helvetica", "", 8.5); self.set_text_color(*INK)
            self.set_xy(self.l_margin, y); self.cell(label_w - 2, row_h, _t(label)[:34])
            x0 = self.l_margin + label_w
            for k, (v, color) in enumerate(((a, VIZ[0]), (b, VIZ[1]))):
                yy = y + 1.2 + k * (bar_h + gap)
                self._bar(x0, yy, area * v / scale, bar_h, color)
                self.set_font("Helvetica", "B" if k else "", 7.5); self.set_text_color(*INK)
                self.set_xy(x0 + area * v / scale + 1.5, yy - 0.6); self.cell(18, bar_h + 1, _pct(v, 0))
            self.set_y(y + row_h)
        self.ln(2)

    def legend(self, items: list[tuple[str, tuple]]):
        self.set_font("Helvetica", "", 8); x = self.l_margin; y = self.get_y()
        for name, color in items:
            self.set_fill_color(*color); self.rect(x, y + 1.2, 3, 3, style="F")
            self.set_text_color(*INK); self.set_xy(x + 4.2, y); w = self.get_string_width(_t(name)) + 8
            self.cell(w, 5.4, _t(name)); x += w + 4
        self.set_y(y + 6.5)

    def segmented(self, parts: list[tuple[str, float]]):
        """One bar split by asset class, a 0.6 mm paper gap between parts; a legend names every part with its share."""
        self.room(20)
        total_w = self.w - self.l_margin - self.r_margin
        x, y, h = self.l_margin, self.get_y(), 8.0
        for i, (label, v) in enumerate(parts):
            w = total_w * v - (0.6 if i < len(parts) - 1 else 0)
            self._bar(x, y, w, h, VIZ[i % len(VIZ)])
            if w > 22:
                self.set_font("Helvetica", "B", 8); self.set_text_color(*ON_FILL[i % len(VIZ)])
                self.set_xy(x + 1.5, y); self.cell(w - 3, h, _t(f"{label} {_pct(v, 0)}"))
            x += total_w * v
        self.set_y(y + h + 2)
        self.legend([(f"{label} {_pct(v, 0)}", VIZ[i % len(VIZ)]) for i, (label, v) in enumerate(parts)])

    def meter(self, score: float, band: str):
        total_w = self.w - self.l_margin - self.r_margin
        y = self.get_y()
        color = (31, 122, 69) if score >= 85 else (176, 112, 0) if score >= 70 else (179, 38, 30)
        self.set_fill_color(*TRACK); self.rect(self.l_margin, y, total_w, 4, style="F", round_corners=True, corner_radius=2)
        self._bar(self.l_margin, y, total_w * score / 100, 4, color)
        self.set_y(y + 5.5)
        self.set_font("Helvetica", "", 8); self.set_text_color(*MUTED)
        self.cell(0, 4, _t(f"{score}% - {band}. 85% and up: good fit; 70-84%: acceptable; 50-69%: weak; under 50%: poor."),
                  new_x="LMARGIN", new_y="NEXT")
        self.ln(2)

    def table(self, head: list[str], rows: list[list[str]], widths: list[float], align: str, status_col: int = -1):
        self.set_font("Helvetica", "B", 8.5); self.set_text_color(*MUTED)
        for h, w, a in zip(head, widths, align):
            self.cell(w, 6, _t(h), align="R" if a == "R" else "L")
        self.ln(6)
        self.set_draw_color(*RULE)
        for row in rows:
            self.set_font("Helvetica", "", 8.5)
            # the row's height: the tallest wrapped cell
            lines = max(len(self.multi_cell(w, 4.6, _t(c), dry_run=True, output="LINES")) for c, w in zip(row, widths))
            hgt = max(6, lines * 4.6 + 1.4)
            if self.get_y() + hgt > self.h - 20:
                self.add_page()
            y, x = self.get_y(), self.l_margin
            self.line(self.l_margin, y, self.w - self.r_margin, y)
            for i, (c, w, a) in enumerate(zip(row, widths, align)):
                self.set_xy(x, y + 0.7)
                color = STATUS_COLOR.get(c) if i == status_col else None
                self.set_text_color(*(color or INK))
                self.set_font("Helvetica", "B" if color else "", 8.5)
                self.multi_cell(w, 4.6, _t(c), align="R" if a == "R" else "L")
                x += w
            self.set_y(y + hgt)
        self.line(self.l_margin, self.get_y(), self.w - self.r_margin, self.get_y())
        self.ln(2)


def suitability_pdf(profile: dict, weights: dict, fit: dict, universe, as_of: str, run: Optional[dict] = None,
                    generated_at: Optional[datetime] = None, label: str = "Your portfolio",
                    charts: Optional[dict] = None) -> bytes:
    now = generated_at or datetime.now(timezone.utc)
    stamp = now.strftime("%Y-%m-%d %H:%M UTC")
    rid = report_id(profile, weights, fit)
    doc = _Doc(rid, stamp)
    doc.alias_nb_pages()
    doc.add_page()
    name = lambda t: str(universe.loc[t, "name"]) if t in universe.index else t  # noqa: E731
    cls = lambda t: CLASS_WORDS.get(str(universe.loc[t, "asset_class"]), "-") if t in universe.index else "-"  # noqa: E731

    # ----- Title and verdict -----
    doc.set_font("Helvetica", "B", 9); doc.set_text_color(*ACCENT)
    doc.cell(0, 5, "ALPHAFORGE  -  SUITABILITY REPORT", new_x="LMARGIN", new_y="NEXT")
    doc.h1(f"{label}: {fit['score']}% fit with the investor's profile")
    doc.meter(fit["score"], fit["band"])
    doc.para(f"Governance verdict: {fit['verdict']}. Generated {stamp}; market data up to {as_of} "
             f"(period {fit['period'].replace('/', ' to ')}). Report {rid}.", 9, MUTED)
    doc.set_fill_color(244, 246, 248)
    doc.set_font("Helvetica", "B", 9); doc.set_text_color(*INK)
    doc.multi_cell(0, 5, _t("Simulation only. This report is not investment advice and places no trade. Past returns "
                            "do not predict future returns."), fill=True, new_x="LMARGIN", new_y="NEXT")
    for c in fit.get("caps") or []:
        doc.ln(1); doc.para(f"Score capped at {c['at']}%: {c['why']}.", 9.5, STATUS_COLOR["BLOCK"], "B")

    # ----- Profile -----
    doc.h2("1. The investor's profile")
    band = profile.get("risk_band")
    toggles = profile.get("agent_toggles") or {}
    off = [ANALYSES[k] for k, v in toggles.items() if v is False and k in ANALYSES]
    loss = profile.get("loss_capacity")
    loss_item = next(i for p in fit["parts"] if p["key"] == "loss" for i in p["items"])
    doc.facts([
        ("Risk level", RISK_WORDS.get(band, band or "-")),
        ("Horizon", _horizon(profile.get("horizon_years"))),
        ("Experience", str(profile.get("experience") or "-")),
        ("Largest loss accepted", _pct(loss, 0) if loss is not None
         else f"not given (the usual {_pct(-loss_item['target'], 0)} for this risk level is used)"),
        ("Simulated amount", _money(profile.get("capital")) + (" (CAD, not real money)" if profile.get("capital") else "")),
        ("Hard limits", "; ".join(_limit_words(e) for e in profile.get("exclusions") or []) or "none"),
        ("Preferences", "; ".join(profile.get("preferences") or []) or "none stated"),
        ("Analyses switched off", ", ".join(off) or "none"),
    ])

    # ----- Portfolio -----
    doc.h2("2. The portfolio")
    cap = profile.get("capital")
    rows = [[t, name(t), cls(t), _pct(w), _money(w * cap) if cap else "-"]
            for t, w in sorted(weights.items(), key=lambda kv: -kv[1])]
    if charts and charts.get("split"):
        doc.chart_title("How the money is split", "Share of the money, by kind of asset", need=20)
        doc.segmented(charts["split"])
    ranked = sorted(weights.items(), key=lambda kv: -kv[1])
    doc.chart_title("Each holding", "Share of the money" + (", and the simulated amount" if cap else ""), need=len(ranked) * 6 + 2)
    doc.bars([(t, w, _pct(w) + (f"  {_money(w * cap)}" if cap else "")) for t, w in ranked], ranked[0][1])
    doc.table(["Ticker", "Name", "Kind", "Share", "Amount"], rows, [24, 76, 30, 22, 28], "LLLRR")

    # ----- Fit score -----
    doc.h2(f"3. Fit with the profile: {fit['score']} / 100")
    doc.para("Each part measures one side of the profile. Points are lost only for a stated reason; the thresholds are "
             "the team's documented assumptions (rules.yaml, fit_score version "
             f"{fit.get('fit_version')}), not regulatory standards.", 8.5, MUTED)
    doc.chart_title("Points kept in each part", "Out of each part's maximum; a shorter bar means points lost, for the reason below",
                    need=len(fit["parts"]) * 6 + 2)
    doc.bars([(p["title"], p["points"] / p["max"], f"{p['points']:g} / {p['max']}") for p in fit["parts"]], 1.0)
    rows = []
    for p in fit["parts"]:
        reasons = [i["why"] if p["key"] != "rules" else f"{CHECK_NAMES.get(i['id'], i['id'])}: {i['why']}"
                   for i in p["items"] if p["key"] != "rules" or i["lost"] > 0.05]
        rows.append([p["title"], f"{p['points']:g} / {p['max']}", "; ".join(reasons) or "every check respected"])
    doc.table(["Part", "Points", "Measured against the profile"], rows, [42, 20, 118], "LRL")

    # ----- Governance -----
    doc.h2("4. Every rule checked")
    rule_items = next(p for p in fit["parts"] if p["key"] == "rules")["items"]
    rows = []
    for i in rule_items:
        v, lim = _check_value(i.get("measured"), i.get("target"))
        rows.append([CHECK_NAMES.get(i["id"], i["id"].replace("_", " ").capitalize()), i["status"], v, lim,
                     (i.get("detail") or "")[:220]])
    doc.table(["Rule", "Status", "Measured", "Limit", "Detail"], rows, [40, 22, 22, 18, 78], "LLRRL", status_col=1)

    # ----- Risk measures -----
    doc.h2("5. Risk measures")
    m = fit["measured"]
    doc.facts([
        ("Volatility (annualised)", _pct(m["ann_vol"])),
        ("Worst drop", f"{_pct(abs(m['max_drawdown']))} (from {m['drawdown_peak']} to {m['drawdown_trough']})"),
        ("Back to its previous high", m["recovered_on"] or "not yet, at the end of the data"),
        ("Time below its previous high", f"{m['days_below_previous_high']} days"),
    ])
    doc.para(fit.get("method", ""), 8.5, MUTED)
    if charts and charts.get("risk"):
        doc.chart_title("Where the ups and downs come from",
                        "A holding can be a small part of the money and still cause a large part of the swings",
                        need=len(charts["risk"]) * 9 + 9)
        doc.paired([(t, a, b) for t, (a, b) in sorted(charts["risk"].items(), key=lambda kv: -kv[1][1])],
                   ("Share of the money", "Share of the ups and downs"))
    if charts and charts.get("sectors"):
        sec = list(charts["sectors"].items())
        top, rest = sec[:8], sum(v for _, v in sec[8:])
        rows_s = [(k, v, _pct(v)) for k, v in top] + ([("Other sectors", rest, _pct(rest))] if rest > 0.0005 else [])
        limit = charts.get("max_sector")
        doc.chart_title("Stock sectors", "Share of the whole portfolio, equity funds looked through"
                        + (f"; dashed line: the {_pct(limit, 0)} limit for this risk level" if limit else ""),
                        need=len(rows_s) * 6 + 8)
        doc.bars(rows_s, max([v for _, v, _ in rows_s] + [limit or 0]) * 1.05, VIZ[0],
                 ref=(f"limit {_pct(limit, 0)}", limit) if limit else None)

    # ----- Provenance -----
    doc.h2("6. How this portfolio was built, and the sources")
    facts = [("Report", f"{rid} (sha256 of the weights, profile limits, score and data period)"),
             ("Governance rules", f"rules.yaml version {fit.get('rules_version')}")]
    if run:
        pr = run.get("prompt_ref") or {}
        plan = run.get("execution_plan") or {}
        facts += [("Built by run", f"{run.get('run_id')} on {str(run.get('created_at') or '')[:16].replace('T', ' ')}"),
                  ("Analyses used", ", ".join(ANALYSES.get(a.get("agent"), a.get("agent")) for a in plan.get("enabled_agents") or []) or "-"),
                  ("Explanation template", f"{pr.get('key')} version {pr.get('version')} (sha256 {str(pr.get('sha256') or '')[:12]})")]
    else:
        facts.append(("Built by", "a mix tried in the app (not an AlphaForge proposal)"))
    doc.facts(facts)
    doc.ln(1)
    doc.para("Data: daily total returns (dividends reinvested) converted to CAD - Yahoo Finance via yfinance (prices), "
             "Bank of Canada FXUSDCAD (exchange rate); ETF holdings from State Street, iShares Canada and SEC N-PORT; "
             "controversies from verified rows only.", 8.5, MUTED)
    for a in fit.get("assumptions") or []:
        doc.para(f"- {a}", 8.5, MUTED)
    return bytes(doc.output())
