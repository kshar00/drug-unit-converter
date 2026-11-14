"""Drug Unit Converter: Streamlit UI (single-line form plus CSV batch upload).

Run locally:  .venv/Scripts/python -m streamlit run app.py
"""

from __future__ import annotations

import io
import os
import tempfile
import threading
from dataclasses import replace
from pathlib import Path

import pandas as pd
import streamlit as st

from converter.aggregate import GroupResult, convert_lines
from converter.convert import ClaimLine, PatientDataError
from converter.infer import Settings
from converter.reference import ReferenceStore
from converter.report import group_rows, line_rows, lines_from_dataframe, template_csv
from data.build_app_store import materialize

ROOT = Path(__file__).resolve().parent
FULL_DB = ROOT / "data" / "reference.duckdb"  # built locally by python -m data.refresh
APP_PARQUET = ROOT / "data" / "app"  # committed slim copy (python -m data.build_app_store)
BUILT_DB = Path(tempfile.gettempdir()) / "drug_unit_converter" / "reference.duckdb"
SAMPLE_CSV = ROOT / "samples" / "synthetic_claims.csv"
MAX_BATCH_LINES = 5000
MODIFIERS = ["JW", "JZ", "RT", "LT", "KP", "KQ"]
AMOUNT_FIELDS = ("ingredient_cost", "allowed_amount", "billed_amount", "fee")

st.set_page_config(page_title="Drug Unit Converter", page_icon="💊", layout="wide")


# --- reference store -----------------------------------------------------------------------------


@st.cache_resource(show_spinner="Loading reference data…")
def load_store() -> tuple[ReferenceStore | None, threading.Lock, str | None]:
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


# --- sidebar -------------------------------------------------------------------------------------

with st.sidebar:
    st.markdown("### No patient data")
    st.info(
        "This tool never asks for or stores patient name, ID, date of birth, prescriber or Rx number. "
        "Enter synthetic or de-identified claim lines only. Uploads with patient-like columns are refused, "
        "and nothing you enter is saved."
    )
    st.markdown("### Settings")
    band = st.slider("Price tolerance band (±%)", 5, 60, 25, 5,
                     help="An implied price within this % of ASP or NADAC counts as a fit.")
    typical = st.slider("Typical maximum packages per line", 2, 50, 12,
                        help="Readings above this many packages score lower in the structural check.")
    settings = Settings(tolerance_band=band / 100, typical_max_packages=typical)
    if store is not None:
        st.markdown("### Reference data")
        try:
            st.dataframe(data_vintage(db_name), hide_index=True, use_container_width=True)
        except Exception:  # refresh_log missing in a hand-built store
            st.caption(db_name)
        st.caption("Public sources: FDA NDC Directory, CMS ASP NDC-HCPCS crosswalk and payment limits, Medicaid NADAC.")

st.title("Drug Unit Converter")
st.markdown(
    "Takes a drug claim line where you don't know whether the quantity is in **NDC (NCPDP) units** or "
    "**HCPCS billing units**, and returns both, which one it most likely is, and how sure it is."
)

if store is None:
    st.error("Reference data not found. Build it with `python -m data.refresh` (then `python -m data.build_app_store`).")
    st.stop()


# --- result rendering ----------------------------------------------------------------------------

BADGE = {"High": ":green[**High**]", "Medium": ":orange[**Medium**]", "Low": ":red[**Low**]"}
BASIS = {"NDC": "NDC units", "HCPCS": "HCPCS units", "EITHER": "either (1:1)", None: "unknown"}


def fmt(x, unit: str = "") -> str:
    if x is None:
        return "–"
    s = f"{x:,.4f}".rstrip("0").rstrip(".")
    return f"{s} {unit}".strip()


def render_group(g: GroupResult) -> None:
    r = g.result
    st.subheader(r.product_description or f"NDC {r.line.ndc}")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric(f"NDC units ({r.ncpdp_unit or '?'})", fmt(r.ndc_units))
    c2.metric(f"HCPCS units ({r.hcpcs or '?'})", fmt(r.hcpcs_units))
    c3.metric("Quantity was in", BASIS.get(r.inferred_basis, r.inferred_basis))
    with c4:
        st.markdown("Confidence")
        st.markdown(f"### {BADGE[r.confidence]}")

    if r.inferred_basis is None and r.readings:
        st.warning(
            f"Couldn't tell which unit {fmt(r.line.quantity)} is in. If it's NDC units it is "
            f"{fmt(r.readings['NDC'].hcpcs_units)} HCPCS units; if it's HCPCS units it is "
            f"{fmt(r.readings['HCPCS'].ndc_units, r.ncpdp_unit or '')}."
        )

    st.markdown("**Why**")
    st.markdown("\n".join(f"- {reason}" for reason in r.reasons))

    left, right = st.columns(2)
    with left:
        st.markdown("**Product**")
        st.dataframe(pd.DataFrame([
            ("NDC (11-digit)", r.ndc_hyphenated or "–"),
            ("NCPDP billing unit", r.ncpdp_unit or "–"),
            ("Package size", fmt(r.package_size, r.ncpdp_unit or "")),
            ("FDA package", r.package_description or "–"),
        ], columns=["", "value"]), hide_index=True, use_container_width=True)
    with right:
        st.markdown("**HCPCS**")
        st.dataframe(pd.DataFrame([
            ("Code", r.hcpcs or "–"),
            ("Descriptor", f"{r.hcpcs_description or ''} ({r.hcpcs_dosage or '?'})" if r.hcpcs else "–"),
            ("HCPCS units per NDC unit", fmt(r.factor)),
            ("Other candidates", ", ".join(c.hcpcs for c in r.candidates if c.hcpcs != r.hcpcs) or "none"),
        ], columns=["", "value"]), hide_index=True, use_container_width=True)

    fc = r.financial_check
    if fc and fc.comparisons:
        st.markdown(f"**Price check** (amount used: {fc.amount_field.replace('_', ' ')} "
                    f"{fmt(fc.amount)}{' after stripping the fee' if fc.fee_stripped else ''})")
        rows = []
        for c in fc.comparisons:
            for basis in ("NDC", "HCPCS"):
                if basis in c.implied:
                    rows.append({
                        "if the quantity is in": BASIS[basis],
                        "reference": f"{c.reference} per {c.unit}",
                        "reference price": round(c.reference_price, 4),
                        "implied price": round(c.implied[basis], 4),
                        "variance %": round(100 * c.variance[basis], 1),
                        "fits": "yes" if c.fits[basis] else "no",
                    })
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)

    if r.readings:
        with st.expander("Both readings, structural checks"):
            st.dataframe(pd.DataFrame([
                {
                    "if the quantity is in": BASIS[b],
                    "NDC units": rd.ndc_units, "HCPCS units": rd.hcpcs_units,
                    "items (vials, tablets…)": rd.items, "packages": rd.packages,
                    "possible": "yes" if rd.feasible else "no", "score": rd.score,
                    "passed": "; ".join(rd.passed), "failed": "; ".join(rd.failed),
                }
                for b, rd in r.readings.items()
            ]), hide_index=True, use_container_width=True)

    if g.aggregated:
        with st.expander(f"Line breakdown ({len(g.lines)} lines combined)", expanded=True):
            st.dataframe(pd.DataFrame([
                {"line": s.line.line_no, "role": s.role, "modifiers": " ".join(s.line.modifiers),
                 "quantity": s.line.quantity, "NDC units": s.ndc_units, "HCPCS units": s.hcpcs_units}
                for s in g.lines
            ]), hide_index=True, use_container_width=True)

    if r.issues:
        with st.expander("All flags and notes"):
            st.dataframe(pd.DataFrame([{"severity": i.severity, "flag": i.code, "detail": i.message} for i in r.issues]),
                         hide_index=True, use_container_width=True)


# --- tabs ----------------------------------------------------------------------------------------

tab_one, tab_batch, tab_how = st.tabs(["Single line", "Batch CSV", "How it works"])

EXAMPLES = {
    "—": {},
    "Keytruda 200 units, allowed amount (HCPCS units)": dict(ndc="0006-3026-02", qty=200.0, hcpcs="J9271", allowed=12450.60),
    "Keytruda 8, ingredient cost (NDC units, mL)": dict(ndc="00006302602", qty=8.0, ingredient=12200.0),
    "Avastin 35 units + 5 JW waste": dict(ndc="50242-060-01", qty=35.0, hcpcs="J9035", waste=5.0, allowed=3019.68),
    "Remicade 40, no amounts": dict(ndc="57894-030-01", qty=40.0, hcpcs="J1745"),
    "Ambiguous 10-digit NDC 5024206001, qty 4": dict(ndc="5024206001", qty=4.0),
}
FIELDS = dict(ndc="", qty=None, hcpcs="", mods=[], dos=None, waste=None, ingredient=None, allowed=None, billed=None, fee=None)


def load_example():
    ex = EXAMPLES[st.session_state["example"]]
    for k, default in FIELDS.items():
        st.session_state[f"f_{k}"] = ex.get(k, default)


with tab_one:
    st.selectbox("Try an example", list(EXAMPLES), key="example", on_change=load_example)
    with st.form("single"):
        a, b, c = st.columns([2, 1, 1])
        ndc = a.text_input("NDC (any format)", key="f_ndc", placeholder="0006-3026-02")
        qty = b.number_input("Quantity (unit unknown)", min_value=0.0, value=None, step=1.0, key="f_qty")
        hcpcs = c.text_input("HCPCS code (optional)", key="f_hcpcs", placeholder="J9271")
        a, b, c = st.columns([2, 1, 1])
        mods = a.multiselect("Modifiers (optional)", MODIFIERS, key="f_mods")
        dos = b.date_input("Date of service (optional)", value=None, key="f_dos")
        waste = c.number_input("JW waste quantity (optional)", min_value=0.0, value=None, step=1.0, key="f_waste",
                               help="Adds a separate JW line, in the same unit as the quantity.")
        with st.expander("Financials (optional, used to confirm the unit basis)"):
            a, b, c, d = st.columns(4)
            ingredient = a.number_input("Ingredient cost", min_value=0.0, value=None, key="f_ingredient")
            allowed = b.number_input("Allowed amount", min_value=0.0, value=None, key="f_allowed")
            billed = c.number_input("Billed amount", min_value=0.0, value=None, key="f_billed")
            fee = d.number_input("Dispensing / admin fee", min_value=0.0, value=None, key="f_fee")
        submitted = st.form_submit_button("Convert", type="primary")

    if submitted:
        if not ndc.strip() or not qty:
            st.error("Enter an NDC and a quantity greater than zero.")
        else:
            line = ClaimLine(
                ndc=ndc.strip(), quantity=float(qty), hcpcs=hcpcs.strip().upper() or None,
                modifiers=tuple(m for m in mods if m != "JW"), date_of_service=dos,
                ingredient_cost=ingredient, allowed_amount=allowed, billed_amount=billed, fee=fee,
                claim_id="FORM", line_no="1",
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


with tab_batch:
    st.markdown(
        "Upload a CSV with one claim line per row. Required: **ndc**, **quantity**. Optional: hcpcs, modifiers, "
        "date_of_service (YYYY-MM-DD), ingredient_cost, allowed_amount, billed_amount, fee, and a synthetic "
        "claim_id to group JW / RT / LT lines. Common header spellings (qty, dos, hcpcs_code…) are recognised."
    )
    a, b, _ = st.columns([1, 1, 2])
    a.download_button("Download template", template_csv(), "claims_template.csv", "text/csv")
    if SAMPLE_CSV.exists():
        b.download_button("Download sample claims", SAMPLE_CSV.read_bytes(), "synthetic_claims.csv", "text/csv")
    upload = st.file_uploader("Claims CSV", type=["csv"])
    if st.button("Or run the synthetic sample file"):
        st.session_state["use_sample"] = True
    if upload is not None:
        st.session_state["use_sample"] = False

    source = None
    if upload is not None:
        source = upload.getvalue()
    elif st.session_state.get("use_sample") and SAMPLE_CSV.exists():
        source = SAMPLE_CSV.read_bytes()

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
            st.dataframe(pd.DataFrame(errors), hide_index=True)
        if not groups:
            st.stop()

        out = group_rows(groups)
        per_line = line_rows(groups)

        counts = out["confidence"].value_counts()
        m = st.columns(5)
        m[0].metric("Lines read", n_lines)
        m[1].metric("Results (after grouping)", len(out))
        m[2].metric("High", int(counts.get("High", 0)))
        m[3].metric("Medium", int(counts.get("Medium", 0)))
        m[4].metric("Low", int(counts.get("Low", 0)))

        st.dataframe(out, hide_index=True, use_container_width=True)
        with st.expander("Per-line breakdown"):
            st.dataframe(per_line, hide_index=True, use_container_width=True)
        a, b, _ = st.columns([1, 1, 2])
        a.download_button("Download results CSV", out.to_csv(index=False), "converted_claims.csv", "text/csv")
        b.download_button("Download per-line CSV", per_line.to_csv(index=False), "converted_lines.csv", "text/csv")

        st.markdown("#### Look at one result")
        labels = [f"{i + 1}. {row.claim_id or ''} {row.ndc11 or row.ndc_input} {row.hcpcs or ''} [{row.confidence}]"
                  for i, row in enumerate(out.itertuples())]
        pick = st.selectbox("Result", range(len(labels)), format_func=lambda i: labels[i])
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
