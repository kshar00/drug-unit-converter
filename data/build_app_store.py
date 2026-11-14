"""Slim, committable copy of the reference store for deployment.

The full store (data/reference.duckdb, ~230 MB with indexes) is too big for GitHub, and rebuilding it
from the public files on every cold start takes a minute and ~150 MB of downloads. Instead:

  export       python -m data.build_app_store
               writes data/app/<table>.parquet (zstd, ~20 MB) with only what the converter reads:
                 fda_product / fda_package   the columns converter.reference uses
                 asp_crosswalk / asp_pricing  as is
                 nadac                        one row per NDC and effective date (NADAC repeats every
                                              price in each weekly file)
                 refresh_log                  so the app can show the data vintage
  materialize  materialize(dest) loads the Parquet files into an indexed DuckDB file (~4 s);
               the Streamlit app does this once per container when data/reference.duckdb is absent.
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.refresh import DB_PATH, create_indexes  # noqa: E402

APP_DIR = ROOT / "data" / "app"

TABLES = {
    "fda_product": """
        SELECT product_id, proprietary_name, nonproprietary_name, dosage_form, route, labeler_name,
               substance_name, strength, strength_unit, source
        FROM src.fda_product""",
    "fda_package": """
        SELECT product_id, ndc11, package_ndc, package_description, pkg_qty, ncpdp_unit, is_kit,
               parse_notes, parse_warnings, source, end_marketing_date
        FROM src.fda_package WHERE ndc11 IS NOT NULL""",
    "asp_crosswalk": "SELECT * FROM src.asp_crosswalk",
    "asp_pricing": "SELECT * FROM src.asp_pricing",
    "nadac": """
        SELECT * FROM src.nadac
        QUALIFY row_number() OVER (PARTITION BY ndc11, effective_date ORDER BY as_of_date DESC) = 1""",
    "refresh_log": "SELECT * FROM src.refresh_log",
}


def _q(path: Path) -> str:
    return str(path).replace("\\", "/").replace("'", "''")


def export(src: Path = DB_PATH, out_dir: Path = APP_DIR) -> dict[str, int]:
    if not src.exists():
        raise FileNotFoundError(f"{src} not found; run python -m data.refresh first")
    out_dir.mkdir(parents=True, exist_ok=True)
    counts = {}
    with duckdb.connect() as con:
        con.execute(f"ATTACH '{_q(src)}' AS src (READ_ONLY)")
        for name, sql in TABLES.items():
            target = out_dir / f"{name}.parquet"
            con.execute(f"COPY ({sql}) TO '{_q(target)}' (FORMAT parquet, COMPRESSION zstd, COMPRESSION_LEVEL 19)")
            counts[name] = con.execute(f"SELECT count(*) FROM read_parquet('{_q(target)}')").fetchone()[0]
    return counts


def materialize(dest: Path, parquet_dir: Path = APP_DIR) -> Path:
    """Build an indexed DuckDB reference store from the exported Parquet files."""
    files = sorted(parquet_dir.glob("*.parquet"))
    if not files:
        raise FileNotFoundError(f"no Parquet files in {parquet_dir}; run python -m data.build_app_store")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.stem + ".building.duckdb")
    tmp.unlink(missing_ok=True)
    with duckdb.connect(str(tmp)) as con:
        for f in files:
            con.execute(f"CREATE TABLE {f.stem} AS SELECT * FROM read_parquet('{_q(f)}')")
        create_indexes(con)
        con.execute("CHECKPOINT")
    tmp.replace(dest)
    return dest


def main() -> int:
    counts = export()
    for name, n in counts.items():
        print(f"  {name:<14} {n:>10,} rows")
    size = sum(f.stat().st_size for f in APP_DIR.glob("*.parquet"))
    print(f"wrote {APP_DIR} ({size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
