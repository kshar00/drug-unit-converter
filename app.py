"""Drug Unit Converter: Streamlit UI (single-line form plus CSV batch upload).

Run locally:  .venv/Scripts/python -m streamlit run app.py
"""

from __future__ import annotations

import hashlib
import importlib
import io
import os
import re
import sys
import tempfile
import threading
from dataclasses import replace
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent

# --- keep the engine in step with the code after a redeploy ---------------------------------------
# Streamlit Cloud pulls new code into a running process and re-runs app.py, but modules it already
# imported (converter.*, data.*, ui.*) can stay at their old version, and so can objects cached with
# st.cache_resource. That mix caused "'ReferenceStore' object has no attribute 'search_products'".
# So: fingerprint the engine code, and when it changes, reload the engine modules (dependencies
# first) before importing from them; the cached store is keyed on the same fingerprint.
ENGINE_MODULES = [
    "converter.ndc", "converter.crosswalk", "converter.infer", "converter.financial", "converter.uom",
    "converter.convert", "converter.aggregate", "converter.reference", "converter.report",
    "data.refresh", "data.build_app_store", "ui.components",
]


def code_version() -> str:
    digest = hashlib.sha1()
    for name in ENGINE_MODULES:
        path = ROOT.joinpath(*name.split(".")).with_suffix(".py")
        if path.exists():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


@st.cache_resource
def _process_state() -> dict:
    return {}


CODE_VERSION = code_version()
_state = _process_state()
if _state.get("code_version") not in (None, CODE_VERSION):
    for _name in ENGINE_MODULES:
        if _name in sys.modules:
            importlib.reload(sys.modules[_name])
_state["code_version"] = CODE_VERSION

from converter.aggregate import GroupResult, convert_lines  # noqa: E402
from converter.convert import ClaimLine, PatientDataError  # noqa: E402
from converter.infer import Settings  # noqa: E402
from converter.reference import ReferenceStore  # noqa: E402
from converter.report import group_rows, line_rows, lines_from_dataframe, template_csv  # noqa: E402
from converter.uom import LABELS as UOM_LABELS  # noqa: E402
from data.build_app_store import materialize  # noqa: E402
from ui import components as ui  # noqa: E402

FULL_DB = ROOT / "data" / "reference.duckdb"  # built locally by python -m data.refresh
APP_PARQUET = ROOT / "data" / "app"  # committed slim copy (python -m data.build_app_store)
BUILT_DB = Path(tempfile.gettempdir()) / "drug_unit_converter" / "reference.duckdb"
SAMPLE_CSV = ROOT / "samples" / "synthetic_claims.csv"
MAX_BATCH_LINES = 5000
MODIFIERS = ["JW", "JZ", "RT", "LT", "KP", "KQ"]
UOM_CHOICES = ["", "EA", "ML", "GM", "UN", "F2", "ME"]
AMOUNT_FIELDS = ("ingredient_cost", "allowed_amount", "billed_amount", "fee")

st.set_page_config(page_title="Drug Unit Converter", page_icon=":material/medication:", layout="wide")
ui.inject_css()


# --- reference store -----------------------------------------------------------------------------


def load_store() -> tuple[ReferenceStore | None, threading.Lock, str | None]:
    return _load_store(CODE_VERSION)


@st.cache_resource(show_spinner="Loading reference data…", max_entries=2)
def _load_store(version: str) -> tuple[ReferenceStore | None, threading.Lock, str | None]:
    # `version` is only the cache key (no leading underscore: Streamlit doesn't hash those arguments)
    return _open_store()


def _open_store() -> tuple[ReferenceStore | None, threading.Lock, str | None]:
    if FULL_DB.exists() and not os.environ.get("DUC_USE_BUNDLED"):  # DUC_USE_BUNDLED=1 tests the deploy path
        return ReferenceStore(FULL_DB), threading.Lock(), "local full store"
    if any(APP_PARQUET.glob("*.parquet")):
        newest = max(f.stat().st_mtime for f in APP_PARQUET.glob("*.parquet"))
        if not BUILT_DB.exists() or BUILT_DB.stat().st_mtime < newest:
            materialize(BUILT_DB, APP_PARQUET)
        return ReferenceStore(BUILT_DB), threading.Lock(), "bundled slim store"
    return None, threading.Lock(), None


@st.cache_data
def data_vintage(db_name: str) -> pd.DataFrame:
    store, lock, _ = load_store()
    with lock:
        return store.con.execute(
            """
            SELECT source, any_value(quarter ORDER BY loaded_at DESC) AS quarter,
                   max(rows) AS rows, max(loaded_at)::DATE AS loaded
            FROM refresh_log GROUP BY source ORDER BY source
            """
        ).df().fillna("")


store, lock, db_name = load_store()


def run(lines: list[ClaimLine], settings: Settings) -> list[GroupResult]:
    with lock:  # one DuckDB connection is shared across sessions
        return convert_lines(lines, store, settings)


@st.cache_data(max_entries=8, show_spinner=False)
def convert_csv(source: bytes, band: float, typical: float):
    """Parse and convert an uploaded CSV; cached so widget clicks don't re-run the batch."""
    df = pd.read_csv(io.BytesIO(source), dtype=str, keep_default_na=False)
    lines, errors, ignored = lines_from_dataframe(df)
    if len(lines) > MAX_BATCH_LINES:
        raise ValueError(f"{len(lines):,} lines is more than this demo handles ({MAX_BATCH_LINES:,}); split the file.")
    groups = run(lines, Settings(tolerance_band=band, typical_max_packages=typical))
    return len(lines), groups, errors, ignored


vintage = None
if store is not None:
    try:
        vintage = data_vintage(db_name)
    except Exception:  # refresh_log missing in a hand-built store
        vintage = None


# --- sidebar -------------------------------------------------------------------------------------

with st.sidebar:
    ui.note(
        "No patient data",
        "This tool never asks for or stores patient name, ID, date of birth, prescriber or Rx number. "
        "Enter synthetic or de-identified claim lines only. Uploads with patient-like columns are refused, "
        "and nothing you enter is saved.",
    )
    ui.label("Settings")
    band = st.slider("Price tolerance band (±%)", 5, 60, 25, 5,
                     help="An implied price within this % of ASP or NADAC counts as a fit.")
    typical = st.slider("Typical maximum packages per line", 2, 50, 12,
                        help="Readings above this many packages score lower in the structural check.")
    settings = Settings(tolerance_band=band / 100, typical_max_packages=typical)
    if store is not None:
        ui.label("Reference data")
        if vintage is not None:
            st.dataframe(
                vintage, hide_index=True, width="stretch", column_order=["source", "quarter", "rows"],
                column_config={
                    "source": st.column_config.TextColumn("Source"),
                    "quarter": st.column_config.TextColumn("Period"),
                    "rows": st.column_config.NumberColumn("Rows", format="localized"),
                },
            )
            loaded = str(pd.to_datetime(vintage["loaded"]).max().date()) if "loaded" in vintage and len(vintage) else ""
        else:
            loaded = ""
        st.caption(f"{db_name.capitalize()}{f', loaded {loaded}' if loaded else ''}. Public sources: FDA NDC "
                   "Directory, CMS ASP NDC-HCPCS crosswalk and payment limits, Medicaid NADAC.")


# --- header --------------------------------------------------------------------------------------

chips = ["Public data only", "No PHI"]
if vintage is not None and not vintage.empty:
    cms = vintage[vintage["source"].str.contains("asp", case=False)]["quarter"]
    if not cms.empty and cms.iloc[0]:
        chips.append(f"CMS ASP {cms.iloc[0]}")
chips.append(f"Tolerance ±{band}%")
ui.header(
    "Drug Unit Converter",
    "NDC (NCPDP) units ↔ HCPCS billing units for drug claim lines whose quantity basis is unknown, "
    "with the likely basis, a confidence level and the evidence behind it.",
    chips,
)

if store is None:
    st.error("Reference data not found. Build it with `python -m data.refresh` (then `python -m data.build_app_store`).")
    st.stop()


# --- result rendering ----------------------------------------------------------------------------

BASIS = {"NDC": "NDC units", "HCPCS": "HCPCS units", "EITHER": "Either (1:1)", None: "Undetermined"}
ROLE = {"administered": "Administered", "waste": "JW waste", "RT": "Right (RT)", "LT": "Left (LT)"}
SEVERITY = {"edge": "Edge case", "penalty": "Penalty", "info": "Note"}


def fmt(x, unit: str = "") -> str:
    if x is None:
        return "–"
    s = f"{x:,.4f}".rstrip("0").rstrip(".")
    return f"{s} {unit}".strip()


def money(x) -> str:
    return "–" if x is None else f"${x:,.2f}"


def render_group(g: GroupResult) -> None:
    r = g.result
    line = r.line

    meta = [f"NDC {r.ndc_hyphenated or line.ndc}"]
    if r.hcpcs:
        meta.append(f"{r.hcpcs} ({r.hcpcs_dosage or '?'})")
    if line.modifiers or g.aggregated:
        mods = sorted({m for s in g.lines for m in s.line.modifiers})
        if mods:
            meta.append("modifiers " + " ".join(mods))
    if line.unit_of_measure:
        meta.append(f"claim UoM {line.unit_of_measure}")
    meta.append(f"quantity {fmt(line.quantity)}")
    ui.html(
        '<div class="duc-result-head"><div>'
        f'<h3>{ui.escape(r.product_description or "Product not found")}</h3>'
        f'<div class="duc-meta">{ui.escape(" · ".join(meta))}</div></div></div>'
    )

    ui.tiles([
        ui.tile("NDC units", fmt(r.ndc_units), r.ncpdp_unit or "unit unknown"),
        ui.tile("HCPCS units", fmt(r.hcpcs_units), r.hcpcs or "no HCPCS match"),
        ui.tile("Quantity was in", BASIS.get(r.inferred_basis, r.inferred_basis),
                ("best guess · " if r.basis_is_guess else "")
                + (f"factor {fmt(r.factor)} HCPCS per NDC unit" if r.factor else "")),
        ui.tile("Confidence", ui.pill(r.confidence), "", accent=r.confidence, raw_value=True),
    ])

    if r.basis_is_guess and r.readings:
        other = "NDC" if r.inferred_basis == "HCPCS" else "HCPCS"
        o = r.readings[other]
        ui.note(
            "Best guess. ",
            f"The checks couldn't tell which unit {fmt(line.quantity)} is in, so this uses the usual billing "
            f"convention ({'a line with a HCPCS code is counted in HCPCS units' if r.inferred_basis == 'HCPCS' else 'a line without a HCPCS code is counted in NDC units'}). "
            f"If it's {BASIS[other]} instead: {fmt(o.ndc_units)} {r.ncpdp_unit or 'NDC units'} = "
            f"{fmt(o.hcpcs_units)} HCPCS units. An allowed amount or ingredient cost would settle it.",
            accent=ui.CONFIDENCE_COLOURS["Medium"],
        )
    elif r.inferred_basis is None and r.readings:
        st.warning(
            f"Couldn't tell which unit {fmt(line.quantity)} is in. If it's NDC units it is "
            f"{fmt(r.readings['NDC'].hcpcs_units)} HCPCS units; if it's HCPCS units it is "
            f"{fmt(r.readings['HCPCS'].ndc_units, r.ncpdp_unit or '')}."
        )

    ui.label("Why")
    ui.html(ui.reasons_list(r.reasons))

    left, right = st.columns(2)
    with left:
        ui.label("Product")
        ui.html(ui.kv_table([
            ("NDC (11-digit)", r.ndc_hyphenated or "–"),
            ("NCPDP billing unit", r.ncpdp_unit or "–"),
            ("Package size", fmt(r.package_size, r.ncpdp_unit or "")),
            ("FDA package", r.package_description or "–"),
        ]))
    with right:
        ui.label("HCPCS")
        ui.html(ui.kv_table([
            ("Code", r.hcpcs or "–"),
            ("Descriptor", f"{r.hcpcs_description or ''} ({r.hcpcs_dosage or '?'})" if r.hcpcs else "–"),
            ("HCPCS units per NDC unit", fmt(r.factor)),
            ("Other candidates", ", ".join(c.hcpcs for c in r.candidates if c.hcpcs != r.hcpcs) or "None"),
        ]))

    fc = r.financial_check
    if fc and fc.comparisons:
        amount_name = (fc.amount_field or "amount").replace("_", " ")
        ui.label(f"Price check · {amount_name} {money(fc.amount)}"
                 f"{' after stripping the fee' if fc.fee_stripped else ''}")
        rows = []
        for c in fc.comparisons:
            for basis in ("NDC", "HCPCS"):
                if basis in c.implied:
                    rows.append({
                        "reading": BASIS[basis],
                        "reference": f"{c.reference} per {c.unit}",
                        "reference_price": c.reference_price,
                        "implied_price": c.implied[basis],
                        "variance": round(100 * c.variance[basis], 1) + 0.0,  # no "-0.0%"
                        "fits": "Fit" if c.fits[basis] else "Outside band",
                    })
        st.dataframe(
            pd.DataFrame(rows), hide_index=True, width="stretch",
            column_config={
                "reading": st.column_config.TextColumn("If the quantity is in"),
                "reference": st.column_config.TextColumn("Reference"),
                "reference_price": st.column_config.NumberColumn("Reference price", format="$%.4f"),
                "implied_price": st.column_config.NumberColumn("Implied price", format="$%.4f"),
                "variance": st.column_config.NumberColumn("Variance", format="%+.1f%%"),
                "fits": st.column_config.TextColumn(f"Within ±{band}%"),
            },
        )

    if g.aggregated:
        with st.expander(f"Line breakdown · {len(g.lines)} lines combined", expanded=True):
            st.dataframe(
                pd.DataFrame([
                    {"line": s.line.line_no, "role": ROLE.get(s.role, s.role),
                     "modifiers": " ".join(s.line.modifiers), "quantity": s.line.quantity,
                     "ndc_units": s.ndc_units, "hcpcs_units": s.hcpcs_units}
                    for s in g.lines
                ]),
                hide_index=True, width="stretch",
                column_config={
                    "line": st.column_config.TextColumn("Line"),
                    "role": st.column_config.TextColumn("Role"),
                    "modifiers": st.column_config.TextColumn("Modifiers"),
                    "quantity": st.column_config.NumberColumn("Quantity", format="localized"),
                    "ndc_units": st.column_config.NumberColumn(f"NDC units ({r.ncpdp_unit or '?'})", format="localized"),
                    "hcpcs_units": st.column_config.NumberColumn("HCPCS units", format="localized"),
                },
            )

    if r.readings:
        with st.expander("Both readings · structural checks"):
            st.dataframe(
                pd.DataFrame([
                    {
                        "reading": BASIS[b], "ndc_units": rd.ndc_units, "hcpcs_units": rd.hcpcs_units,
                        "items": rd.items, "packages": rd.packages, "possible": rd.feasible,
                        "score": rd.score, "passed": "; ".join(rd.passed), "failed": "; ".join(rd.failed),
                    }
                    for b, rd in r.readings.items()
                ]),
                hide_index=True, width="stretch",
                column_config={
                    "reading": st.column_config.TextColumn("If the quantity is in"),
                    "ndc_units": st.column_config.NumberColumn("NDC units", format="localized"),
                    "hcpcs_units": st.column_config.NumberColumn("HCPCS units", format="localized"),
                    "items": st.column_config.NumberColumn("Items (vials, tablets…)", format="localized"),
                    "packages": st.column_config.NumberColumn("Packages", format="localized"),
                    "possible": st.column_config.CheckboxColumn("Possible"),
                    "score": st.column_config.NumberColumn("Score"),
                    "passed": st.column_config.TextColumn("Passed", width="large"),
                    "failed": st.column_config.TextColumn("Failed", width="large"),
                },
            )

    if r.issues:
        with st.expander(f"Flags and notes · {len(r.issues)}"):
            st.dataframe(
                pd.DataFrame([{"severity": SEVERITY.get(i.severity, i.severity), "flag": i.code, "detail": i.message}
                              for i in r.issues]),
                hide_index=True, width="stretch",
                column_config={
                    "severity": st.column_config.TextColumn("Severity"),
                    "flag": st.column_config.TextColumn("Flag"),
                    "detail": st.column_config.TextColumn("Detail", width="large"),
                },
            )


# --- tabs ----------------------------------------------------------------------------------------

tab_one, tab_batch, tab_how = st.tabs(["Single line", "Batch CSV", "How it works"])

EXAMPLES = {
    "—": {},
    "Keytruda 200 units, allowed amount (HCPCS units)": dict(ndc="0006-3026-02", qty=200.0, hcpcs="J9271", allowed=12450.60),
    "Keytruda 8 mL, ingredient cost (NDC units)": dict(ndc="00006302602", qty=8.0, uom="ML", ingredient=12200.0),
    "Avastin 35 units + 5 JW waste": dict(ndc="50242-060-01", qty=35.0, hcpcs="J9035", waste=5.0, allowed=3019.68),
    "Remicade 40, no amounts": dict(ndc="57894-030-01", qty=40.0, hcpcs="J1745"),
    "Ambiguous 10-digit NDC 5024206001, qty 4": dict(ndc="5024206001", qty=4.0),
}
FIELDS = dict(ndc="", qty=None, uom="", hcpcs="", mods=[], dos=None, waste=None,
              ingredient=None, allowed=None, billed=None, fee=None)


def load_example():
    ex = EXAMPLES[st.session_state["example"]]
    for k, default in FIELDS.items():
        st.session_state[f"f_{k}"] = ex.get(k, default)


@st.cache_data(max_entries=256, show_spinner=False)
def search_products(query: str) -> list[dict]:
    with lock:
        return store.search_products(query, limit=50)


def _package_short(desc: str | None) -> str:
    text = re.sub(r"\(\s*\d[\d-]*\s*\)", "", desc or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text if len(text) <= 70 else text[:67] + "…"


def product_label(row: dict) -> str:
    name = row.get("proprietary_name") or row.get("nonproprietary_name") or "?"
    generic = row.get("nonproprietary_name")
    if generic and generic.lower() != str(name).lower():
        name = f"{name} ({generic})"
    strength = f"{row['strength']} {row['strength_unit']}" if row.get("strength") and row.get("strength_unit") else ""
    n = row["ndc11"]
    parts = [f"{name} {strength}".strip(), _package_short(row.get("package_description")),
             f"NDC {n[:5]}-{n[5:9]}-{n[9:]}", row.get("hcpcs") or "no HCPCS crosswalk"]
    label = " · ".join(p for p in parts if p)
    return label + (" · delisted" if row.get("source") == "excluded" else "")


def use_product(rows: list[dict]) -> None:
    i = st.session_state.get("product_pick")
    if i is None:
        return
    row = rows[i]
    n = row["ndc11"]
    st.session_state["f_ndc"] = f"{n[:5]}-{n[5:9]}-{n[9:]}"
    codes = [c.strip() for c in (row.get("hcpcs") or "").split(",") if c.strip()]
    st.session_state["f_hcpcs"] = codes[0] if len(codes) == 1 else ""


with tab_one:
    find_col, demo_col = st.columns([3, 2])
    query = find_col.text_input(
        "Find a product (optional)", key="product_query",
        placeholder="Brand, generic or HCPCS code, e.g. Keytruda, pembrolizumab, J9271",
        help="Searches the FDA NDC Directory. Picking a package fills in its NDC (and the HCPCS code when "
             "the CMS crosswalk has exactly one). You can also type any NDC straight into the form.",
    )
    demo_col.selectbox("Or fill in a demo line (optional)", list(EXAMPLES), key="example", on_change=load_example)
    if query and len(query.strip()) >= 3:
        matches = search_products(query.strip())
        if matches:
            st.selectbox(
                f"{len(matches)}{'+' if len(matches) == 50 else ''} matching packages: pick one to fill in the NDC",
                range(len(matches)), index=None, key="product_pick",
                format_func=lambda i: product_label(matches[i]),
                placeholder="Choose a package…",
                on_change=use_product, args=(matches,),
            )
            if len(matches) == 50:
                st.caption("Showing the first 50. Packages with a HCPCS crosswalk are listed first; refine the search to narrow it.")
        else:
            st.caption("No product matches that name or HCPCS code. You can still type an NDC below.")
    elif query:
        st.caption("Type at least 3 characters.")
    with st.form("single"):
        a, b, c, d = st.columns([2.2, 1, 1.3, 1])
        ndc = a.text_input("NDC (any format)", key="f_ndc", placeholder="0006-3026-02")
        qty = b.number_input("Quantity (unit unknown)", min_value=0.0, value=None, step=1.0, key="f_qty")
        uom = c.selectbox("Unit of measure (optional)", UOM_CHOICES, key="f_uom",
                          format_func=lambda v: UOM_LABELS.get(v, "Not given"),
                          help="The unit reported on the claim, if any. Used as a weak hint: billing "
                               "systems often fill it in by default.")
        hcpcs = d.text_input("HCPCS (optional)", key="f_hcpcs", placeholder="J9271")
        a, b, c = st.columns([2.2, 1.3, 2.3])
        mods = a.multiselect("Modifiers (optional)", MODIFIERS, key="f_mods")
        dos = b.date_input("Date of service (optional)", value=None, key="f_dos")
        waste = c.number_input("JW waste quantity (optional)", min_value=0.0, value=None, step=1.0, key="f_waste",
                               help="Adds a separate JW line, in the same unit as the quantity.")
        with st.expander("Financials (optional, used to confirm the unit basis)"):
            a, b, c, d = st.columns(4)
            ingredient = a.number_input("Ingredient cost ($)", min_value=0.0, value=None, key="f_ingredient")
            allowed = b.number_input("Allowed amount ($)", min_value=0.0, value=None, key="f_allowed")
            billed = c.number_input("Billed amount ($)", min_value=0.0, value=None, key="f_billed")
            fee = d.number_input("Dispensing / admin fee ($)", min_value=0.0, value=None, key="f_fee")
        submitted = st.form_submit_button("Convert", type="primary")

    if submitted:
        if not ndc.strip() or not qty:
            st.error("Enter an NDC and a quantity greater than zero.")
        else:
            line = ClaimLine(
                ndc=ndc.strip(), quantity=float(qty), hcpcs=hcpcs.strip().upper() or None,
                modifiers=tuple(m for m in mods if m != "JW"), date_of_service=dos,
                ingredient_cost=ingredient, allowed_amount=allowed, billed_amount=billed, fee=fee,
                unit_of_measure=uom or None, claim_id="FORM", line_no="1",
            )
            lines = [line]
            if waste:
                # amounts entered in the form are the total for both lines: the JW line carries zero
                zero = {k: (0.0 if getattr(line, k) is not None else None) for k in AMOUNT_FIELDS}
                lines.append(replace(line, quantity=float(waste), modifiers=("JW",), line_no="2", **zero))
                if any(v is not None for v in zero.values()):
                    st.caption("Amounts are taken as the total for the administered and JW lines together.")
            for g in run(lines, settings):
                render_group(g)


# Batch results table: curated, formatted view; the CSV download keeps every column.
RESULT_COLUMNS = {
    "claim_id": st.column_config.TextColumn("Claim"),
    "line_no": st.column_config.TextColumn("Line"),
    "ndc11": st.column_config.TextColumn("NDC 11"),
    "product": st.column_config.TextColumn("Product", width="medium"),
    "hcpcs": st.column_config.TextColumn("HCPCS"),
    "modifiers": st.column_config.TextColumn("Mods"),
    "quantity": st.column_config.NumberColumn("Qty", format="localized"),
    "unit_of_measure": st.column_config.TextColumn("UoM"),
    "inferred_basis": st.column_config.TextColumn("Basis"),
    "confidence": st.column_config.TextColumn("Confidence"),
    "ndc_units": st.column_config.NumberColumn("NDC units", format="localized"),
    "ncpdp_unit": st.column_config.TextColumn("NCPDP"),
    "hcpcs_units": st.column_config.NumberColumn("HCPCS units", format="localized"),
    "asp_variance_pct": st.column_config.NumberColumn("ASP var.", format="%+.1f%%"),
    "nadac_variance_pct": st.column_config.NumberColumn("NADAC var.", format="%+.1f%%"),
    "lines_combined": st.column_config.NumberColumn("Lines"),
    "flags": st.column_config.TextColumn("Flags", width="medium"),
    "reason": st.column_config.TextColumn("Reason", width="large"),
}
LINE_COLUMNS = {
    "claim_id": st.column_config.TextColumn("Claim"),
    "line_no": st.column_config.TextColumn("Line"),
    "ndc_input": st.column_config.TextColumn("NDC (input)"),
    "hcpcs": st.column_config.TextColumn("HCPCS"),
    "modifiers": st.column_config.TextColumn("Mods"),
    "role": st.column_config.TextColumn("Role"),
    "quantity": st.column_config.NumberColumn("Qty", format="localized"),
    "unit_of_measure": st.column_config.TextColumn("UoM"),
    "ndc_units": st.column_config.NumberColumn("NDC units", format="localized"),
    "hcpcs_units": st.column_config.NumberColumn("HCPCS units", format="localized"),
    "group_basis": st.column_config.TextColumn("Group basis"),
    "group_confidence": st.column_config.TextColumn("Group confidence"),
}


def show_table(df: pd.DataFrame, columns: dict) -> None:
    order = [c for c in columns if c in df.columns]  # tolerate report columns being added or dropped
    height = min(35 * (len(df) + 1) + 3, 528)  # show small batches without an inner scrollbar
    st.dataframe(df, hide_index=True, width="stretch", height=height, column_order=order,
                 column_config={k: v for k, v in columns.items() if k in df.columns})


with tab_batch:
    st.markdown(
        "Upload a CSV with one claim line per row. Required: **ndc**, **quantity**. Optional: unit_of_measure "
        "(EA, ML, GM, UN, F2, ME), hcpcs, modifiers, date_of_service (YYYY-MM-DD), ingredient_cost, "
        "allowed_amount, billed_amount, fee, and a synthetic claim_id to group JW / RT / LT lines. Common header "
        "spellings (qty, uom, dos, hcpcs_code…) are recognised. Up to "
        f"{MAX_BATCH_LINES:,} lines."
    )
    a, b, c, _ = st.columns([1, 1, 1.3, 1.5])
    a.download_button("Template CSV", template_csv(), "claims_template.csv", "text/csv",
                      icon=":material/download:", width="stretch")
    if SAMPLE_CSV.exists():
        b.download_button("Sample claims", SAMPLE_CSV.read_bytes(), "synthetic_claims.csv", "text/csv",
                          icon=":material/download:", width="stretch")
    if c.button("Run the synthetic sample", icon=":material/play_arrow:", width="stretch"):
        st.session_state["use_sample"] = True
    upload = st.file_uploader("Claims CSV", type=["csv"])
    if upload is not None:
        st.session_state["use_sample"] = False

    source = None
    if upload is not None:
        source = upload.getvalue()
    elif st.session_state.get("use_sample") and SAMPLE_CSV.exists():
        source = SAMPLE_CSV.read_bytes()
        st.caption(f"Showing results for the synthetic sample file ({SAMPLE_CSV.name}).")

    if source is not None:
        try:
            with st.spinner("Converting…"):
                n_lines, groups, errors, ignored = convert_csv(source, settings.tolerance_band, settings.typical_max_packages)
        except PatientDataError as e:
            st.error(str(e))
            st.stop()
        except (ValueError, pd.errors.ParserError, UnicodeDecodeError) as e:
            st.error(f"Could not read the file: {e}")
            st.stop()
        if ignored:
            st.caption(f"Columns not used: {', '.join(ignored)}")
        if errors:
            st.warning(f"{len(errors)} row(s) could not be read and were skipped.")
            st.dataframe(pd.DataFrame(errors), hide_index=True, width="stretch")
        if not groups:
            st.stop()

        out = group_rows(groups)
        per_line = line_rows(groups)

        counts = out["confidence"].value_counts()
        cnt = {k: int(counts.get(k, 0)) for k in ("High", "Medium", "Low")}
        undecided = int((out["inferred_basis"] == "unknown").sum()) if "inferred_basis" in out else 0
        ui.tiles([
            ui.tile("Lines read", f"{n_lines:,}"),
            ui.tile("Results", f"{len(out):,}", "after grouping JW / RT / LT"),
            ui.tile("High", f"{cnt['High']:,}", f"{100 * cnt['High'] / len(out):.0f}% of results", accent="High"),
            ui.tile("Medium", f"{cnt['Medium']:,}", f"{100 * cnt['Medium'] / len(out):.0f}% of results", accent="Medium"),
            ui.tile("Low", f"{cnt['Low']:,}", f"{undecided} undecided" if undecided else
                    f"{100 * cnt['Low'] / len(out):.0f}% of results", accent="Low"),
        ])
        ui.confidence_bar(cnt)

        ui.label("Results")
        show_table(out, RESULT_COLUMNS)
        with st.expander(f"Per-line breakdown · {len(per_line)} lines"):
            show_table(per_line, LINE_COLUMNS)
        a, b, _ = st.columns([1, 1, 2.8])
        a.download_button("Results CSV", out.to_csv(index=False), "converted_claims.csv", "text/csv",
                          icon=":material/download:", width="stretch")
        b.download_button("Per-line CSV", per_line.to_csv(index=False), "converted_lines.csv", "text/csv",
                          icon=":material/download:", width="stretch")

        st.divider()
        ui.label("Result detail")
        labels = [f"{i + 1}. {row.claim_id or ''} · {row.ndc11 or row.ndc_input} · {row.hcpcs or 'no HCPCS'} · {row.confidence}"
                  for i, row in enumerate(out.itertuples())]
        pick = st.selectbox("Result", range(len(labels)), format_func=lambda i: labels[i],
                            label_visibility="collapsed")
        render_group(groups[pick])


with tab_how:
    st.markdown(
        f"""
**1. Normalise the NDC** to 11 digits (5-4-2). An unhyphenated 10-digit NDC is ambiguous, so all three
possible paddings are checked against the reference data.

**2. Package and NCPDP unit** come from the FDA NDC Directory package description (EA, ML or GM).
Powders for injection are billed EA per vial even though FDA lists a reconstituted volume.

**3. Conversion factor** (HCPCS units per NDC unit) comes from the CMS ASP NDC-HCPCS crosswalk,
cross-checked against the FDA labelled strength and the HCPCS dosage descriptor
(e.g. 25 mg/mL ÷ 1 mg = 25 HCPCS units per mL). That cross-check catches crosswalk rows whose package
size or billing units don't add up.

**4. Both readings** of the quantity are computed and scored:
- *Structure*: whole vials or tablets, a typical number of packages, HCPCS units must be whole numbers,
  and JW waste should be less than one vial.
- *Price* (when amounts are given): implied price per HCPCS unit vs the ASP payment limit, and per NDC
  unit vs NADAC, within ±{band}%. Billed charges get a markup test instead.
- *Unit of measure* (when given): EA / ML / GM that matches the product's NCPDP unit hints at NDC units;
  ME (mg) or F2 (IU) hints at HCPCS units when the HCPCS descriptor is exactly 1 mg or 1 unit. It is
  weaker evidence than structure or price, because billing systems often fill it in by default.

**5. Confidence**: **High** when structure and price agree inside the band; **Medium** when only one
check is decisive or the price is near the edge of the band; **Low** when they conflict or for edge
cases (kits, multi-ingredient products, unclassified codes such as J3490 / J3590 / J9999, NDCs with
several HCPCS codes, NDCs missing from the crosswalk).

**6. Modifier lines** on the same synthetic claim, date and HCPCS are combined before converting:
JW waste is added to the administered units, RT / LT sides are added together, and KP / KQ lines are
linked and must agree on the NDC quantity.

**Validation**: simulating one-package claims for all 7,402 NDC/HCPCS pairs in the Q4 2026 crosswalk,
with allowed amounts near ASP, the tool infers the right unit basis on 99.7% of them.

**Limits**: built on public reference data only; ASP is for one quarter; NADAC covers mostly retail
drugs; reference data can lag new NDCs. Not billing or coding advice.
"""
    )
