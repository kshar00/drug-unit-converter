"""Claim unit of measure as a hint about the quantity's unit basis."""

from datetime import date

import pandas as pd
import pytest

from converter.aggregate import convert_lines
from converter.convert import ClaimLine, convert_line
from converter.report import group_rows, lines_from_dataframe
from converter.uom import normalize_uom, uom_hint
from tests.store_builder import asp, build_store, product, xw

BEV = "50242-0060-01"  # 100 mg / 4 mL vial, J9035 per 10 mg: 35 units vs 35 mL is a structural tie
POWDER = "57894-0030-01"  # powder vial, billed EA


@pytest.fixture
def ref(tmp_path):
    store = build_store(
        tmp_path,
        [
            product(BEV, "1 VIAL, SINGLE-DOSE in 1 CARTON (50242-060-01)  / 4 mL in 1 VIAL, SINGLE-DOSE",
                    name="Bevtest", strength="25", strength_unit="mg/mL"),
            product(POWDER, "1 VIAL, SINGLE-USE in 1 BOX (57894-030-01)  / 10 mL in 1 VIAL, SINGLE-USE",
                    name="Powdertest", form="INJECTION, POWDER, LYOPHILIZED, FOR SOLUTION", strength="100", strength_unit="mg/1"),
        ],
        [xw(BEV, "J9035", "10 MG", 4, 1, 10, 10, "Bevtest"), xw(POWDER, "J1745", "10 MG", 1, 1, 10, 10, "Powdertest")],
        [asp("J9035", 75.49, "10 MG"), asp("J1745", 29.86, "10 MG")],
    )
    yield store
    store.close()


def codes(r):
    return {i.code: i.severity for i in r.issues}


@pytest.mark.parametrize("raw, expected", [
    ("ml", "ML"), ("Each", "EA"), ("gr", "GM"), ("GM", "GM"), ("mg", "ME"), ("IU", "F2"), ("un", "UN"),
    ("", None), (None, None), ("  ", None), ("xx", "XX"),
])
def test_normalize(raw, expected):
    assert normalize_uom(raw) == expected


def test_hints():
    assert uom_hint("ML", "ML", "10 MG")[0] == "NDC"
    assert uom_hint("EA", "EA", "10 MG")[0] == "NDC"
    hint, issues = uom_hint("ML", "EA", "10 MG")
    assert hint is None and issues[0].code == "uom_mismatch" and issues[0].severity == "penalty"
    hint, issues = uom_hint("UN", "ML", "1 MG")
    assert hint is None and issues[0].code == "uom_un"
    assert uom_hint("ME", "ML", "1 MG")[0] == "HCPCS"
    assert uom_hint("ME", "ML", "10 MG")[0] is None
    assert uom_hint("F2", "ML", "1 IU")[0] == "HCPCS"
    assert uom_hint("ML", None, "1 MG")[1][0].code == "uom_unchecked"
    assert uom_hint("XX", "ML", "1 MG")[1][0].code == "uom_unrecognised"
    assert uom_hint(None, "ML", "1 MG") == (None, [])


def test_uom_breaks_structural_tie(ref):
    without = convert_line(ClaimLine(ndc=BEV, quantity=35, hcpcs="J9035"), ref)
    assert without.inferred_basis is None
    r = convert_line(ClaimLine(ndc=BEV, quantity=35, hcpcs="J9035", unit_of_measure="ML"), ref)
    assert (r.inferred_basis, r.ndc_units, r.confidence) == ("NDC", 35, "Medium")
    assert "uom_matches" in codes(r)


def test_milligram_uom_with_one_mg_descriptor_does_not_apply_to_10mg(ref):
    r = convert_line(ClaimLine(ndc=BEV, quantity=35, hcpcs="J9035", unit_of_measure="ME"), ref)
    assert r.inferred_basis is None and "uom_no_hint" in codes(r)


def test_uom_cannot_overrule_price(ref):
    # claim says ML, but the allowed amount is priced as 35 HCPCS units
    r = convert_line(ClaimLine(ndc=BEV, quantity=35, hcpcs="J9035", unit_of_measure="ML", allowed_amount=35 * 75.49), ref)
    assert r.inferred_basis == "HCPCS" and r.confidence == "Low"
    assert codes(r)["uom_conflict"] == "penalty"


def test_uom_mismatch_with_ncpdp_unit(ref):
    # ML reported on a powder vial that NCPDP bills EA
    r = convert_line(ClaimLine(ndc=POWDER, quantity=4, hcpcs="J1745", unit_of_measure="ML"), ref)
    assert r.ncpdp_unit == "EA" and codes(r)["uom_mismatch"] == "penalty"
    assert r.inferred_basis == "NDC" and r.confidence == "Low"


def test_uom_column_aliases_and_output(ref):
    df = pd.DataFrame({"NDC": [BEV], "Qty": ["35"], "UOM": ["ml"], "HCPCS": ["J9035"]})
    lines, errors, ignored = lines_from_dataframe(df)
    assert not errors and not ignored and lines[0].unit_of_measure == "ML"
    out = group_rows(convert_lines(lines, ref))
    assert out.iloc[0].unit_of_measure == "ML" and out.iloc[0].inferred_basis == "NDC"


def test_mixed_uoms_in_group(ref):
    dos = date(2026, 11, 5)
    lines = [
        ClaimLine(ndc=BEV, quantity=35, hcpcs="J9035", claim_id="C1", date_of_service=dos, unit_of_measure="ML"),
        ClaimLine(ndc=BEV, quantity=5, hcpcs="J9035", claim_id="C1", date_of_service=dos, modifiers=("JW",), unit_of_measure="UN"),
    ]
    (g,) = convert_lines(lines, ref)
    assert codes(g.result)["uom_mixed"] == "penalty"


def test_ea_on_liquid_offers_whole_item_reading(ref):
    # 1 EA of a 4 mL vial billed in ML: most likely one vial = 4 mL = 10 J9035 units
    r = convert_line(ClaimLine(ndc=BEV, quantity=1, hcpcs="J9035", unit_of_measure="EA"), ref)
    msg = next(i.message for i in r.issues if i.code == "uom_ea_items")
    assert "4 ML = 10 J9035 units" in msg
    assert msg in r.reasons  # shown with the reasons, not just in the flags
