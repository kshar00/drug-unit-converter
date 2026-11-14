from converter.crosswalk import FactorEstimate, Issue
from converter.infer import Settings, apply_issues, decide


def est(factor, per_item, per_pkg, unit="ML"):
    return FactorEstimate(
        hcpcs="J9999", short_description=None, hcpcs_dosage="1 MG", drug_name=None, ndc_unit=unit,
        factor=factor, ndc_units_per_item=per_item, ndc_units_per_package=per_pkg,
        hcpcs_units_per_package=factor * per_pkg, factor_source="cms",
    )


VIAL = est(25, 4, 4)  # 100 mg / 4 mL vial, 1 mg HCPCS


def test_hcpcs_reading_wins_for_dose_in_mg():
    d = decide(200, VIAL)
    assert d.basis == "HCPCS" and d.level == "Medium"
    assert d.readings["HCPCS"].ndc_units == 8 and d.readings["HCPCS"].items == 2
    assert d.readings["NDC"].hcpcs_units == 5000


def test_ndc_reading_wins_for_whole_vials_in_ml():
    d = decide(8, VIAL)
    assert d.basis == "NDC" and d.level == "Medium"


def test_fractional_quantity_cannot_be_hcpcs():
    d = decide(2.5, est(25, 4, 4))
    assert not d.readings["HCPCS"].feasible
    assert d.basis == "NDC"
    assert "whole" in d.reason


def test_factor_one_is_either_and_high():
    d = decide(3, est(1, 1, 1))
    assert d.basis == "EITHER" and d.level == "High"


def test_tie_is_unknown_and_low():
    # bevacizumab-like 100 mg / 4 mL vial, HCPCS per 10 mg: 35 units vs 35 mL are both plausible
    d = decide(35, est(2.5, 4, 4))
    assert d.basis is None and d.level == "Low"


def test_absurd_package_count_is_infeasible():
    d = decide(5000, est(25, 4, 4), Settings(max_packages=1000))
    assert not d.readings["NDC"].feasible  # 1250 packages
    assert d.basis == "HCPCS"


def test_non_positive_quantity():
    d = decide(0, VIAL)
    assert d.basis is None and d.level == "Low"


def test_apply_issues():
    assert apply_issues("Medium", []) == ("Medium", [])
    assert apply_issues("Medium", [Issue("x", "info only")]) == ("Medium", [])
    assert apply_issues("High", [Issue("x", "p", "penalty"), Issue("y", "q", "penalty")]) == ("Medium", ["p", "q"])
    assert apply_issues("Medium", [Issue("x", "p", "penalty")])[0] == "Low"
    assert apply_issues("High", [Issue("x", "kit", "edge")]) == ("Low", ["kit"])
