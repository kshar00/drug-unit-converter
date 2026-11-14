"""Modifier aggregation on synthetic multi-line claims."""

from datetime import date

import pytest

from converter.aggregate import convert_lines, group_lines, role
from converter.convert import ClaimLine
from tests.store_builder import asp, build_store, product, xw

DOS = date(2026, 11, 5)
BEV = "50242-0060-01"  # bevacizumab-like: 100 mg / 4 mL single-dose vial, J9035 per 10 mg (2.5 units/mL)
EYE = "61755-0005-02"  # aflibercept-like: 2 mg / 0.05 mL single-dose vial, J0178 per 1 mg (40 units/mL)
COMBO = "00487-9801-30"  # albuterol 2.5 mg + ipratropium 0.5 mg per 3 mL vial, 30 vials


@pytest.fixture
def ref(tmp_path):
    products = [
        product(BEV, "1 VIAL, SINGLE-DOSE in 1 CARTON (50242-060-01)  / 4 mL in 1 VIAL, SINGLE-DOSE",
                name="Bevtest", strength="25", strength_unit="mg/mL"),
        product(EYE, "1 VIAL, SINGLE-DOSE in 1 CARTON (61755-005-02)  / .05 mL in 1 VIAL, SINGLE-DOSE",
                name="Eyetest", strength="40", strength_unit="mg/mL"),
        product(COMBO, "30 VIAL in 1 CARTON (0487-9801-30)  / 3 mL in 1 VIAL", name="Combotest",
                form="SOLUTION", strength="2.5; .5", strength_unit="mg/3mL; mg/3mL"),
    ]
    crosswalk = [
        xw(BEV, "J9035", "10 MG", 4, 1, 10, 10, "Bevtest"),
        xw(EYE, "J0178", "1 MG", 0.05, 1, 2, 2, "Eyetest"),
        xw(COMBO, "J7613", "1 MG", 3, 30, 2.5, 75, "Combotest"),
        xw(COMBO, "J7644", "1 MG", 3, 30, 0.5, 15, "Combotest"),
    ]
    prices = [asp("J9035", 75.49, "10 MG"), asp("J0178", 900.0), asp("J7613", 0.05), asp("J7644", 0.25)]
    store = build_store(tmp_path, products, crosswalk, prices)
    yield store
    store.close()


def line(q, hcpcs="J9035", mods=(), ndc=BEV, claim="C1", **kw):
    return ClaimLine(ndc=ndc, quantity=q, hcpcs=hcpcs, modifiers=tuple(mods), date_of_service=DOS, claim_id=claim, **kw)


def codes(result):
    return {i.code for i in result.issues}


# --- grouping ------------------------------------------------------------------


def test_roles():
    assert role(line(5, mods=["JW"])) == "waste"
    assert role(line(35, mods=["JZ"])) == "administered"
    assert role(line(2, mods=["RT"])) == "RT"
    assert role(line(2, mods=["LT", "KX"])) == "LT"


def test_grouping_keys():
    lines = [
        line(35), line(5, mods=["JW"]),            # same group
        line(35, claim="C2"),                       # other claim
        line(35, ndc="50242006001"),                # same NDC in 11-digit form: same group
        line(35, ndc="5024206001"),                 # ambiguous 10-digit: can't be keyed safely, own group
        line(35, hcpcs="J9999"),                    # other HCPCS
        ClaimLine(ndc=BEV, quantity=35, hcpcs="J9035"),  # no claim ID: stands alone
    ]
    sizes = [len(members) for _, members in group_lines(lines)]
    assert sizes == [3, 1, 1, 1, 1]


# --- JW / JZ -------------------------------------------------------------------


def test_jw_rolls_up_and_breaks_down(ref):
    (g,) = convert_lines([line(35, allowed_amount=2642.15), line(5, mods=["JW"], allowed_amount=377.45)], ref)
    r = g.result
    assert g.aggregated and r.line.quantity == 40 and r.line.allowed_amount == pytest.approx(3019.60)
    assert (r.inferred_basis, r.hcpcs_units, r.ndc_units, r.confidence) == ("HCPCS", 40, 16, "High")
    assert [(s.role, s.hcpcs_units, s.ndc_units) for s in g.lines] == [("administered", 35, 14), ("waste", 5, 2)]
    assert "aggregated" in codes(r)


def test_jw_decides_without_prices(ref):
    (g,) = convert_lines([line(35), line(5, mods=["JW"])], ref)
    assert (g.result.inferred_basis, g.result.confidence) == ("HCPCS", "Medium")
    assert "JW waste is 0.5 of one item" in g.result.reasons[0]


def test_same_quantity_without_jw_is_undecided(ref):
    (g,) = convert_lines([line(35)], ref)
    assert g.result.inferred_basis is None


def test_jw_and_jz_is_low(ref):
    (g,) = convert_lines([line(35, mods=["JZ"]), line(5, mods=["JW"])], ref)
    assert g.result.confidence == "Low" and "jw_and_jz" in codes(g.result)


def test_waste_only_is_low(ref):
    (g,) = convert_lines([line(5, mods=["JW"])], ref)
    assert g.result.confidence == "Low" and "waste_only" in codes(g.result)


def test_jz_with_partial_vial_is_penalised(ref):
    (g,) = convert_lines([line(35, mods=["JZ"], allowed_amount=35 * 75.49)], ref)
    assert "jz_partial_vial" in codes(g.result)
    assert g.result.confidence == "Low"  # Medium (price only) dropped one level


def test_jz_with_whole_vials_is_fine(ref):
    (g,) = convert_lines([line(40, mods=["JZ"], allowed_amount=40 * 75.49)], ref)
    assert "jz_partial_vial" not in codes(g.result)


def test_missing_waste_modifier_noted(ref):
    (g,) = convert_lines([line(35, allowed_amount=35 * 75.49)], ref)
    assert "waste_modifier_missing" in codes(g.result)


def test_duplicate_administered_lines(ref):
    (g,) = convert_lines([line(20, allowed_amount=20 * 75.49), line(20, allowed_amount=20 * 75.49)], ref)
    assert "duplicate_lines" in codes(g.result)


def test_partial_amounts_not_used(ref):
    (g,) = convert_lines([line(35, allowed_amount=2642.15), line(5, mods=["JW"])], ref)
    assert g.result.line.allowed_amount is None
    assert "partial_amounts" in codes(g.result)


# --- RT / LT -------------------------------------------------------------------


def test_laterality_rolls_up(ref):
    lines = [line(2, "J0178", ["RT"], EYE, allowed_amount=1800), line(2, "J0178", ["LT"], EYE, allowed_amount=1800)]
    (g,) = convert_lines(lines, ref)
    r = g.result
    assert r.line.quantity == 4 and r.inferred_basis == "HCPCS"
    assert r.ndc_units == pytest.approx(0.1)
    assert [(s.role, s.hcpcs_units, s.ndc_units) for s in g.lines] == [("RT", 2, 0.05), ("LT", 2, 0.05)]


def test_duplicate_side(ref):
    lines = [line(2, "J0178", ["RT"], EYE), line(2, "J0178", ["RT"], EYE)]
    (g,) = convert_lines(lines, ref)
    assert "duplicate_side" in codes(g.result)


# --- KP / KQ -------------------------------------------------------------------


def test_kp_kq_linked_and_consistent(ref):
    lines = [
        line(75, "J7613", ["KP"], COMBO, allowed_amount=75 * 0.05),
        line(15, "J7644", ["KQ"], COMBO, allowed_amount=15 * 0.25),
    ]
    kp, kq = convert_lines(lines, ref)
    assert kp.related == [kq.key] and kq.related == [kp.key]
    # albuterol's factor (0.83/mL) is too close to 1 to call on its own; the KQ partner settles it
    assert "resolved_by_partner" in codes(kp.result)
    assert kp.result.ndc_units == pytest.approx(90, rel=1e-4) and kq.result.ndc_units == pytest.approx(90, rel=1e-4)
    assert kp.result.inferred_basis == "HCPCS" and kp.lines[0].ndc_units == pytest.approx(90, rel=1e-4)
    assert "multi_drug_consistent" in codes(kp.result)


def test_kp_kq_quantity_mismatch(ref):
    lines = [
        line(75, "J7613", ["KP"], COMBO, allowed_amount=75 * 0.05),
        line(10, "J7644", ["KQ"], COMBO, allowed_amount=10 * 0.25),
    ]
    kp, kq = convert_lines(lines, ref)
    assert "multi_drug_mismatch" in codes(kp.result) and "multi_drug_mismatch" in codes(kq.result)


def test_kp_without_kq(ref):
    (kp,) = convert_lines([line(75, "J7613", ["KP"], COMBO)], ref)
    assert "multi_drug_unpaired" in codes(kp.result) and kp.related == []
