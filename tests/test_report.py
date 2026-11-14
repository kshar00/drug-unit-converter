import duckdb
import pandas as pd
import pytest

from converter.aggregate import convert_lines
from converter.convert import PatientDataError
from converter.reference import ReferenceStore
from converter.report import check_columns, group_rows, line_rows, lines_from_dataframe, template_csv
from data import build_app_store, refresh as rf
from tests.store_builder import asp, build_store, product, xw

BEV = "50242-0060-01"


@pytest.fixture
def ref(tmp_path):
    store = build_store(
        tmp_path,
        [product(BEV, "1 VIAL, SINGLE-DOSE in 1 CARTON (50242-060-01)  / 4 mL in 1 VIAL, SINGLE-DOSE",
                 name="Bevtest", strength="25", strength_unit="mg/mL")],
        [xw(BEV, "J9035", "10 MG", 4, 1, 10, 10, "Bevtest")],
        [asp("J9035", 75.49, "10 MG")],
    )
    yield store
    store.close()


def test_header_aliases_and_ignored_columns():
    df = pd.DataFrame({"Claim Number": ["C1"], "NDC Code": [BEV], "Qty": ["35"], "HCPCS_Code": ["J9035"],
                       "Mods": ["JW"], "DOS": ["2026-11-05"], "Allowed": ["$2,642.15"], "Notes": ["x"]})
    lines, errors, ignored = lines_from_dataframe(df)
    assert not errors and ignored == ["Notes"]
    (l,) = lines
    assert (l.claim_id, l.ndc, l.quantity, l.hcpcs, l.modifiers, l.allowed_amount) == ("C1", BEV, 35, "J9035", ("JW",), 2642.15)


@pytest.mark.parametrize("col", ["Patient Name", "member_id", "DOB", "Rx Number", "Prescriber NPI"])
def test_patient_columns_refused(col):
    with pytest.raises(PatientDataError):
        check_columns(["ndc", "quantity", col])


def test_missing_required_columns():
    with pytest.raises(ValueError, match="NDC"):
        lines_from_dataframe(pd.DataFrame({"hcpcs": ["J9035"], "quantity": ["1"]}))


def test_bad_rows_reported_not_fatal():
    df = pd.DataFrame({"ndc": [BEV, BEV, ""], "quantity": ["35", "abc", "1"], "date_of_service": ["", "", ""]})
    lines, errors, _ = lines_from_dataframe(df)
    assert len(lines) == 1 and [e["row"] for e in errors] == [2, 3]


def test_result_tables(ref):
    df = pd.DataFrame({"claim_id": ["C1", "C1"], "ndc": [BEV, BEV], "quantity": ["35", "5"],
                       "hcpcs": ["J9035", "J9035"], "modifiers": ["", "JW"], "date_of_service": ["2026-11-05"] * 2,
                       "allowed_amount": ["2642.15", "377.45"]})
    lines, _, _ = lines_from_dataframe(df)
    groups = convert_lines(lines, ref)
    out = group_rows(groups)
    assert len(out) == 1
    row = out.iloc[0]
    assert (row.inferred_basis, row.hcpcs_units, row.ndc_units, row.confidence, row.lines_combined) == ("HCPCS", 40, 16, "High", 2)
    assert row.asp_variance_pct == pytest.approx(0, abs=0.5)
    per_line = line_rows(groups)
    assert per_line["role"].tolist() == ["administered", "waste"]
    assert per_line["ndc_units"].tolist() == [14, 2]


def test_template_has_required_columns():
    header = template_csv().strip().split(",")
    assert "ndc" in header and "quantity" in header


def test_slim_store_round_trip(tmp_path, raw_files):
    full = tmp_path / "full.duckdb"
    with duckdb.connect(str(full)) as con:
        rf.load_fda(con, [(raw_files["fda"], rf.FDA_NDC_URL, "active")])
        rf.load_crosswalk(con, raw_files["crosswalk"], "https://x/october-2026-ndc-hcpcs-crosswalk-final.zip")
        rf.load_asp(con, raw_files["asp"], "https://x/october-2026-medicare-part-b-payment-limit-files-final.zip")
        rf.load_nadac(con, raw_files["nadac"], "https://x/nadac.csv")
    out_dir = tmp_path / "app"
    counts = build_app_store.export(full, out_dir)
    assert counts["nadac"] == 3 and counts["fda_package"] == 2
    built = build_app_store.materialize(tmp_path / "built.duckdb", out_dir)
    with ReferenceStore(built) as ref, ReferenceStore(full) as ref_full:
        for ndc in ("11111000101", "02222033330"):
            slim, fat = ref.package(ndc), ref_full.package(ndc)
            assert (slim["pkg_qty"], slim["ncpdp_unit"], slim["proprietary_name"]) == (fat["pkg_qty"], fat["ncpdp_unit"], fat["proprietary_name"])
        assert ref.crosswalk("11111000101") == ref_full.crosswalk("11111000101")
        assert ref.nadac("02222033330")["nadac_per_unit"] == ref_full.nadac("02222033330")["nadac_per_unit"]
