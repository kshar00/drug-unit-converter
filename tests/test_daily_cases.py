"""Daily added test cases: one new behaviour check per day, synthetic data only."""

import pandas as pd

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
