"""Plotly figures for the dashboard (reference data-viz palette, validated light + dark).

Colour jobs: categorical slots 1-2 (blue, orange) = current vs proposed; sequential blue = magnitude;
diverging blue<->red with a gray midpoint = correlation; status colours (good/warning/critical) ONLY for
verdicts and limits, always with a text label. One y-axis per chart, thin marks, hover on every mark.
"""
from __future__ import annotations

import plotly.graph_objects as go

PALETTE = {
    "light": {"s1": "#2a78d6", "s2": "#eb6834", "ink": "#0b0b0b", "ink2": "#52514e", "muted": "#898781",
              "grid": "#e1e0d9", "axis": "#c3c2b7", "mid": "#f0efec"},
    "dark": {"s1": "#3987e5", "s2": "#d95926", "ink": "#f0efec", "ink2": "#c3c2b7", "muted": "#898781",
             "grid": "#2c2c2a", "axis": "#383835", "mid": "#383835"},
}
STATUS = {"PASS": "#0ca30c", "WARN": "#fab219", "BLOCK": "#d03b3b"}
STATUS_ICON = {"PASS": "✅", "WARN": "⚠️", "BLOCK": "⛔"}
SEQ_BLUE = ["#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b"]


def _layout(fig: go.Figure, mode: str, title: str, height: int = 340, **kw) -> go.Figure:
    c = PALETTE[mode]
    fig.update_layout(
        title={"text": title, "font": {"size": 15, "color": c["ink"]}, "x": 0},
        height=height, margin={"l": 8, "r": 8, "t": 44, "b": 8},
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font={"color": c["ink2"], "size": 12}, hoverlabel={"font_size": 12},
        legend={"orientation": "h", "y": 1.08, "x": 0, "font": {"color": c["ink2"]}}, **kw)
    fig.update_xaxes(gridcolor=c["grid"], linecolor=c["axis"], zerolinecolor=c["axis"], tickfont={"color": c["muted"]})
    fig.update_yaxes(gridcolor=c["grid"], linecolor=c["axis"], zerolinecolor=c["axis"], tickfont={"color": c["muted"]})
    return fig


def position_curve(data: dict, chosen: float, mode: str = "light") -> go.Figure:
    """Crisis max drawdown vs weight of the tested asset, with the profile's limit and the user's own idea."""
    c = PALETTE[mode]
    xs = [w for w, _ in data["curve"]]
    ys = [d for _, d in data["curve"]]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines", name=f"Portfolio loss in {data['window']} crisis",
                             line={"color": c["s1"], "width": 2},
                             hovertemplate=f"{data['asset']} %{{x:.1%}} → worst loss %{{y:.1%}}<extra></extra>"))
    if data.get("tested"):
        t = data["tested"]
        fig.add_trace(go.Scatter(x=[t["weight"]], y=[t["max_drawdown"]], mode="markers+text", name="Your idea",
                                 marker={"size": 11, "color": STATUS["BLOCK"], "line": {"width": 2, "color": "white"}},
                                 text=[f"your idea: {t['weight']:.0%}"], textposition="top left",
                                 textfont={"color": c["ink"]},
                                 hovertemplate=f"{data['asset']} %{{x:.0%}} → worst loss %{{y:.1%}}<extra></extra>"))
    from views import interpolate

    y_chosen = interpolate(data["curve"], chosen) if chosen <= xs[-1] else (
        data["tested"]["max_drawdown"] if data.get("tested") else ys[-1])
    ok = y_chosen >= data["limit"]
    fig.add_trace(go.Scatter(x=[chosen], y=[y_chosen], mode="markers", name="Selected weight",
                             marker={"size": 13, "color": STATUS["PASS"] if ok else STATUS["BLOCK"],
                                     "line": {"width": 2, "color": "white"}},
                             hovertemplate=f"{data['asset']} %{{x:.1%}} → %{{y:.1%}}<extra></extra>"))
    fig.add_hline(y=data["limit"], line={"color": STATUS["BLOCK"], "width": 1.5, "dash": "dash"},
                  annotation_text=f"your limit {data['limit']:.0%}", annotation_position="bottom right",
                  annotation_font_color=c["ink"])
    tl = data.get("tested_limit")
    fig.add_vline(x=data["max_weight"], line={"color": c["muted"], "width": 1, "dash": "dot"},
                  annotation_text=f"max ≈ {data['max_weight']:.1%}" + (f" at {tl:.0%}" if data.get("limit_differs") else ""),
                  annotation_position="top left", annotation_font_color=c["ink2"])
    fig.update_xaxes(tickformat=".0%", title_text=f"Weight of {data['asset']}")
    fig.update_yaxes(tickformat=".0%", title_text="Worst loss (max drawdown)")
    return _layout(fig, mode, f"How much {data['asset']} fits your loss limit?", height=380)


def allocation(data: dict, mode: str = "light") -> go.Figure:
    c = PALETTE[mode]
    names = sorted(set(data["current"]) | set(data["proposed"]),
                   key=lambda k: -max(data["current"].get(k, 0), data["proposed"].get(k, 0)))
    fig = go.Figure()
    for key, label, color in (("current", "Current", c["s1"]), ("proposed", "Proposed (governance-checked)", c["s2"])):
        if data[key]:
            fig.add_trace(go.Bar(x=names, y=[data[key].get(n, 0) for n in names], name=label,
                                 marker={"color": color, "cornerradius": 4},
                                 hovertemplate="%{x}: %{y:.1%}<extra>" + label + "</extra>"))
    fig.update_layout(barmode="group", bargap=0.25, bargroupgap=0.08)
    fig.update_yaxes(tickformat=".0%")
    return _layout(fig, mode, "Current vs proposed weights")


def risk_vs_weight(data: dict, mode: str = "light") -> go.Figure:
    c = PALETTE[mode]
    names = sorted(data["risk"], key=lambda k: -data["risk"][k])
    fig = go.Figure()
    fig.add_trace(go.Bar(x=names, y=[data["weights"].get(n, 0) for n in names], name="Share of the money",
                         marker={"color": c["s1"], "cornerradius": 4}, hovertemplate="%{x}: %{y:.1%} of weight<extra></extra>"))
    fig.add_trace(go.Bar(x=names, y=[data["risk"][n] for n in names], name="Share of the risk",
                         marker={"color": c["s2"], "cornerradius": 4}, hovertemplate="%{x}: %{y:.1%} of risk<extra></extra>"))
    fig.update_layout(barmode="group", bargap=0.25, bargroupgap=0.08)
    fig.update_yaxes(tickformat=".0%")
    return _layout(fig, mode, "Money vs risk, per position")


def sectors(data: dict, mode: str = "light") -> go.Figure:
    c = PALETTE[mode]
    items = sorted(data["by_sector"].items(), key=lambda kv: kv[1])
    fig = go.Figure(go.Bar(x=[v for _, v in items], y=[k for k, _ in items], orientation="h",
                           marker={"color": c["s1"], "cornerradius": 4},
                           hovertemplate="%{y}: %{x:.1%}<extra></extra>", text=[f"{v:.0%}" for _, v in items],
                           textposition="outside", textfont={"color": c["ink2"]}))
    fig.update_xaxes(tickformat=".0%")
    return _layout(fig, mode, "Exposure by sector (ETFs looked through)", height=max(260, 28 * len(items) + 80))


def correlation(data: dict, mode: str = "light") -> go.Figure:
    c = PALETTE[mode]
    names = list(data["matrix"])
    z = [[data["matrix"][a].get(b) for b in names] for a in names]
    fig = go.Figure(go.Heatmap(
        z=z, x=names, y=names, zmin=-1, zmax=1,
        colorscale=[[0, "#184f95"], [0.5, c["mid"]], [1, "#d03b3b"]],
        text=[[f"{v:.2f}" if v is not None else "" for v in row] for row in z], texttemplate="%{text}",
        hovertemplate="%{y} vs %{x}: %{z:.2f}<extra></extra>", xgap=2, ygap=2,
        colorbar={"title": "corr.", "tickfont": {"color": c["muted"]}}))
    return _layout(fig, mode, "Correlation of daily returns (1 = move together)", height=360)


def stress_assets(per_asset: dict, mode: str = "light") -> go.Figure:
    c = PALETTE[mode]
    items = sorted(per_asset.items(), key=lambda kv: kv[1])
    fig = go.Figure(go.Bar(x=[v for _, v in items], y=[k for k, _ in items], orientation="h",
                           marker={"color": [STATUS["BLOCK"] if v < 0 else c["s1"] for _, v in items],
                                   "cornerradius": 4},
                           text=[f"{v:+.0%}" for _, v in items], textposition="outside",
                           textfont={"color": c["ink2"]}, hovertemplate="%{y}: %{x:+.1%} in the crisis<extra></extra>"))
    fig.update_xaxes(tickformat=".0%")
    return _layout(fig, mode, "Each asset during the crisis window", height=max(240, 30 * len(items) + 80))


S3 = {"light": "#1baf7a", "dark": "#199e70"}  # categorical slot 3 (aqua): third Venn set only


def replay(data: dict, limit: float | None, mode: str = "light", detailed: bool = False) -> go.Figure:
    """Money day by day if a year like 2022 happened again: the idea vs the proposal, with the loss limit in $."""
    c = PALETTE[mode]
    cap = data["capital"]
    names = {"idea": ("Your idea", c["s2"]), "proposal": ("Suggested mix", c["s1"])}
    fig = go.Figure()
    for key in ("idea", "proposal"):
        s = data["series"].get(key)
        if not s:
            continue
        label, color = names[key]
        fig.add_trace(go.Scatter(x=s["dates"], y=s["value"], mode="lines", name=label, line={"color": color, "width": 2},
                                 hovertemplate=label + " · %{x|%b %d}: $%{y:,.0f}<extra></extra>"))
        end = s["value"][-1]
        short = "Idea" if key == "idea" else "Suggested"
        fig.add_annotation(x=s["dates"][-1], y=end, text=f"{short} ${end:,.0f}", showarrow=False, xanchor="left",
                           xshift=6, font={"color": c["ink2"], "size": 11})
        if detailed:
            fig.add_trace(go.Scatter(x=[s["trough_date"]], y=[s["trough_value"]], mode="markers", showlegend=False,
                                     marker={"size": 9, "color": color, "line": {"width": 2, "color": "white"}},
                                     hovertemplate=f"{label} low point %{{x|%b %d}}: $%{{y:,.0f}} "
                                                   f"({s['max_drawdown']:.1%})<extra></extra>"))
            fig.add_annotation(x=s["trough_date"], y=s["trough_value"], text=f"low {s['max_drawdown']:.1%}",
                               showarrow=True, arrowhead=0, ay=30 if key == "idea" else -42, ax=0 if key == "idea" else -30,
                               font={"color": c["ink"], "size": 11}, arrowcolor=c["muted"])
    fig.add_hline(y=cap, line={"color": c["muted"], "width": 1, "dash": "dot"},
                  annotation_text=f"start ${cap:,.0f}", annotation_position="top right", annotation_font_color=c["ink2"])
    if limit is not None:
        floor = cap * (1 + limit)
        fig.add_hline(y=floor, line={"color": STATUS["BLOCK"], "width": 1.5, "dash": "dash"},
                      annotation_text=f"the most you accept to lose: ${cap - floor:,.0f} ({abs(limit):.0%})",
                      annotation_position="bottom left", annotation_font_color=c["ink"])
    fig.update_yaxes(tickprefix="$", tickformat=",.0f")
    dates = [d for s in data["series"].values() for d in s["dates"]]
    fig.update_xaxes(tickformat="%b", range=[min(dates), max(dates)] if dates else None)
    title = "If a year like 2022 happened again" + (" · day by day, daily rebalancing" if detailed else "")
    fig = _layout(fig, mode, title, height=360 if detailed else 300)
    fig.update_layout(margin={"l": 8, "r": 130, "t": 44, "b": 8})   # room for the end-value labels
    return fig


def score_gauge(score: float, band: str, mode: str = "light") -> go.Figure:
    c = PALETTE[mode]
    color = STATUS["BLOCK"] if score < 3.5 else STATUS["WARN"] if score < 6.5 else STATUS["PASS"]
    fig = go.Figure(go.Indicator(
        mode="gauge+number", value=score, number={"suffix": " / 10", "font": {"size": 30, "color": c["ink"]}},
        title={"text": band, "font": {"size": 13, "color": c["ink2"]}},
        gauge={"axis": {"range": [0, 10], "tickwidth": 1, "tickcolor": c["muted"], "tickfont": {"color": c["muted"]}},
               "bar": {"color": color, "thickness": 0.35}, "bgcolor": c["grid"], "borderwidth": 0}))
    fig.update_layout(height=200, margin={"l": 24, "r": 24, "t": 36, "b": 8}, paper_bgcolor="rgba(0,0,0,0)",
                      font={"color": c["ink2"]})
    return fig


def _region_text(items: list[str], limit: int = 4) -> str:
    shown = items[:limit] + ([f"+{len(items) - limit} more"] if len(items) > limit else [])
    return "<br>".join(shown) if shown else "–"


def venn2(v: dict, mode: str = "light", height: int = 260) -> go.Figure:
    """Two overlapping sets with their members written in each region (identity never by colour alone)."""
    c = PALETTE[mode]
    fig = go.Figure()
    for x0, color in ((0.0, c["s2"]), (1.2, c["s1"])):
        fig.add_shape(type="circle", x0=x0, y0=0, x1=x0 + 2, y1=2, line={"color": color, "width": 2},
                      fillcolor=color, opacity=0.18, layer="below")
    texts = [(0.55, 1.0, v["left"]), (1.6, 1.0, v["both"]), (2.65, 1.0, v["right"])]
    for x, y, items in texts:
        fig.add_annotation(x=x, y=y, text=_region_text(items), showarrow=False, font={"size": 12, "color": c["ink"]})
    for x, title in ((0.7, v["left_title"]), (1.6, v["both_title"]), (2.5, v["right_title"])):
        fig.add_annotation(x=x, y=2.22 if title != v["both_title"] else 1.55, text=f"<b>{title}</b>", showarrow=False,
                           font={"size": 12, "color": c["ink2"]})
    fig.update_xaxes(visible=False, range=[-0.1, 3.3])
    fig.update_yaxes(visible=False, range=[-0.1, 2.45], scaleanchor="x")
    fig.update_layout(height=height, margin={"l": 0, "r": 0, "t": 4, "b": 0}, paper_bgcolor="rgba(0,0,0,0)",
                      plot_bgcolor="rgba(0,0,0,0)", showlegend=False)
    return fig


def venn3(v: dict, mode: str = "light") -> go.Figure:
    """Three sets (A, B, C); each region lists the holdings that belong to exactly those sets."""
    c = PALETTE[mode]
    centres = {"A": (1.0, 1.75), "B": (2.0, 1.75), "C": (1.5, 0.9)}
    colors = {"A": c["s1"], "B": c["s2"], "C": S3[mode]}
    r = 1.0
    fig = go.Figure()
    for k, (x, y) in centres.items():
        fig.add_shape(type="circle", x0=x - r, y0=y - r, x1=x + r, y1=y + r, line={"color": colors[k], "width": 2},
                      fillcolor=colors[k], opacity=0.16, layer="below")
    pos = {"A": (0.55, 2.05), "B": (2.45, 2.05), "C": (1.5, 0.3), "AB": (1.5, 2.25), "AC": (0.95, 1.05),
           "BC": (2.05, 1.05), "ABC": (1.5, 1.5)}
    for key, (x, y) in pos.items():
        items = v["regions"].get(key, [])
        if items:
            fig.add_annotation(x=x, y=y, text=("<b>" if key == "ABC" else "") + _region_text(items, 3)
                               + ("</b>" if key == "ABC" else ""), showarrow=False,
                               font={"size": 12 if key != "ABC" else 13, "color": c["ink"]})
    for k, (x, y, anchor) in {"A": (0.0, 2.95, "left"), "B": (3.0, 2.95, "right"), "C": (1.5, -0.3, "center")}.items():
        fig.add_annotation(x=x, y=y, text=f"<b>{v['titles'][k]}</b>", showarrow=False, xanchor=anchor,
                           font={"size": 12, "color": c["ink2"]})
    fig.update_xaxes(visible=False, range=[-0.1, 3.1])
    fig.update_yaxes(visible=False, range=[-0.5, 3.1], scaleanchor="x")
    fig.update_layout(height=340, margin={"l": 0, "r": 0, "t": 4, "b": 0}, paper_bgcolor="rgba(0,0,0,0)",
                      plot_bgcolor="rgba(0,0,0,0)", showlegend=False)
    return fig
