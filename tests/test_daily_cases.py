"""Daily added test cases: one new behaviour check per day, synthetic data only."""

import pandas as pd
import pytest

from converter.convert import ClaimLine
from converter.crosswalk import FactorEstimate
from converter.financial import financial_check
from converter.infer import Settings, decide
from converter.report import lines_from_dataframe
from converter.uom import uom_hint


def test_uom_hint_needs_matching_dose_kind_and_scales_descriptor_units():
    """ME/F2 hint HCPCS units only when one HCPCS unit is exactly 1 mg / 1 unit of the same kind, after unit scaling."""
    # 1000 MCG is exactly 1 mg, so a milligram quantity equals HCPCS units
    assert uom_hint("ME", "ML", "1000 MCG")[0] == "HCPCS"
    # 1 MCG is 0.001 mg: milligrams are not HCPCS units
    hint, issues = uom_hint("ME", "ML", "1 MCG")
    assert hint is None and issues[0].code == "uom_no_hint"
    # kinds must line up: milligrams vs a per-unit descriptor, international units vs a per-mg descriptor
    assert uom_hint("ME", "EA", "1 UNIT")[0] is None
    assert uom_hint("F2", "EA", "1 MG")[0] is None
    # unparseable descriptor gives no hint rather than an error
    hint, issues = uom_hint("F2", "EA", None)
    assert hint is None and "unknown" in issues[0].message


def test_pharmacy_csv_headers_map_and_duplicate_aliases_keep_first_column():
    """Pharmacy-style headers map to ClaimLine fields; a second column aliasing the same field is ignored, not merged."""
    df = pd.DataFrame({
        "NDC11": ["00002771501"], "National Drug Code": ["99999-9999-99"],
        "Quantity Dispensed": ["1,000"], "Qty": ["7"],
        "Quantity Qualifier": ["ea"], "Fill Date": ["2026-11-03T00:00:00"],
        "Ingredient Cost Paid": ["$1,234.50"], "Dispensing Fee": ["10.00"],
        "Modifier": ["jw; jz"],
    })
    lines, errors, ignored = lines_from_dataframe(df)
    assert not errors
    assert ignored == ["National Drug Code", "Qty"]
    (line,) = lines
    assert line.ndc == "00002771501" and line.quantity == 1000
    assert line.unit_of_measure == "EA"
    assert str(line.date_of_service) == "2026-11-03"
    assert (line.ingredient_cost, line.fee) == (1234.5, 10.0)
    assert line.modifiers == ("JW", "JZ")


def test_billed_charge_one_sided_band_edges_markup_ceiling_and_fee_strip():
    """Billed charges fit between 0.75x and 5x ASP after the fee is stripped; below 0.8x is near the edge."""
    # 100 mg / 4 mL vial, 1 mg HCPCS unit: 25 HCPCS units per mL; ASP $10.00 per unit
    vial = FactorEstimate(
        hcpcs="J9999", short_description=None, hcpcs_dosage="1 MG", drug_name=None, ndc_unit="ML", factor=25,
        ndc_units_per_item=4, ndc_units_per_package=4, hcpcs_units_per_package=100, factor_source="cms",
    )
    asp = {"payment_limit": 10.0}
    settings = Settings()

    def billed(ratio, fee=50.0):
        # 100 HCPCS units (4 mL): net billed = ratio x ASP x 100, plus a fee that must be stripped first
        line = ClaimLine(ndc="00000000000", quantity=100, billed_amount=ratio * 1000 + fee, fee=fee)
        d = decide(100, vial, settings)
        return financial_check(line, d.readings, "ML", asp, None, settings)

    below = billed(0.70)  # under the 0.75x floor: neither reading fits
    assert below.one_sided and below.fee_stripped == 50.0
    assert below.amount == pytest.approx(700.0)
    assert below.verdict == "NEITHER"

    edge = billed(0.78)  # just above the floor but under 0.8x: fits, flagged near the edge
    assert edge.verdict == "HCPCS" and edge.near_edge

    markup = billed(4.9)  # a heavy markup a symmetric band would reject still fits, not near the edge
    assert markup.verdict == "HCPCS" and not markup.near_edge
    assert markup.comparisons[0].variance["HCPCS"] == pytest.approx(3.9)

    assert billed(5.5).verdict == "NEITHER"  # over the 5x markup ceiling
    # a looser band lowers the floor: 0.70x now fits under +/-35%
    line = ClaimLine(ndc="00000000000", quantity=100, billed_amount=750.0, fee=50.0)
    loose = Settings(tolerance_band=0.35)
    assert financial_check(line, decide(100, vial, loose).readings, "ML", asp, None, loose).verdict == "HCPCS"


def test_two_jw_waste_lines_sum_into_total_but_penalise_as_possible_duplicate(tmp_path):
    """Two JW lines on one claim still roll up with the administered line, but drop one level as a possible duplicate."""
    from datetime import date

    from converter.aggregate import convert_lines
    from tests.store_builder import asp, build_store, product, xw

    bev = "50242-0060-01"  # 100 mg / 4 mL single-dose vial, J9035 per 10 mg: 2.5 HCPCS units per mL
    store = build_store(
        tmp_path,
        [product(bev, "1 VIAL, SINGLE-DOSE in 1 CARTON (50242-060-01)  / 4 mL in 1 VIAL, SINGLE-DOSE",
                 name="Bevtest", strength="25", strength_unit="mg/mL")],
        [xw(bev, "J9035", "10 MG", 4, 1, 10, 10, "Bevtest")],
        [asp("J9035", 75.49, "10 MG")],
    )
    try:
        def line(q, mods=(), amount=None, n=None):
            return ClaimLine(ndc=bev, quantity=q, hcpcs="J9035", modifiers=tuple(mods), allowed_amount=amount,
                             date_of_service=date(2026, 11, 5), claim_id="SYN-DUPJW", line_no=n)

        # 35 administered + 2 JW + 3 JW = 40 HCPCS units = 16 mL = 4 whole vials (0.5 vial wasted); allowed matches ASP
        clean = [line(35, amount=2642.15, n=1), line(5, ["JW"], 377.45, 2)]
        dup = [line(35, amount=2642.15, n=1), line(2, ["JW"], 150.98, 2), line(3, ["JW"], 226.47, 3)]
        (g_clean,) = convert_lines(clean, store)
        (g_dup,) = convert_lines(dup, store)
    finally:
        store.close()

    r = g_dup.result
    assert g_dup.aggregated and r.line.quantity == 40 and r.line.line_no == "1,2,3"
    assert r.line.allowed_amount == pytest.approx(3019.60)
    assert (r.inferred_basis, r.hcpcs_units, r.ndc_units) == ("HCPCS", 40, 16)
    issues = {i.code: i.severity for i in r.issues}
    assert issues["duplicate_waste"] == "penalty"
    assert "duplicate_lines" not in issues  # only one administered line
    # same totals in a single JW line score High; the duplicate costs exactly one level
    assert g_clean.result.confidence == "High" and r.confidence == "Medium"
    assert [(s.role, s.hcpcs_units, s.ndc_units) for s in g_dup.lines] == [
        ("administered", 35, 14), ("waste", 2, 0.8), ("waste", 3, 1.2)]


def test_ten_digit_ndc_matching_two_real_products_stays_unresolved_and_low(tmp_path):
    """An unhyphenated 10-digit NDC whose 4-4-2 and 5-3-2 readings both exist is not guessed: no NDC, Low."""
    from converter.convert import convert_line
    from tests.store_builder import build_store, product, xw

    vial = "1 VIAL, SINGLE-DOSE in 1 CARTON / 10 mL in 1 VIAL, SINGLE-DOSE"
    ref = build_store(
        tmp_path,
        [product("1234-5678-90", vial, name="Alphamab"), product("12345-678-90", vial, name="Betamab")],
        [xw("1234-5678-90", "J1111", "10 MG", 10, 1, 10, 10), xw("12345-678-90", "J2222", "10 MG", 10, 1, 10, 10)],
    )
    resolved = ref.resolve("1234567890")
    assert resolved.ndc11 is None and resolved.found_in == []
    assert "several candidates exist: 01234567890, 12345067890" in resolved.reason

    # the same digits with hyphens are unambiguous and pick one product
    assert ref.resolve("12345-678-90").ndc11 == "12345067890"

    res = convert_line(ClaimLine(ndc="1234567890", quantity=10), ref)
    assert res.ndc11 is None and res.ndc_units is None and res.hcpcs_units is None
    assert res.confidence == "Low"
    assert any("several candidates exist" in r for r in res.reasons)
    assert any("ambiguous" in r for r in res.reasons)
