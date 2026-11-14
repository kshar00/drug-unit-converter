"""Read-only lookups against the DuckDB reference store built by data/refresh.py."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from converter.ndc import NormalizedNDC, normalize_ndc

DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "reference.duckdb"

# Tables that can confirm an 11-digit NDC exists, in order of authority.
_NDC_TABLES = ("fda_package", "asp_crosswalk", "nadac")


@dataclass
class ResolvedNDC:
    normalized: NormalizedNDC
    ndc11: str | None
    found_in: list[str] = field(default_factory=list)
    reason: str = ""


class ReferenceStore:
    def __init__(self, db_path: str | Path = DEFAULT_DB):
        path = Path(db_path)
        if not path.exists():
            raise FileNotFoundError(
                f"reference store not found at {path}; build it with: python -m data.refresh"
            )
        self.con = duckdb.connect(str(path), read_only=True)
        self._tables = {r[0] for r in self.con.execute("SHOW TABLES").fetchall()}
        self._cache: dict[tuple, object] = {}

    def close(self) -> None:
        self.con.close()

    def __enter__(self) -> "ReferenceStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _rows(self, sql: str, params: list) -> list[dict]:
        cur = self.con.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def tables_with(self, ndc11: str) -> list[str]:
        return self._cached(
            ("tables_with", ndc11),
            lambda: [
                t
                for t in _NDC_TABLES
                if t in self._tables
                and self.con.execute(f"SELECT 1 FROM {t} WHERE ndc11 = ? LIMIT 1", [ndc11]).fetchone()
            ],
        )

    def resolve(self, raw: object) -> ResolvedNDC:
        """Normalise an NDC and, if ambiguous, pick the candidate that exists in the reference data."""
        norm = normalize_ndc(raw)
        if not norm.is_ambiguous:
            found = self.tables_with(norm.ndc11)  # type: ignore[arg-type]
            reason = "" if found else "NDC not found in FDA directory, crosswalk or NADAC"
            return ResolvedNDC(norm, norm.ndc11, found, reason)

        hits = {c: self.tables_with(c) for c in norm.candidates}
        matches = [c for c, found in hits.items() if found]
        if len(matches) == 1:
            c = matches[0]
            return ResolvedNDC(norm, c, hits[c], "ambiguous 10-digit NDC resolved by reference lookup")
        if not matches:
            return ResolvedNDC(norm, None, [], "ambiguous 10-digit NDC; no candidate found in reference data")
        return ResolvedNDC(
            norm, None, [], f"ambiguous 10-digit NDC; several candidates exist: {', '.join(matches)}"
        )

    # Lookups are plain indexed equality filters (DuckDB only uses its ART index for those), with
    # ordering done in Python, and cached per store because batch files repeat NDCs.

    def _cached(self, key: tuple, fn):
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    def package(self, ndc11: str) -> dict | None:
        """FDA package row (active listing preferred over delisted) merged with its product row."""
        if "fda_package" not in self._tables:
            return None
        return self._cached(("package", ndc11), lambda: self._package(ndc11))

    def _package(self, ndc11: str) -> dict | None:
        rows = self._rows("SELECT * FROM fda_package WHERE ndc11 = ?", [ndc11])
        if not rows:
            return None
        rows.sort(key=lambda r: (r.get("source") == "excluded", -_date_key(r.get("end_marketing_date"))))
        pkg = dict(rows[0])
        if "fda_product" in self._tables and pkg.get("product_id"):
            prod = self._rows(
                "SELECT proprietary_name, nonproprietary_name, dosage_form, route, labeler_name, "
                "substance_name, strength, strength_unit FROM fda_product WHERE product_id = ? LIMIT 1",
                [pkg["product_id"]],
            )
            if prod:
                pkg.update(prod[0])
        return pkg

    def search_products(self, query: str, limit: int = 50) -> list[dict]:
        """Find packages by brand / generic / ingredient name, or by HCPCS code (e.g. 'J9271').

        Packages in the CMS crosswalk (convertible to HCPCS) come first, then active FDA listings,
        then delisted ones. Each row has ndc11, the product names, strength, dosage form, package
        description, source and `hcpcs` (crosswalk codes, comma-separated, or None).
        """
        q = (query or "").strip()
        if len(q) < 3 or "fda_package" not in self._tables or "fda_product" not in self._tables:
            return []
        has_xw = "asp_crosswalk" in self._tables
        xw_join = (
            "LEFT JOIN (SELECT ndc11, string_agg(DISTINCT hcpcs, ', ' ORDER BY hcpcs) AS hcpcs "
            "FROM asp_crosswalk GROUP BY ndc11) x USING (ndc11)"
            if has_xw else ""
        )
        hcpcs_col = "x.hcpcs" if has_xw else "NULL"
        if has_xw and re.fullmatch(r"[A-Za-z]\d{4}", q):
            where, params = "pk.ndc11 IN (SELECT ndc11 FROM asp_crosswalk WHERE hcpcs = ?)", [q.upper()]
            starts = "FALSE"
        else:
            like = f"%{q}%"
            where = "(pr.proprietary_name ILIKE ? OR pr.nonproprietary_name ILIKE ? OR pr.substance_name ILIKE ?)"
            params = [like, like, like]
            starts = "(pr.proprietary_name ILIKE ? OR pr.nonproprietary_name ILIKE ?)"
            params += [f"{q}%", f"{q}%"]
        return self._rows(
            f"""
            SELECT pk.ndc11, pr.proprietary_name, pr.nonproprietary_name, pr.strength, pr.strength_unit,
                   pr.dosage_form, pk.package_description, pk.source, {hcpcs_col} AS hcpcs
            FROM fda_package pk
            JOIN fda_product pr USING (product_id)
            {xw_join}
            WHERE pk.ndc11 IS NOT NULL AND {where}
            ORDER BY NOT {starts}, {hcpcs_col} IS NULL, pk.source = 'excluded', pr.proprietary_name, pk.ndc11
            LIMIT {int(limit)}
            """,
            params,
        )

    def crosswalk(self, ndc11: str) -> list[dict]:
        if "asp_crosswalk" not in self._tables:
            return []
        return self._cached(
            ("crosswalk", ndc11),
            lambda: sorted(self._rows("SELECT * FROM asp_crosswalk WHERE ndc11 = ?", [ndc11]), key=lambda r: r["hcpcs"]),
        )

    def asp_price(self, hcpcs: str) -> dict | None:
        if "asp_pricing" not in self._tables:
            return None
        code = hcpcs.strip().upper()

        def load():
            rows = self._rows("SELECT * FROM asp_pricing WHERE hcpcs = ?", [code])
            return rows[0] if rows else None

        return self._cached(("asp", code), load)

    def nadac(self, ndc11: str, as_of=None) -> dict | None:
        """NADAC price in effect on `as_of` (a date; default: the latest).

        Takes the newest effective date on or before as_of (newest weekly file wins ties). If as_of
        predates every row, falls back to the earliest row and marks it with `_after_dos=True`.
        """
        if "nadac" not in self._tables:
            return None

        def load():
            rows = self._rows("SELECT * FROM nadac WHERE ndc11 = ?", [ndc11])
            rows.sort(key=lambda r: (_date_key(r.get("effective_date")), _date_key(r.get("as_of_date"))), reverse=True)
            return rows

        rows = self._cached(("nadac", ndc11), load)
        if not rows:
            return None
        if as_of is None:
            return rows[0]
        cutoff = _date_key(as_of)
        for r in rows:
            if _date_key(r.get("effective_date")) <= cutoff:
                return r
        return {**rows[-1], "_after_dos": True}


def _date_key(d) -> int:
    """Sortable integer for a date; missing dates sort as 'still marketed' (newest)."""
    if d is None or d != d:  # None or NaT
        return 99999999
    try:
        return d.year * 10000 + d.month * 100 + d.day
    except AttributeError:
        return 99999999
