"""Conversion factors. Cases mirror real crosswalk quirks found in the Q4 2026 CMS file."""

import pytest

from converter.crosswalk import (
    estimate_factor,
    hcpcs_candidates,
    parse_hcpcs_dosage,
    strength_factor,
    strength_per_ndc_unit,
)


def row(size, qty, bu, bup, dosage="1 MG", hcpcs="J9999"):
    return dict(hcpcs=hcpcs, hcpcs_dosage=dosage, pkg_size=size, pkg_qty=qty, bill_units=bu, bill_units_pkg=bup)


def pkg(qty, unit, strength=None, strength_unit=None):
    return dict(pkg_qty=qty, ncpdp_unit=unit, strength=strength, strength_unit=strength_unit)


def codes(est):
    return {i.code: i.severity for i in est.issues}


# --- parsing helpers ---------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("10 MG", ("mass", 10.0)),
        ("1MG", ("mass", 1.0)),
        ("100 MCG", ("mass", 0.1)),
        ("1.5 GM", ("mass", 1500.0)),
        ("UP TO 80 MG", ("mass", 80.0)),
        ("1000 UNITS", ("unit", 1000.0)),
        ("1 IU", ("unit", 1.0)),
        ("0.1 ML", ("volume", 0.1)),
        ("250 CC", ("volume", 250.0)),
        ("", None),
        (None, None),
        ("PER STUDY DOSE", None),
    ],
)
def test_parse_hcpcs_dosage(text, expected):
    assert parse_hcpcs_dosage(text) == expected


@pytest.mark.parametrize(
    "strength, unit, ndc_unit, expected",
    [
        ("40", "mg/mL", "ML", ("mass", 40.0)),
        ("2.5", "mg/.5mL", "ML", ("mass", 5.0)),
        ("120", "ug/.3mL", "ML", ("mass", 0.4)),
        ("100", "mg/1", "EA", ("mass", 100.0)),
        ("100", "[iU]/mL", "ML", ("unit", 100.0)),
        ("50", "g/100mL", "ML", ("mass", 500.0)),
        ("100", "mg/10mL", "EA", None),  # powder vial: per reconstituted mL, can't line up with EA
        ("1.5; 0.5", "g/1; g/1", "EA", None),  # multi-ingredient
        (None, "mg/mL", "ML", None),
        ("24", "mg/h", "EA", None),
    ],
)
def test_strength_per_ndc_unit(strength, unit, ndc_unit, expected):
    got = strength_per_ndc_unit(strength, unit, ndc_unit)
    if expected is None:
        assert got is None
    else:
        assert got[0] == expected[0] and got[1] == pytest.approx(expected[1])


def test_strength_factor_skips_up_to_descriptors():
    assert strength_factor(pkg(2, "ML", ".25", "mg/2mL"), "UP TO 0.50 MG", "ML") is None


def test_volume_descriptor_needs_no_strength():
    assert strength_factor(None, "500 ML", "ML") == pytest.approx(0.002)


# --- factor estimation -------------------------------------------------------


def test_single_vial_agreement():
    # pembrolizumab-like: 100 mg / 4 mL vial, J-code per 1 mg
    e = estimate_factor(row(4, 1, 100, 100), pkg(4, "ML", "25", "mg/mL"))
    assert e.factor == 25 and e.factor_source == "cms"
    assert e.ndc_units_per_item == 4 and e.ndc_units_per_package == 4 and e.hcpcs_units_per_package == 100
    assert codes(e) == {"strength_confirms": "info"}


def test_carton_of_two():
    e = estimate_factor(row(4, 2, 100, 200), pkg(8, "ML", "25", "mg/mL"))
    assert e.factor == 25 and e.ndc_units_per_package == 8 and e.hcpcs_units_per_package == 200


def test_cms_rounds_billing_units_up():
    # 0.5 mL syringe, HCPCS per 1 mL: BILLUNITS 0.5, BILLUNITSPKG rounded up to 1
    e = estimate_factor(row(0.5, 1, 0.5, 1, dosage="1 ML"), pkg(0.5, "ML"))
    assert e.factor == 1
    assert codes(e)["cms_rounded_up"] == "info"


def test_powder_vial_billed_each():
    # infliximab-like: 100 mg powder vial, NCPDP EA, J-code per 10 mg
    e = estimate_factor(row(1, 1, 10, 10, dosage="10 MG"), pkg(1, "EA", "100", "mg/1"))
    assert e.factor == 10 and e.ndc_unit == "EA"


def test_strength_picks_cms_item_size_over_fda_total():
    # granisetron-like: CMS says 4 mL item, FDA package parsed as 1 mL; strength 1 mg/mL, per 100 mcg
    e = estimate_factor(row(4, 1, 40, 40, dosage="100 MCG"), pkg(1, "ML", "1", "mg/mL"))
    assert e.factor == 10
    assert codes(e)["package_mismatch"] == "penalty"


def test_tablet_bottle_item_size_from_fda():
    # methylprednisolone-like: 50 x 16 mg tablets, J-code per 4 mg; CMS PKG SIZE 200 is not a tablet count
    e = estimate_factor(row(200, 1, 200, 200, dosage="4 MG"), pkg(50, "EA", "16", "mg/1"))
    assert e.factor == 4 and e.ndc_units_per_item == 50


def test_wrong_bag_size_fixed_by_volume_descriptor():
    # dextrose-like: CMS PKG SIZE 500 but bags are 1000 mL (FDA 10 x 1000 mL); HCPCS per 500 mL
    e = estimate_factor(row(500, 10, 2, 20, dosage="500 ML"), pkg(10000, "ML", "50", "mg/mL"))
    assert e.factor == pytest.approx(0.002) and e.ndc_units_per_item == 1000


def test_inconsistent_billunitspkg_settled_by_strength():
    e = estimate_factor(row(5, 1, 5, 1, dosage="1 MG"), pkg(5, "ML", "1", "mg/mL"))
    assert e.factor == 1 and e.factor_source == "cms_items"
    assert codes(e)["cms_inconsistent"] == "penalty"


def test_inconsistent_billunits_settled_by_strength():
    e = estimate_factor(row(5, 1, 1, 20, dosage="10 MG"), pkg(5, "ML", "40", "mg/mL"))
    assert e.factor == 4 and e.factor_source == "cms_package"
    assert codes(e)["cms_inconsistent"] == "penalty"


def test_unsettled_conflict_is_edge():
    # multi-ingredient product: no strength cross-check possible
    e = estimate_factor(row(1, 10, 10, 1, dosage="1.5 GM"), pkg(10, "EA", "2; 1", "g/1; g/1"))
    assert codes(e)["factor_conflict"] == "edge"


def test_strength_far_off_is_edge():
    e = estimate_factor(row(1, 1, 0.1, 0.1, dosage="10 MG"), pkg(20, "ML", "1000", "mg/20mL"))
    assert codes(e)["strength_disagrees"] == "edge"


def test_strength_slightly_off_is_penalty():
    e = estimate_factor(row(1, 1, 3, 3, dosage="0.1 MG"), pkg(1, "ML", ".324", "mg/mL"))
    assert codes(e)["strength_disagrees"] == "penalty"


def test_no_fda_package():
    e = estimate_factor(row(0.5, 1, 1, 1, dosage="0.5 ML"), None)
    assert e.factor == 2 and e.ndc_unit is None
    assert codes(e)["ncpdp_unit_unknown"] == "penalty"


def test_unusable_row():
    e = estimate_factor(row(None, None, None, None), pkg(1, "ML"))
    assert e.factor is None and codes(e)["no_factor"] == "edge"


def test_candidates_dedupe_by_hcpcs():
    rows = [row(4, 1, 100, 100, hcpcs="J9271"), row(4, 1, 100, 100, hcpcs="j9271"), row(4, 1, 4, 4, "25 MG", "J1234")]
    assert [c.hcpcs for c in hcpcs_candidates(rows, pkg(4, "ML"))] == ["J9271", "J1234"]
