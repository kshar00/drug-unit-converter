"""Financial check and its combination with the structural decision."""

import pytest

from converter.convert import ClaimLine
from converter.crosswalk import FactorEstimate
from converter.financial import choose_amount, financial_check
from converter.infer import Settings, combine, decide

ASP = {"payment_limit": 62.25}  # per 1 mg HCPCS unit
VIAL = FactorEstimate(  # 100 mg / 4 mL vial: 25 HCPCS units per mL
    hcpcs="J9271", short_description=None, hcpcs_dosage="1 MG", drug_name=None, ndc_unit="ML", factor=25,
    ndc_units_per_item=4, ndc_units_per_package=4, hcpcs_units_per_package=100, factor_source="cms",
)
NADAC = {"nadac_per_unit": 1556.25, "pricing_unit": "ML"}  # 25 x 62.25


def check(q, settings=Settings(), asp=ASP, nadac=None, **amounts):
    line = ClaimLine(ndc="00006302602", quantity=q, **amounts)
    d = decide(q, VIAL, settings)
    return d, financial_check(line, d.readings, "ML", asp, nadac, settings)


# --- amount selection ----------------------------------------------------------


def test_amount_preference_and_fee_strip():
    issues = []
    assert choose_amount(ClaimLine("x", 1, ingredient_cost=100, allowed_amount=150, fee=10), issues)[:3] == ("ingredient_cost", 100, None)
    assert choose_amount(ClaimLine("x", 1, allowed_amount=150, fee=10), issues)[:3] == ("allowed_amount", 140, 10)
    assert choose_amount(ClaimLine("x", 1, billed_amount=500), issues) == ("billed_amount", 500, None, True)
    assert choose_amount(ClaimLine("x", 1), issues)[0] is None


def test_fee_larger_than_amount_not_stripped():
    issues = []
    assert choose_amount(ClaimLine("x", 1, allowed_amount=5, fee=10), issues)[1] == 5
    assert issues[0].code == "fee_exceeds_amount"


# --- verdicts ------------------------------------------------------------------


def test_allowed_amount_picks_hcpcs_reading():
    _, fc = check(200, allowed_amount=12450)
    assert fc.verdict == "HCPCS" and not fc.near_edge
    asp_cmp = fc.comparisons[0]
    assert asp_cmp.implied["HCPCS"] == pytest.approx(62.25)
    assert asp_cmp.variance["NDC"] == pytest.approx(12450 / 5000 / 62.25 - 1)


def test_ingredient_cost_picks_ndc_reading_with_nadac():
    _, fc = check(8, ingredient_cost=12450, nadac=NADAC)
    assert fc.verdict == "NDC"
    assert {c.reference for c in fc.comparisons} == {"ASP", "NADAC"}


def test_near_edge_of_band():
    _, fc = check(200, allowed_amount=200 * 62.25 * 1.22)  # +22%, inside 25% but beyond 80% of it
    assert fc.verdict == "HCPCS" and fc.near_edge


def test_outside_band_fits_neither():
    _, fc = check(200, allowed_amount=200 * 62.25 * 1.6)
    assert fc.verdict == "NEITHER"


def test_band_is_configurable():
    _, fc = check(200, Settings(tolerance_band=0.7), allowed_amount=200 * 62.25 * 1.6)
    assert fc.verdict == "HCPCS"


def test_billed_charge_markup_test():
    _, fc = check(200, billed_amount=200 * 62.25 * 3)  # 3x markup: fails a 25% band, passes the markup test
    assert fc.one_sided and fc.verdict == "HCPCS"


def test_factor_near_one_fits_both():
    est = FactorEstimate("J1", None, "1 ML", None, "ML", 1.1, 1, 1, 1.1, "cms")
    line = ClaimLine("x", 10, allowed_amount=100)
    d = decide(10, est)
    fc = financial_check(line, d.readings, "ML", {"payment_limit": 10}, None)
    assert fc.verdict == "BOTH"


def test_asp_and_nadac_disagree_follow_primary_reference():
    # ASP says HCPCS, NADAC says NDC
    odd_nadac = {"nadac_per_unit": 62.25, "pricing_unit": "ML"}
    _, fc = check(200, ingredient_cost=12450, nadac=odd_nadac)  # pharmacy cost: NADAC leads
    assert fc.verdict == "NDC" and fc.reference_conflict
    assert any(i.code == "reference_conflict" and i.severity == "penalty" for i in fc.issues)
    _, fc = check(200, allowed_amount=12450, nadac=odd_nadac)  # medical allowed: ASP leads
    assert fc.verdict == "HCPCS" and fc.reference_conflict


def test_nadac_unit_mismatch_skips_nadac():
    _, fc = check(8, ingredient_cost=12450, nadac={"nadac_per_unit": 3112.5, "pricing_unit": "EA"})
    assert [c.reference for c in fc.comparisons] == ["ASP"]
    assert any(i.code == "nadac_unit_mismatch" for i in fc.issues)


def test_no_amounts_or_prices():
    assert not check(200)[1].available
    assert not check(200, asp=None, allowed_amount=100)[1].available


def test_summary_for_display():
    _, fc = check(200, allowed_amount=12450)
    s = fc.summary("HCPCS")[0]
    assert s["reference"] == "ASP" and s["within_band"] and s["variance_pct"] == pytest.approx(0, abs=0.1)
    assert fc.summary(None) == []


# --- combine -------------------------------------------------------------------


def test_combine_agree_is_high():
    d, fc = check(200, allowed_amount=12450)
    assert combine(d, fc)[:2] == ("HCPCS", "High")


def test_combine_near_edge_is_medium():
    d, fc = check(200, allowed_amount=200 * 62.25 * 1.22)
    assert combine(d, fc)[:2] == ("HCPCS", "Medium")


def test_combine_billed_only_is_medium():
    d, fc = check(200, billed_amount=200 * 62.25 * 3)
    assert combine(d, fc)[:2] == ("HCPCS", "Medium")


def test_combine_financial_breaks_structural_tie():
    est = FactorEstimate("J9035", None, "10 MG", None, "ML", 2.5, 4, 4, 10, "cms")  # 35 units vs 35 mL tie
    line = ClaimLine("x", 35, allowed_amount=35 * 75.49)
    d = decide(35, est)
    assert d.basis is None
    fc = financial_check(line, d.readings, "ML", {"payment_limit": 75.49}, None)
    assert combine(d, fc)[:2] == ("HCPCS", "Medium")


def test_combine_conflict_is_low_and_follows_price():
    # whole vials say NDC (8 mL); allowed amount priced as 8 HCPCS units says HCPCS
    d, fc = check(8, allowed_amount=8 * 62.25)
    assert d.basis == "NDC" and fc.verdict == "HCPCS"
    basis, level, reasons = combine(d, fc)
    assert (basis, level) == ("HCPCS", "Low") and "conflict" in reasons[-1]


def test_combine_conflict_with_impossible_reading():
    # 2.5 can't be HCPCS units, but the price says HCPCS
    d, fc = check(2.5, allowed_amount=2.5 * 62.25)
    assert fc.verdict == "HCPCS"
    assert combine(d, fc)[:2] == (None, "Low")


def test_combine_neither_downgrades():
    d, fc = check(200, allowed_amount=200 * 62.25 * 3)
    assert combine(d, fc)[:2] == ("HCPCS", "Low")


def test_combine_no_financials_keeps_structural():
    d, fc = check(200)
    basis, level, reasons = combine(d, fc)
    assert (basis, level) == ("HCPCS", "Medium") and "structural checks only" in reasons[-1]
