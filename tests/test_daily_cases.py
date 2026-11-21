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
