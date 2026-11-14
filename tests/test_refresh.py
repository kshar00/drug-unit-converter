import duckdb
import pytest

from converter.reference import ReferenceStore
from data import refresh as rf


# --- discovery -------------------------------------------------------------

CMS_PAGE = """
<a href="/files/zip/october-2026-medicare-part-b-payment-limit-files-final.zip">x</a>
<a href="/files/zip/october-2026-ndc-hcpcs-crosswalk-final.zip">x</a>
<a href="/files/zip/2026-2027-seasonal-vaccine-price-file.zip">x</a>
<a href="/files/zip/2026-2027-ndc-hcpcs-crosswalk-seasonal-vaccines.zip">x</a>
<a href="/files/zip/july-2026-ndc-hcpcs-crosswalk.zip">x</a>
<a href="/files/zip/october-2025-noc-pricing-final-file.zip">x</a>
"""


def test_discover_cms_urls_picks_newest_main_files():
    urls = rf.discover_cms_urls(CMS_PAGE)
    assert urls["crosswalk"] == "https://www.cms.gov/files/zip/october-2026-ndc-hcpcs-crosswalk-final.zip"
    assert urls["asp"] == "https://www.cms.gov/files/zip/october-2026-medicare-part-b-payment-limit-files-final.zip"


def test_discover_cms_skips_noc_and_vaccine_files():
    page = '<a href="/files/zip/october-2025-noc-pricing-final-file.zip"></a><a href="/files/zip/october-2025-asp-pricing-final-file.zip"></a>'
    assert rf.discover_cms_urls(page)["asp"].endswith("october-2025-asp-pricing-final-file.zip")


@pytest.mark.parametrize(
    "url, label",
    [
        ("https://x/files/zip/october-2026-ndc-hcpcs-crosswalk-final.zip", "2026-Q4"),
        ("https://x/files/zip/january-2025-asp-pricing-file.zip", "2025-Q1"),
        ("https://x/files/zip/apr2025nocpricingfile.zip", None),
    ],
)
def test_quarter_label(url, label):
    assert rf.quarter_label(url) == label


def test_pick_nadac_dataset_takes_latest_year():
    datasets = [
        {"identifier": "a", "title": "NADAC (National Average Drug Acquisition Cost) 2025"},
        {"identifier": "b", "title": "NADAC (National Average Drug Acquisition Cost) 2026"},
        {"identifier": "c", "title": "NADAC Comparison"},
    ]
    assert rf.pick_nadac_dataset(datasets) == ("b", "NADAC (National Average Drug Acquisition Cost) 2026")
    assert rf.pick_nadac_dataset([]) is None


def test_nadac_download_url():
    item = {"distribution": [{"data": {"downloadURL": "https://download.medicaid.gov/data/nadac-x.csv"}}]}
    assert rf.nadac_download_url(item) == "https://download.medicaid.gov/data/nadac-x.csv"
    assert rf.nadac_download_url({"distribution": [{"downloadURL": None}]}) is None


# --- parsers ---------------------------------------------------------------


def test_parse_fda_zip(raw_files):
    prod, pack = rf.parse_fda_zip(raw_files["fda"])
    assert len(prod) == 2 and len(pack) == 2
    vial = pack.set_index("ndc11").loc["11111000101"]
    assert (vial.pkg_qty, vial.ncpdp_unit, bool(vial.is_kit)) == (10, "ML", False)
    tabs = pack.set_index("ndc11").loc["02222033330"]
    assert tabs.ncpdp_unit == "EA"


def test_parse_crosswalk_zip(raw_files):
    df = rf.parse_crosswalk_zip(raw_files["crosswalk"], "2026-Q4")
    assert list(df.hcpcs) == ["J9999", "J8888"]
    assert set(df.ndc11) == {"11111000101"}
    assert df.drug_name.iat[0] == "Testumab"  # trailing space stripped
    assert df.bill_units_pkg.tolist() == [10.0, 1.0]
    assert (df.quarter == "2026-Q4").all()


def test_parse_asp_zip(raw_files):
    df = rf.parse_asp_zip(raw_files["asp"], "2026-Q4")
    assert list(df.hcpcs) == ["J9999", "J8888"]  # footnote rows dropped
    assert df.payment_limit.tolist() == [12.345, 1234.5]


# --- end-to-end load into DuckDB --------------------------------------------


@pytest.fixture
def db(tmp_path, raw_files):
    path = tmp_path / "ref.duckdb"
    with duckdb.connect(str(path)) as con:
        rf.load_fda(
            con,
            [(raw_files["fda"], rf.FDA_NDC_URL, "active"), (raw_files["fda_excluded"], rf.FDA_EXCLUDED_URL, "excluded")],
        )
        rf.load_crosswalk(con, raw_files["crosswalk"], "https://x/october-2026-ndc-hcpcs-crosswalk-final.zip")
        rf.load_asp(con, raw_files["asp"], "https://x/october-2026-medicare-part-b-payment-limit-files-final.zip")
        rf.load_nadac(con, raw_files["nadac"], "https://x/nadac.csv")
    return path


def test_tables_loaded(db):
    with duckdb.connect(str(db), read_only=True) as con:
        counts = dict(con.execute("SELECT source, rows FROM refresh_log").fetchall())
        assert counts == {"fda_active": 2, "fda_excluded": 1, "crosswalk": 2, "asp": 2, "nadac": 3}
        assert con.execute("SELECT count(*) FROM nadac_latest").fetchone()[0] == 2
        assert con.execute("SELECT quarter FROM asp_crosswalk LIMIT 1").fetchone()[0] == "2026-Q4"


def test_reference_store_lookups(db):
    with ReferenceStore(db) as ref:
        pkg = ref.package("11111000101")
        assert pkg["proprietary_name"] == "Testumab"
        assert (pkg["pkg_qty"], pkg["ncpdp_unit"]) == (10, "ML")

        assert [r["hcpcs"] for r in ref.crosswalk("11111000101")] == ["J8888", "J9999"]
        assert ref.asp_price("j9999")["payment_limit"] == pytest.approx(12.345)
        assert ref.nadac("02222033330")["nadac_per_unit"] == pytest.approx(0.55)  # latest price
        assert ref.package("33333044401")["source"] == "excluded"
        assert ref.package("99999999999") is None


def test_resolve_ambiguous_ten_digit(db):
    with ReferenceStore(db) as ref:
        # "1111100101" could be 01111100101, 11111000101 or 11111001001; only the 5-3-2 reading exists
        r = ref.resolve("1111100101")
        assert r.ndc11 == "11111000101"
        assert "fda_package" in r.found_in

        r = ref.resolve("5555555555")
        assert r.ndc11 is None and "no candidate" in r.reason

        r = ref.resolve("11111-0001-01")
        assert r.ndc11 == "11111000101" and r.found_in == ["fda_package", "asp_crosswalk"]


def test_missing_db_has_helpful_error(tmp_path):
    with pytest.raises(FileNotFoundError, match="data.refresh"):
        ReferenceStore(tmp_path / "nope.duckdb")


def test_search_products(db):
    with ReferenceStore(db) as ref:
        hits = ref.search_products("testu")
        assert [h["ndc11"] for h in hits] == ["11111000101"]
        assert hits[0]["hcpcs"] == "J8888, J9999" and hits[0]["proprietary_name"] == "Testumab"
        assert [h["ndc11"] for h in ref.search_products("j9999")] == ["11111000101"]  # by HCPCS code
        assert ref.search_products("pill")[0]["hcpcs"] is None  # found, but not in the crosswalk
        assert ref.search_products("oldrug")[0]["source"] == "excluded"
        assert ref.search_products("ab") == []  # too short
        assert ref.search_products("nothing like this") == []
