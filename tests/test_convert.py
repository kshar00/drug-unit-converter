"""End-to-end single-line conversion on a synthetic reference store (no network, no real DuckDB file)."""

from datetime import date

import pytest

from converter.convert import ClaimLine, PatientDataError, convert_line
from tests.store_builder import asp, build_store, nadac, product, xw

VIAL_DESC = "1 VIAL, SINGLE-DOSE in 1 CARTON (11111-0001-01)  / 4 mL in 1 VIAL, SINGLE-DOSE"


@pytest.fixture
def ref(tmp_path):
    products = [
        product("11111-0001-01", VIAL_DESC, name="Testumab", strength="25", strength_unit="mg/mL"),
        product("11111-0002-01", "1 VIAL in 1 CARTON (11111-0002-01)  / 10 mL in 1 VIAL", name="Powderex",
                form="INJECTION, POWDER, LYOPHILIZED, FOR SOLUTION", strength="100", strength_unit="mg/1"),
        product("22222-0003-30", "30 TABLET in 1 BOTTLE (22222-0003-30) ", name="Pillex", form="TABLET",
                strength="5", strength_unit="mg/1"),
        product("33333-0004-01", "1 KIT in 1 CARTON (33333-0004-01)  *  1 mL in 1 VIAL  *  1 mL in 1 SYRINGE",
                name="Kitrix", form="KIT"),
        product("44444-0005-01", "1 VIAL in 1 CARTON (44444-0005-01)  / 2 mL in 1 VIAL", name="Dualcode",
                strength="50", strength_unit="mg/mL"),
        product("55555-0006-01", "1 SYRINGE in 1 CARTON (55555-0006-01)  / 1 mL in 1 SYRINGE", name="Onetoone",
                strength="1", strength_unit="mg/mL"),
    ]
    crosswalk = [
        xw("11111-0001-01", "J9271", "1 MG", 4, 1, 100, 100, "Testumab"),
        xw("11111-0002-01", "J1745", "10 MG", 1, 1, 10, 10, "Powderex"),
        xw("33333-0004-01", "J7777", "1 MG", 1, 1, 1, 1, "Kitrix"),
        xw("44444-0005-01", "J1111", "10 MG", 2, 1, 10, 10, "Dualcode"),
        xw("44444-0005-01", "J2222", "1 MG", 2, 1, 100, 100, "Dualcode"),
        xw("55555-0006-01", "J3333", "1 MG", 1, 1, 1, 1, "Onetoone"),
    ]
    prices = [asp("J9271", 62.25), asp("J1745", 30, "10 MG"), asp("J1111", 40, "10 MG"), asp("J2222", 4)]
    nadacs = [
        nadac("11111-0001-01", 1500.00, "ML", "2026-09-01"),
        nadac("11111-0001-01", 1556.25, "ML", "2026-10-15"),
    ]
    store = build_store(tmp_path, products, crosswalk, prices, nadacs)
    yield store
    store.close()


# --- with financials (milestone 3) -------------------------------------------


def test_priced_hcpcs_line_is_high(ref):
    r = convert_line(ClaimLine(ndc="11111-0001-01", quantity=200, hcpcs="J9271", allowed_amount=12450), ref)
    assert (r.inferred_basis, r.confidence) == ("HCPCS", "High")
    assert r.price_summary[0]["reference"] == "ASP" and r.price_summary[0]["within_band"]


def test_priced_ndc_line_uses_nadac_for_dos(ref):
    line = ClaimLine(ndc="11111-0001-01", quantity=8, ingredient_cost=12000, date_of_service=date(2026, 9, 20))
    r = convert_line(line, ref)
    assert (r.inferred_basis, r.confidence) == ("NDC", "High")
    nadac_row = next(s for s in r.price_summary if s["reference"] == "NADAC")
    assert nadac_row["reference_price"] == 1500.00  # the September price, not October's


def test_dos_before_any_nadac(ref):
    r = convert_line(ClaimLine(ndc="11111-0001-01", quantity=8, ingredient_cost=12000, date_of_service=date(2025, 1, 1)), ref)
    assert any(i.code == "nadac_after_dos" for i in r.issues)


def test_fee_stripped_from_allowed(ref):
    r = convert_line(ClaimLine(ndc="11111-0001-01", quantity=200, allowed_amount=12450 + 150, fee=150), ref)
    assert r.financial_check.fee_stripped == 150
    assert r.price_summary[0]["implied_price"] == pytest.approx(62.25)


def test_price_conflict_is_low(ref):
    # 8 whole mL structurally, but priced like 8 HCPCS units
    r = convert_line(ClaimLine(ndc="11111-0001-01", quantity=8, allowed_amount=8 * 62.25), ref)
    assert (r.inferred_basis, r.confidence) == ("HCPCS", "Low")


def test_price_ranks_multiple_hcpcs(ref):
    # Dualcode 50 mg/mL, 2 mL vial: J1111 per 10 mg (5/mL), J2222 per 1 mg (50/mL)
    # 100 mg given: 10 x J1111 at 40 = 400, or 100 x J2222 at 4 = 400. Priced at J2222 x 100 units only:
    r = convert_line(ClaimLine(ndc="44444-0005-01", quantity=100, allowed_amount=400), ref)
    assert r.hcpcs == "J2222" and r.confidence == "Low"  # still an edge case, but ranked by price
    assert "price fit" in next(i.message for i in r.issues if i.code == "multiple_hcpcs")


def test_hcpcs_quantity(ref):
    r = convert_line(ClaimLine(ndc="11111-0001-01", quantity=200, hcpcs="J9271"), ref)
    assert r.ndc11 == "11111000101" and r.ndc_hyphenated == "11111-0001-01"
    assert r.ncpdp_unit == "ML" and r.package_size == 4
    assert r.hcpcs == "J9271" and r.hcpcs_dosage == "1 MG" and r.factor == 25
    assert r.inferred_basis == "HCPCS"
    assert (r.ndc_units, r.hcpcs_units) == (8, 200)
    assert r.confidence == "Medium"
    assert "Testumab" in r.product_description
    assert r.readings["NDC"].hcpcs_units == 5000  # the other reading is kept for display


def test_ndc_quantity_without_hcpcs(ref):
    r = convert_line(ClaimLine(ndc="11111000101", quantity=8), ref)
    assert r.inferred_basis == "NDC" and (r.ndc_units, r.hcpcs_units) == (8, 200)
    assert r.hcpcs == "J9271"  # single crosswalk candidate


def test_ambiguous_ten_digit_ndc(ref):
    r = convert_line(ClaimLine(ndc="1111100101", quantity=8), ref)
    assert r.ndc11 == "11111000101"
    assert any(i.code == "ndc_resolved" for i in r.issues)


def test_powder_vial_each(ref):
    r = convert_line(ClaimLine(ndc="11111-0002-01", quantity=40, hcpcs="J1745"), ref)
    assert r.ncpdp_unit == "EA" and r.factor == 10
    assert r.inferred_basis == "HCPCS" and r.ndc_units == 4


def test_factor_one_high_confidence(ref):
    r = convert_line(ClaimLine(ndc="55555-0006-01", quantity=1), ref)
    assert r.inferred_basis == "EITHER" and r.confidence == "High"
    assert (r.ndc_units, r.hcpcs_units) == (1, 1)


def test_no_crosswalk_gives_ndc_only_low(ref):
    r = convert_line(ClaimLine(ndc="22222-0003-30", quantity=30), ref)
    assert r.inferred_basis == "NDC" and r.ndc_units == 30 and r.hcpcs_units is None
    assert r.confidence == "Low" and "crosswalk" in r.reason


def test_no_crosswalk_with_hcpcs_leaves_basis_open(ref):
    r = convert_line(ClaimLine(ndc="22222-0003-30", quantity=30, hcpcs="J8499"), ref)
    assert r.inferred_basis is None and r.ndc_units is None and r.confidence == "Low"


def test_kit_is_low(ref):
    r = convert_line(ClaimLine(ndc="33333-0004-01", quantity=1), ref)
    assert r.confidence == "Low" and any(i.code == "kit" for i in r.issues)


def test_multiple_hcpcs_without_code_is_low(ref):
    r = convert_line(ClaimLine(ndc="44444-0005-01", quantity=10), ref)
    assert r.confidence == "Low"
    assert {c.hcpcs for c in r.candidates} == {"J1111", "J2222"}
    assert any(i.code == "multiple_hcpcs" for i in r.issues)


def test_multiple_hcpcs_with_code_uses_it(ref):
    r = convert_line(ClaimLine(ndc="44444-0005-01", quantity=100, hcpcs="J2222"), ref)
    assert r.hcpcs == "J2222" and r.factor == 50
    assert not any(i.code == "multiple_hcpcs" for i in r.issues)


def test_hcpcs_not_in_crosswalk_is_low(ref):
    r = convert_line(ClaimLine(ndc="11111-0001-01", quantity=200, hcpcs="J0000"), ref)
    assert r.confidence == "Low" and any(i.code == "hcpcs_not_in_crosswalk" for i in r.issues)
    assert r.hcpcs == "J9271"  # still converts with the crosswalk code, flagged


def test_unclassified_code_is_low(ref):
    r = convert_line(ClaimLine(ndc="11111-0001-01", quantity=200, hcpcs="J3490"), ref)
    assert r.confidence == "Low" and any(i.code == "unclassified_hcpcs" for i in r.issues)


@pytest.mark.parametrize("ndc", ["not-an-ndc", "99999-9999-99", "9999999999"])
def test_bad_or_unknown_ndc(ref, ndc):
    r = convert_line(ClaimLine(ndc=ndc, quantity=1), ref)
    assert r.confidence == "Low" and r.reason


def test_bad_quantity(ref):
    r = convert_line(ClaimLine(ndc="11111-0001-01", quantity=0), ref)
    assert r.confidence == "Low"


def test_dos_outside_quarter_noted(ref):
    r = convert_line(ClaimLine(ndc="11111-0001-01", quantity=200, date_of_service=date(2026, 3, 1)), ref)
    assert any(i.code == "dos_outside_quarter" for i in r.issues)
    assert r.confidence == "Medium"  # info only: factors rarely change between quarters


# --- input parsing / privacy -------------------------------------------------


def test_from_mapping():
    line = ClaimLine.from_mapping(
        {"claim_id": "SYN-1", "ndc": " 0006-3026-02 ", "quantity": "1,200", "hcpcs": "j9271",
         "modifiers": "JW, kp", "date_of_service": "2026-11-02", "allowed_amount": "$1,234.50",
         "billed_amount": "", "note": "ignored"}
    )
    assert line.ndc == "0006-3026-02" and line.quantity == 1200
    assert line.modifiers == ("JW", "KP") and line.date_of_service == date(2026, 11, 2)
    assert line.allowed_amount == 1234.5 and line.billed_amount is None


@pytest.mark.parametrize("col", ["patient_name", "Member ID", "DOB", "rx_number", "prescriber_npi", "first_name"])
def test_patient_columns_refused(col):
    with pytest.raises(PatientDataError):
        ClaimLine.from_mapping({"ndc": "0006-3026-02", "quantity": 1, col: "x"})


def test_required_fields():
    with pytest.raises(ValueError):
        ClaimLine.from_mapping({"ndc": "0006-3026-02"})


def test_best_guess_follows_billing_convention(ref):
    # Dualcode-free check on Testumab: 4 mL vial, 25 units/mL. 2 with a HCPCS code -> HCPCS guess;
    # without a HCPCS code -> NDC guess. Both always return both numbers.
    r = convert_line(ClaimLine(ndc="11111-0001-01", quantity=2, hcpcs="J9271"), ref)
    if r.basis_is_guess:
        assert r.inferred_basis == "HCPCS" and r.confidence == "Low"
    assert r.ndc_units is not None and r.hcpcs_units is not None
    r = convert_line(ClaimLine(ndc="11111-0001-01", quantity=2), ref)
    if r.basis_is_guess:
        assert r.inferred_basis == "NDC" and r.confidence == "Low"
    assert r.ndc_units is not None and r.hcpcs_units is not None


def test_best_guess_never_picks_an_impossible_reading(ref):
    # 2.5 can't be HCPCS units (not whole), so even with a HCPCS code the guess must be NDC
    from converter.convert import best_guess
    from converter.infer import decide
    from converter.crosswalk import FactorEstimate
    est = FactorEstimate("J9271", None, "1 MG", None, "ML", 25, 4, 4, 100, "cms")
    readings = decide(2.5, est).readings
    assert best_guess(ClaimLine(ndc="x", quantity=2.5, hcpcs="J9271"), readings) == "NDC"
