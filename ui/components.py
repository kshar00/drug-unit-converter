"""Styling and small HTML components for the Streamlit UI.

Colours are chosen to work on both the light and the dark Streamlit theme: neutral surfaces use
semi-transparent greys over whatever the theme background is, and the confidence pills are solid
fills with white text (each at least 4.5:1 contrast).
"""

from __future__ import annotations

from html import escape

import streamlit as st

CONFIDENCE_COLOURS = {"High": "#1a7f37", "Medium": "#8a5300", "Low": "#c62828"}

CSS = """
<style>
/* layout */
.block-container { padding-top: 4rem; padding-bottom: 3rem; max-width: 1280px; }
[data-testid="stAppDeployButton"], .stAppDeployButton, footer { display: none !important; }
section[data-testid="stSidebar"] .block-container { padding-top: 1.5rem; }

/* header */
.duc-header { display: flex; justify-content: space-between; align-items: flex-end; gap: 1rem;
  flex-wrap: wrap; padding-bottom: .85rem; margin-bottom: .4rem;
  border-bottom: 1px solid rgba(128,128,128,.28); }
.duc-header h1 { font-size: 1.55rem; font-weight: 650; margin: 0; padding: 0; letter-spacing: -.01em; }
.duc-header p { margin: .2rem 0 0 0; opacity: .72; font-size: .92rem; max-width: 52rem; }
.duc-chips { display: flex; gap: .4rem; flex-wrap: wrap; }
.duc-chip { font-size: .74rem; font-weight: 550; padding: .18rem .55rem; border-radius: 999px;
  border: 1px solid rgba(128,128,128,.35); white-space: nowrap; opacity: .9; }

.duc-header h1 a, .duc-result-head h3 a, [data-testid="stHeaderActionElements"] { display: none !important; }

/* section labels */
.duc-label { font-size: .72rem; font-weight: 650; text-transform: uppercase; letter-spacing: .06em;
  opacity: .62; margin: 1.1rem 0 .35rem 0; }

/* result header */
.duc-result-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 1rem;
  flex-wrap: wrap; margin: .6rem 0 .7rem 0; }
.duc-result-head h3 { font-size: 1.15rem; font-weight: 650; margin: 0; padding: 0; line-height: 1.35; }
.duc-meta { font-size: .84rem; opacity: .7; margin-top: .15rem; }
.duc-meta code { font-size: .8rem; }

/* KPI tiles */
.duc-kpis { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: .6rem; margin-bottom: .4rem; }
.duc-kpis.five { grid-template-columns: repeat(5, minmax(0, 1fr)); }
.duc-tile { border: 1px solid rgba(128,128,128,.25); background: rgba(128,128,128,.06);
  border-radius: .5rem; padding: .65rem .85rem; min-width: 0; }
.duc-tile .k { font-size: .72rem; font-weight: 600; text-transform: uppercase; letter-spacing: .05em; opacity: .65; }
.duc-tile .v { font-size: 1.45rem; font-weight: 650; line-height: 1.25; margin-top: .15rem;
  font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }
.duc-tile .s { font-size: .78rem; opacity: .65; margin-top: .05rem; }
.duc-tile.accent-High { border-left: 4px solid #1a7f37; }
.duc-tile.accent-Medium { border-left: 4px solid #8a5300; }
.duc-tile.accent-Low { border-left: 4px solid #c62828; }
@media (max-width: 760px) {
  .duc-kpis, .duc-kpis.five { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .duc-tile .v { font-size: 1.2rem; }
}

/* confidence pill */
.duc-pill { display: inline-block; font-size: .78rem; font-weight: 650; letter-spacing: .02em;
  padding: .16rem .62rem; border-radius: 999px; color: #fff; white-space: nowrap; vertical-align: middle; }
.duc-tile .v .duc-pill { font-size: 1rem; padding: .18rem .75rem; }

/* reasons */
.duc-reasons { margin: 0; padding-left: 1.1rem; font-size: .92rem; }
.duc-reasons li { margin: .12rem 0; }

/* key-value cards */
.duc-kv { width: 100%; border-collapse: collapse; font-size: .88rem; }
.duc-kv td { padding: .38rem .2rem; border-bottom: 1px solid rgba(128,128,128,.18); vertical-align: top; }
.duc-kv tr:last-child td { border-bottom: none; }
.duc-kv td.k { width: 42%; opacity: .68; padding-right: .8rem; }
.duc-kv td.v { font-variant-numeric: tabular-nums; overflow-wrap: anywhere; }

/* callouts */
.duc-note { border: 1px solid rgba(128,128,128,.28); border-left: 4px solid var(--duc-accent, #0b5c7a);
  border-radius: .45rem; padding: .6rem .8rem; font-size: .86rem; line-height: 1.45; }
.duc-note b { display: block; font-size: .8rem; text-transform: uppercase; letter-spacing: .05em; margin-bottom: .15rem; }

/* confidence distribution bar */
.duc-bar { display: flex; height: .6rem; border-radius: 999px; overflow: hidden; margin: .35rem 0 .3rem 0;
  background: rgba(128,128,128,.15); }
.duc-bar span { display: block; height: 100%; }
.duc-legend { display: flex; gap: 1rem; flex-wrap: wrap; font-size: .8rem; opacity: .8; }
.duc-legend i { display: inline-block; width: .65rem; height: .65rem; border-radius: 2px; margin-right: .3rem;
  vertical-align: -1px; }

/* streamlit widgets */
[data-testid="stForm"] { border-radius: .6rem; }
[data-testid="stTabs"] button[role="tab"] p { font-weight: 600; }
[data-testid="stExpander"] summary p { font-weight: 600; font-size: .9rem; }
[data-testid="stMetricValue"] { font-variant-numeric: tabular-nums; }
</style>
"""


def inject_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def html(markup: str) -> None:
    st.markdown(markup, unsafe_allow_html=True)


def header(title: str, subtitle: str, chips: list[str]) -> None:
    chip_html = "".join(f'<span class="duc-chip">{escape(c)}</span>' for c in chips)
    html(
        f'<div class="duc-header"><div><h1>{escape(title)}</h1><p>{escape(subtitle)}</p></div>'
        f'<div class="duc-chips">{chip_html}</div></div>'
    )


def label(text: str) -> None:
    html(f'<div class="duc-label">{escape(text)}</div>')


def pill(confidence: str) -> str:
    colour = CONFIDENCE_COLOURS.get(confidence, "#5f6368")
    return f'<span class="duc-pill" style="background:{colour}">{escape(confidence)}</span>'


def tile(key: str, value: str, sub: str = "", accent: str | None = None, raw_value: bool = False) -> str:
    cls = f"duc-tile accent-{accent}" if accent else "duc-tile"
    v = value if raw_value else escape(value)
    s = f'<div class="s">{escape(sub)}</div>' if sub else ""
    return f'<div class="{cls}"><div class="k">{escape(key)}</div><div class="v">{v}</div>{s}</div>'


def tiles(items: list[str]) -> None:
    cls = "duc-kpis five" if len(items) == 5 else "duc-kpis"
    html(f'<div class="{cls}">{"".join(items)}</div>')


def kv_table(rows: list[tuple[str, str]]) -> str:
    body = "".join(f'<tr><td class="k">{escape(k)}</td><td class="v">{escape(v)}</td></tr>' for k, v in rows)
    return f'<table class="duc-kv">{body}</table>'


def reasons_list(reasons: list[str]) -> str:
    return '<ul class="duc-reasons">' + "".join(f"<li>{escape(r)}</li>" for r in reasons) + "</ul>"


def note(title: str, body: str, accent: str = "#0b5c7a") -> None:
    html(f'<div class="duc-note" style="--duc-accent:{accent}"><b>{escape(title)}</b>{escape(body)}</div>')


def confidence_bar(counts: dict[str, int]) -> None:
    total = sum(counts.values()) or 1
    segs = "".join(
        f'<span style="width:{100 * counts.get(k, 0) / total:.2f}%;background:{c}"></span>'
        for k, c in CONFIDENCE_COLOURS.items()
    )
    legend = "".join(
        f'<span><i style="background:{c}"></i>{k} {counts.get(k, 0)} ({100 * counts.get(k, 0) / total:.0f}%)</span>'
        for k, c in CONFIDENCE_COLOURS.items()
    )
    html(f'<div class="duc-bar">{segs}</div><div class="duc-legend">{legend}</div>')
