"""Daily added test cases: one new behaviour check per day, synthetic data only."""

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
