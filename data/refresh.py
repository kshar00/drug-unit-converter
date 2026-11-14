"""Download the public reference files and load them into DuckDB.

Sources
  fda        FDA NDC Directory (ndctext.zip, plus ndc_excluded.zip for delisted NDCs seen on older claims)
  crosswalk  CMS ASP NDC-HCPCS crosswalk (quarterly zip; we read the "section 508" CSV inside)
  asp        CMS Medicare Part B payment limit file (quarterly zip; "section 508" CSV)
  nadac      Medicaid NADAC (current-year dataset on data.medicaid.gov, refreshed weekly)

Usage
  python -m data.refresh                        # all sources, latest files
  python -m data.refresh --sources fda nadac    # only some sources
  python -m data.refresh --offline              # reuse files already in data/raw
  python -m data.refresh --crosswalk-url URL    # pin a specific quarter (also --asp-url, --nadac-url)

Downloaded files are kept in data/raw/<source>/ with a manifest.json recording where they came from.
Every load is recorded in the refresh_log table.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlparse

import duckdb
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # allow `python data/refresh.py` as well as `python -m data.refresh`
    sys.path.insert(0, str(ROOT))

from converter.ndc import NDCFormatError, normalize_ndc, parse_package_description  # noqa: E402

DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
DB_PATH = DATA_DIR / "reference.duckdb"

FDA_NDC_URL = "https://www.accessdata.fda.gov/cder/ndctext.zip"
FDA_EXCLUDED_URL = "https://www.accessdata.fda.gov/cder/ndc_excluded.zip"
CMS_ASP_PAGE = "https://www.cms.gov/medicare/payment/part-b-drugs/asp-pricing-files"
MEDICAID_DATASETS = "https://data.medicaid.gov/api/1/metastore/schemas/dataset/items"

USER_AGENT = "drug-unit-converter-refresh/0.1"
TIMEOUT = 120
ALL_SOURCES = ("fda", "crosswalk", "asp", "nadac")


# ---------------------------------------------------------------------------
# Discovery: the CMS and NADAC URLs change every quarter / year
# ---------------------------------------------------------------------------

_CMS_ZIP = re.compile(r'href="([^"]+\.zip)"', re.IGNORECASE)
_QUARTER = re.compile(r"(january|april|july|october)-(\d{4})", re.IGNORECASE)
_CROSSWALK_EXCLUDE = ("seasonal", "vaccine", "opps", "awp", "prep")
_ASP_EXCLUDE = ("noc", "seasonal", "vaccine", "not-payable")


def quarter_label(url: str) -> str | None:
    """'.../october-2026-ndc-hcpcs-crosswalk-final.zip' -> '2026-Q4'."""
    m = _QUARTER.search(url)
    if not m:
        return None
    q = {"january": 1, "april": 2, "july": 3, "october": 4}[m.group(1).lower()]
    return f"{m.group(2)}-Q{q}"


def discover_cms_urls(html: str, base: str = CMS_ASP_PAGE) -> dict[str, str]:
    """Pick the newest quarterly crosswalk and payment-limit zips from the CMS ASP pricing page.

    The page lists quarters newest first, so the first matching link wins.
    """
    found: dict[str, str] = {}
    for href in _CMS_ZIP.findall(html):
        name = href.lower().rsplit("/", 1)[-1]
        if "crosswalk" not in found and "crosswalk" in name and not any(x in name for x in _CROSSWALK_EXCLUDE):
            found["crosswalk"] = urljoin(base, href)
        if (
            "asp" not in found
            and ("payment-limit" in name or "asp-pricing" in name)
            and not any(x in name for x in _ASP_EXCLUDE)
        ):
            found["asp"] = urljoin(base, href)
    return found


_NADAC_TITLE = re.compile(r"^NADAC \(National Average Drug Acquisition Cost\) (\d{4})$")


def pick_nadac_dataset(datasets: list[dict]) -> tuple[str, str] | None:
    """Return (identifier, title) of the newest yearly NADAC dataset."""
    best: tuple[int, str, str] | None = None
    for d in datasets:
        m = _NADAC_TITLE.match(d.get("title", "").strip())
        if m and (best is None or int(m.group(1)) > best[0]):
            best = (int(m.group(1)), d["identifier"], d["title"])
    return (best[1], best[2]) if best else None


def nadac_download_url(item: dict) -> str | None:
    """Pull the CSV download URL out of a data.medicaid.gov dataset item (show-reference-ids=true)."""
    for dist in item.get("distribution", []):
        data = dist.get("data", dist)
        url = data.get("downloadURL")
        if url and url.lower().endswith(".csv"):
            return url
    return None


# ---------------------------------------------------------------------------
# Download helpers
# ---------------------------------------------------------------------------


def _session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


def download(url: str, dest_dir: Path, session: requests.Session) -> Path:
    """Stream a file into dest_dir and record it in dest_dir/manifest.json."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = Path(urlparse(url).path).name or "download"
    dest = dest_dir / name
    tmp = dest.with_suffix(dest.suffix + ".part")
    print(f"  downloading {url}")
    with session.get(url, stream=True, timeout=TIMEOUT) as r:
        r.raise_for_status()
        with open(tmp, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
    tmp.replace(dest)
    manifest = _read_manifest(dest_dir)
    manifest[url] = {"file": name, "downloaded_at": _now()}
    manifest["_latest"] = url
    (dest_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return dest


def _read_manifest(dest_dir: Path) -> dict:
    p = dest_dir / "manifest.json"
    return json.loads(p.read_text()) if p.exists() else {}


def cached_file(dest_dir: Path, url: str | None = None) -> tuple[Path, str]:
    """Offline mode: find a previously downloaded file (the given URL's, or the latest)."""
    manifest = _read_manifest(dest_dir)
    url = url or manifest.get("_latest")
    if not url or url not in manifest:
        raise FileNotFoundError(f"no cached download in {dest_dir}; run without --offline first")
    return dest_dir / manifest[url]["file"], url


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Parsers: path in, DataFrame out (pure, so tests can use small synthetic files)
# ---------------------------------------------------------------------------


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _zip_member(z: zipfile.ZipFile, predicate: Callable[[str], bool], what: str) -> str:
    names = [n for n in z.namelist() if predicate(n.lower())]
    if not names:
        raise ValueError(f"no {what} file found in zip; members: {z.namelist()}")
    return names[0]


def _fda_date(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s.str.strip(), format="%Y%m%d", errors="coerce").dt.date


def _read_fda_table(z: zipfile.ZipFile, member: str) -> pd.DataFrame:
    text = _decode(z.read(member))
    df = pd.read_csv(
        io.StringIO(text), sep="\t", dtype=str, keep_default_na=False, quoting=csv.QUOTE_NONE
    )
    df.columns = [c.strip().upper() for c in df.columns]
    return df


_PRODUCT_COLUMNS = {
    "PRODUCTID": "product_id",
    "PRODUCTNDC": "product_ndc",
    "PRODUCTTYPENAME": "product_type",
    "PROPRIETARYNAME": "proprietary_name",
    "PROPRIETARYNAMESUFFIX": "proprietary_name_suffix",
    "NONPROPRIETARYNAME": "nonproprietary_name",
    "DOSAGEFORMNAME": "dosage_form",
    "ROUTENAME": "route",
    "STARTMARKETINGDATE": "start_marketing_date",
    "ENDMARKETINGDATE": "end_marketing_date",
    "MARKETINGCATEGORYNAME": "marketing_category",
    "APPLICATIONNUMBER": "application_number",
    "LABELERNAME": "labeler_name",
    "SUBSTANCENAME": "substance_name",
    "ACTIVE_NUMERATOR_STRENGTH": "strength",
    "ACTIVE_INGRED_UNIT": "strength_unit",
    "PHARM_CLASSES": "pharm_classes",
    "DEASCHEDULE": "dea_schedule",
}

_PACKAGE_COLUMNS = {
    "PRODUCTID": "product_id",
    "PRODUCTNDC": "product_ndc",
    "NDCPACKAGECODE": "package_ndc",
    "PACKAGEDESCRIPTION": "package_description",
    "STARTMARKETINGDATE": "start_marketing_date",
    "ENDMARKETINGDATE": "end_marketing_date",
    "SAMPLE_PACKAGE": "sample_package",
}


def _safe_ndc11(raw: str) -> str | None:
    try:
        return normalize_ndc(raw).ndc11
    except NDCFormatError:
        return None


def parse_fda_zip(
    path: Path, source: str = "active", extra_forms: pd.Series | None = None
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Parse an FDA NDC Directory zip into (products, packages).

    extra_forms (product_id -> dosage form) covers packages whose product row is in another file:
    some delisted packages in ndc_excluded.zip belong to products still in ndctext.zip.
    """
    with zipfile.ZipFile(path) as z:
        prod = _read_fda_table(z, _zip_member(z, lambda n: "product" in n and n.endswith(".txt"), "product"))
        pack = _read_fda_table(z, _zip_member(z, lambda n: "package" in n and n.endswith(".txt"), "package"))

    prod = prod[[c for c in _PRODUCT_COLUMNS if c in prod.columns]].rename(columns=_PRODUCT_COLUMNS)
    for c in ("start_marketing_date", "end_marketing_date"):
        prod[c] = _fda_date(prod[c])
    prod["source"] = source

    pack = pack[[c for c in _PACKAGE_COLUMNS if c in pack.columns]].rename(columns=_PACKAGE_COLUMNS)
    for c in ("start_marketing_date", "end_marketing_date"):
        pack[c] = _fda_date(pack[c])
    pack["ndc11"] = pack["package_ndc"].map(_safe_ndc11)
    form_map = prod.drop_duplicates("product_id").set_index("product_id")["dosage_form"]
    if extra_forms is not None:
        form_map = form_map.combine_first(extra_forms)
    forms = pack["product_id"].map(form_map)
    parsed = [parse_package_description(d, f) for d, f in zip(pack["package_description"], forms)]
    parsed = pd.Series(parsed, index=pack.index)
    pack["pkg_qty"] = parsed.map(lambda p: p.package_quantity)
    pack["ncpdp_unit"] = parsed.map(lambda p: p.ncpdp_unit)
    pack["is_kit"] = parsed.map(lambda p: p.is_kit)
    pack["parse_notes"] = parsed.map(lambda p: "; ".join(p.notes) or None)
    pack["parse_warnings"] = parsed.map(lambda p: "; ".join(p.warnings) or None)
    pack["source"] = source
    return prod, pack


def _read_cms_csv(path: Path, member_predicate: Callable[[str], bool], header_predicate: Callable[[str], bool], what: str) -> pd.DataFrame:
    """CMS 508 CSVs have a few lines of title/notes above the real header row."""
    with zipfile.ZipFile(path) as z:
        member = _zip_member(z, member_predicate, what)
        lines = _decode(z.read(member)).splitlines()
    header_idx = next((i for i, line in enumerate(lines) if header_predicate(line)), None)
    if header_idx is None:
        raise ValueError(f"header row not found in {member}")
    df = pd.read_csv(io.StringIO("\n".join(lines[header_idx:])), dtype=str, keep_default_na=False)
    df.columns = [re.sub(r"\s+", " ", str(c)).strip().upper() for c in df.columns]
    df = df.loc[:, [c for c in df.columns if c and not c.startswith("UNNAMED")]]
    return df.apply(lambda s: s.str.strip())


def _to_float(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s.str.replace(",", "", regex=False).str.replace("$", "", regex=False), errors="coerce")


def _is_crosswalk_csv(name: str) -> bool:
    return (
        name.endswith(".csv")
        and "crosswalk" in name
        and "asp" in name
        and not any(x in name for x in ("awp", "opps", "prep", "introduction"))
    )


def parse_crosswalk_zip(path: Path, quarter: str | None) -> pd.DataFrame:
    df = _read_cms_csv(
        path,
        _is_crosswalk_csv,
        lambda line: bool(re.match(r'^"?_?\d{4}_CODE"?,', line.strip(), re.IGNORECASE))
        or line.upper().startswith("HCPCS"),
        "ASP NDC-HCPCS crosswalk CSV",
    )
    code_col = next(c for c in df.columns if c.endswith("CODE") or c.startswith("HCPCS"))
    ndc_col = next(c for c in df.columns if c in ("NDC", "NDC2", "NDC 11"))
    out = pd.DataFrame(
        {
            "hcpcs": df[code_col].str.upper(),
            "short_description": df.get("SHORT DESCRIPTION"),
            "labeler_name": df.get("LABELER NAME"),
            "ndc_raw": df[ndc_col],
            "drug_name": df.get("DRUG NAME"),
            "hcpcs_dosage": df.get("HCPCS DOSAGE"),
            "pkg_size": _to_float(df["PKG SIZE"]),
            "pkg_qty": _to_float(df["PKG QTY"]),
            "bill_units": _to_float(df["BILLUNITS"]),
            "bill_units_pkg": _to_float(df["BILLUNITSPKG"]),
        }
    )
    out = out[(out["hcpcs"] != "") & (out["ndc_raw"] != "")]
    out.insert(3, "ndc11", out["ndc_raw"].map(_safe_ndc11))
    out["quarter"] = quarter
    return out.reset_index(drop=True)


def _is_payment_limit_csv(name: str) -> bool:
    if not name.endswith(".csv") or "noc" in name or "not payable" in name:
        return False
    return "payment limit" in name or "asp pricing" in name


def parse_asp_zip(path: Path, quarter: str | None) -> pd.DataFrame:
    df = _read_cms_csv(
        path,
        _is_payment_limit_csv,
        lambda line: line.strip().strip('"').upper().startswith("HCPCS CODE"),
        "Part B payment limit CSV",
    )
    out = pd.DataFrame(
        {
            "hcpcs": df["HCPCS CODE"].str.upper(),
            "short_description": df.get("SHORT DESCRIPTION"),
            "hcpcs_dosage": df.get("HCPCS CODE DOSAGE"),
            "payment_limit": _to_float(df["PAYMENT LIMIT"]),
            "coinsurance_pct": _to_float(df["CO-INSURANCE PERCENTAGE"]) if "CO-INSURANCE PERCENTAGE" in df else None,
            "notes": df.get("NOTES"),
        }
    )
    out = out[out["hcpcs"].str.match(r"^[A-Z0-9]\d{4}$", na=False)]
    out["quarter"] = quarter
    return out.reset_index(drop=True)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


_INDEXES = {
    "fda_package": ["ndc11", "product_id"],
    "fda_product": ["product_id"],
    "asp_crosswalk": ["ndc11", "hcpcs"],
    "asp_pricing": ["hcpcs"],
    "nadac": ["ndc11"],
}


def create_indexes(con: duckdb.DuckDBPyConnection) -> None:
    """Point-lookup indexes for converter.reference (CREATE OR REPLACE TABLE drops them, so rebuild each load)."""
    tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    for table, cols in _INDEXES.items():
        if table in tables:
            for col in cols:
                con.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_{col} ON {table} ({col})")


def _replace_table(con: duckdb.DuckDBPyConnection, name: str, df: pd.DataFrame) -> int:
    con.register("_incoming", df)
    con.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM _incoming")
    con.unregister("_incoming")
    return len(df)


def _log(con: duckdb.DuckDBPyConnection, source: str, url: str, file: Path, quarter: str | None, rows: int) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS refresh_log (
            source VARCHAR, url VARCHAR, file VARCHAR, quarter VARCHAR, rows BIGINT, loaded_at TIMESTAMP
        )
        """
    )
    con.execute(
        "INSERT INTO refresh_log VALUES (?, ?, ?, ?, ?, now())", [source, url, file.name, quarter, rows]
    )


def load_fda(con: duckdb.DuckDBPyConnection, files: list[tuple[Path, str, str]]) -> int:
    """files: (path, url, source label 'active'/'excluded')."""
    prods, packs = [], []
    forms: pd.Series | None = None
    for path, _url, label in files:  # active file first, so its forms cover delisted packages
        p, k = parse_fda_zip(path, label, forms)
        prods.append(p)
        packs.append(k)
        new = p.drop_duplicates("product_id").set_index("product_id")["dosage_form"]
        forms = new if forms is None else forms.combine_first(new)
    _replace_table(con, "fda_product", pd.concat(prods, ignore_index=True))
    rows = _replace_table(con, "fda_package", pd.concat(packs, ignore_index=True))
    for (path, url, label), k in zip(files, packs):
        _log(con, f"fda_{label}", url, path, None, len(k))
    return rows


def load_crosswalk(con: duckdb.DuckDBPyConnection, path: Path, url: str) -> int:
    q = quarter_label(url)
    rows = _replace_table(con, "asp_crosswalk", parse_crosswalk_zip(path, q))
    _log(con, "crosswalk", url, path, q, rows)
    return rows


def load_asp(con: duckdb.DuckDBPyConnection, path: Path, url: str) -> int:
    q = quarter_label(url)
    rows = _replace_table(con, "asp_pricing", parse_asp_zip(path, q))
    _log(con, "asp", url, path, q, rows)
    return rows


_NADAC_COLUMNS = {
    "ndc_description": "ndc_description",
    "ndc": "ndc_raw",
    "nadac_per_unit": "nadac_per_unit",
    "effective_date": "effective_date",
    "pricing_unit": "pricing_unit",
    "pharmacy_type_indicator": "pharmacy_type",
    "otc": "otc",
    "explanation_code": "explanation_code",
    "classification_for_rate_setting": "classification",
    "as_of_date": "as_of_date",
}


def _norm_col(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def load_nadac(con: duckdb.DuckDBPyConnection, path: Path, url: str) -> int:
    """NADAC is large (~1M rows/year), so DuckDB reads the CSV directly."""
    src = str(path).replace("'", "''")
    reader = f"read_csv('{src}', all_varchar=true, header=true)"
    cols = [d[0] for d in con.execute(f"SELECT * FROM {reader} LIMIT 0").description]
    by_norm = {_norm_col(c): c for c in cols}
    missing = [k for k in ("ndc", "nadac_per_unit", "effective_date", "pricing_unit") if k not in by_norm]
    if missing:
        raise ValueError(f"NADAC file is missing columns {missing}; found {cols}")

    def col(key: str) -> str:
        return '"' + by_norm[key].replace('"', '""') + '"' if key in by_norm else "NULL"

    date = "try_strptime(trim({}), '%m/%d/%Y')::DATE"
    con.execute(
        f"""
        CREATE OR REPLACE TABLE nadac AS
        SELECT
            trim({col('ndc_description')})                    AS ndc_description,
            lpad(regexp_replace({col('ndc')}, '[^0-9]', '', 'g'), 11, '0') AS ndc11,
            try_cast(trim({col('nadac_per_unit')}) AS DOUBLE)  AS nadac_per_unit,
            {date.format(col('effective_date'))}               AS effective_date,
            upper(trim({col('pricing_unit')}))                 AS pricing_unit,
            trim({col('pharmacy_type_indicator')})             AS pharmacy_type,
            trim({col('otc')})                                 AS otc,
            trim({col('explanation_code')})                    AS explanation_code,
            trim({col('classification_for_rate_setting')})     AS classification,
            {date.format(col('as_of_date'))}                   AS as_of_date
        FROM {reader}
        WHERE {col('ndc')} IS NOT NULL AND trim({col('ndc')}) <> ''
        """
    )
    rows = con.execute("SELECT count(*) FROM nadac").fetchone()[0]
    con.execute(
        """
        CREATE OR REPLACE VIEW nadac_latest AS
        SELECT * FROM nadac
        QUALIFY row_number() OVER (PARTITION BY ndc11 ORDER BY effective_date DESC, as_of_date DESC) = 1
        """
    )
    _log(con, "nadac", url, path, None, rows)
    return rows


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _get(dest: Path, url: str | None, offline: bool, session: requests.Session) -> tuple[Path, str]:
    if offline:
        return cached_file(dest, url)
    if not url:
        raise ValueError(f"no URL for {dest.name}")
    return download(url, dest, session), url


def refresh(
    sources: tuple[str, ...] | list[str] = ALL_SOURCES,
    db_path: Path = DB_PATH,
    raw_dir: Path = RAW_DIR,
    offline: bool = False,
    crosswalk_url: str | None = None,
    asp_url: str | None = None,
    nadac_url: str | None = None,
    include_excluded: bool = True,
) -> dict[str, int]:
    session = _session()
    results: dict[str, int] = {}

    cms: dict[str, str] = {}
    if not offline and (("crosswalk" in sources and not crosswalk_url) or ("asp" in sources and not asp_url)):
        print(f"discovering CMS quarterly files from {CMS_ASP_PAGE}")
        r = session.get(CMS_ASP_PAGE, timeout=TIMEOUT)
        r.raise_for_status()
        cms = discover_cms_urls(r.text)

    if "nadac" in sources and not offline and not nadac_url:
        print("discovering current NADAC dataset on data.medicaid.gov")
        r = session.get(MEDICAID_DATASETS, params={"show-reference-ids": "false"}, timeout=TIMEOUT)
        r.raise_for_status()
        picked = pick_nadac_dataset(r.json())
        if not picked:
            raise RuntimeError("could not find a NADAC dataset on data.medicaid.gov")
        r = session.get(f"{MEDICAID_DATASETS}/{picked[0]}", params={"show-reference-ids": "true"}, timeout=TIMEOUT)
        r.raise_for_status()
        nadac_url = nadac_download_url(r.json())
        print(f"  using {picked[1]}")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(db_path)) as con:
        if "fda" in sources:
            print("[fda] FDA NDC Directory")
            files = [(*_get(raw_dir / "fda", FDA_NDC_URL, offline, session), "active")]
            if include_excluded:
                files.append((*_get(raw_dir / "fda", FDA_EXCLUDED_URL, offline, session), "excluded"))
            results["fda_package"] = load_fda(con, files)
        if "crosswalk" in sources:
            print("[crosswalk] CMS ASP NDC-HCPCS crosswalk")
            path, url = _get(raw_dir / "cms_crosswalk", crosswalk_url or cms.get("crosswalk"), offline, session)
            results["asp_crosswalk"] = load_crosswalk(con, path, url)
        if "asp" in sources:
            print("[asp] CMS Part B payment limits")
            path, url = _get(raw_dir / "cms_asp", asp_url or cms.get("asp"), offline, session)
            results["asp_pricing"] = load_asp(con, path, url)
        if "nadac" in sources:
            print("[nadac] Medicaid NADAC")
            path, url = _get(raw_dir / "nadac", nadac_url, offline, session)
            results["nadac"] = load_nadac(con, path, url)
        create_indexes(con)
    return results


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sources", nargs="+", choices=ALL_SOURCES, default=list(ALL_SOURCES))
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--raw-dir", type=Path, default=RAW_DIR)
    ap.add_argument("--offline", action="store_true", help="reuse files already downloaded to --raw-dir")
    ap.add_argument("--crosswalk-url")
    ap.add_argument("--asp-url")
    ap.add_argument("--nadac-url")
    ap.add_argument("--no-excluded", action="store_true", help="skip the FDA excluded/delisted NDC file")
    args = ap.parse_args(argv)

    results = refresh(
        sources=args.sources,
        db_path=args.db,
        raw_dir=args.raw_dir,
        offline=args.offline,
        crosswalk_url=args.crosswalk_url,
        asp_url=args.asp_url,
        nadac_url=args.nadac_url,
        include_excluded=not args.no_excluded,
    )
    print("\nloaded:")
    for table, rows in results.items():
        print(f"  {table:<14} {rows:>10,} rows")
    print(f"into {args.db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
