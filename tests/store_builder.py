"""Build a tiny DuckDB reference store from Python dicts, with the same schema data/refresh.py produces."""

from pathlib import Path

import duckdb
import pandas as pd

from converter.ndc import normalize_ndc, parse_package_description
from converter.reference import ReferenceStore


def product(ndc, description, *, name="Synthetic", generic=None, form="INJECTION, SOLUTION", strength=None,
            strength_unit=None, source="active"):
    return dict(ndc=ndc, description=description, name=name, generic=generic or name.lower(), form=form,
                strength=strength, strength_unit=strength_unit, source=source)


def xw(ndc, hcpcs, dosage, size, qty, bu, bup, drug="Synthetic", short=None, quarter="2026-Q4"):
    return dict(hcpcs=hcpcs, short_description=short or f"{drug} inj", labeler_name="Synthetic Pharma",
                ndc11=normalize_ndc(ndc).ndc11, ndc_raw=ndc, drug_name=drug, hcpcs_dosage=dosage,
                pkg_size=float(size), pkg_qty=float(qty), bill_units=float(bu), bill_units_pkg=float(bup),
                quarter=quarter)


def asp(hcpcs, payment_limit, dosage="1 MG", quarter="2026-Q4"):
    return dict(hcpcs=hcpcs, short_description=f"{hcpcs} drug", hcpcs_dosage=dosage, payment_limit=float(payment_limit),
                coinsurance_pct=20.0, notes=None, quarter=quarter)


def nadac(ndc, price, unit, effective, as_of=None):
    from datetime import date as _d

    eff = _d.fromisoformat(effective)
    return dict(ndc_description="SYNTHETIC", ndc11=normalize_ndc(ndc).ndc11, nadac_per_unit=float(price),
                effective_date=eff, pricing_unit=unit, pharmacy_type="C/I", otc="N", explanation_code="1",
                classification="B", as_of_date=_d.fromisoformat(as_of) if as_of else eff)


def build_store(tmp_path: Path, products: list[dict], crosswalk: list[dict], asp_rows: list[dict] = (),
                nadac_rows: list[dict] = ()) -> ReferenceStore:
    prods, packs = [], []
    for i, p in enumerate(products):
        pid = f"P{i}"
        parsed = parse_package_description(p["description"], p["form"])
        prods.append(dict(product_id=pid, proprietary_name=p["name"], nonproprietary_name=p["generic"],
                          dosage_form=p["form"], route="INTRAVENOUS", labeler_name="Synthetic Pharma",
                          substance_name=p["generic"].upper(), strength=p["strength"],
                          strength_unit=p["strength_unit"], source=p["source"]))
        packs.append(dict(product_id=pid, ndc11=normalize_ndc(p["ndc"]).ndc11, package_ndc=p["ndc"],
                          package_description=p["description"], pkg_qty=parsed.package_quantity,
                          ncpdp_unit=parsed.ncpdp_unit, is_kit=parsed.is_kit,
                          parse_notes="; ".join(parsed.notes) or None,
                          parse_warnings="; ".join(parsed.warnings) or None,
                          source=p["source"], end_marketing_date=pd.NaT))
    path = tmp_path / "ref.duckdb"
    with duckdb.connect(str(path)) as con:
        tables = [("fda_product", prods), ("fda_package", packs), ("asp_crosswalk", crosswalk),
                  ("asp_pricing", list(asp_rows)), ("nadac", list(nadac_rows))]
        for name, rows in tables:
            if not rows:
                continue
            df = pd.DataFrame(rows)
            if name == "fda_package":
                df["end_marketing_date"] = pd.to_datetime(df["end_marketing_date"]).dt.date
            con.register("df", df)
            con.execute(f"CREATE TABLE {name} AS SELECT * FROM df")
            con.unregister("df")
    return ReferenceStore(path)
