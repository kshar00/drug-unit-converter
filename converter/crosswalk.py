"""HCPCS candidates and conversion factors.

Conversion factor = HCPCS billing units per NDC (NCPDP) unit.

The CMS ASP crosswalk gives, per NDC and HCPCS code:
  PKG SIZE      amount in one item (e.g. 4 for a 4 mL vial)
  PKG QTY       items in the NDC (e.g. 2 vials in a carton)
  BILLUNITS     HCPCS billing units in one item
  BILLUNITSPKG  HCPCS billing units in the whole NDC (CMS rounds this up to a whole unit)

So the factor is BILLUNITS / (NDC units per item). BILLUNITSPKG / (NDC units per package) should
give the same number; when it doesn't, we check both against the FDA labelled strength and the
HCPCS dosage descriptor (e.g. 40 mg/mL against "10 MG" gives 4 HCPCS units per mL).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

UNCLASSIFIED_HCPCS = frozenset(
    {"J3490", "J3590", "J9999", "C9399", "J7599", "J7699", "J7799", "J8499", "J8999"}
)

FACTOR_TOLERANCE = 0.02  # relative difference at which two factor estimates count as the same


@dataclass(frozen=True)
class Issue:
    """Something that affects confidence.

    severity: "info" (no effect), "penalty" (lowers confidence one level), "edge" (forces Low).
    """

    code: str
    message: str
    severity: str = "info"


@dataclass
class FactorEstimate:
    """One HCPCS candidate for an NDC, with its conversion factor."""

    hcpcs: str
    short_description: str | None
    hcpcs_dosage: str | None
    drug_name: str | None
    ndc_unit: str | None  # NCPDP unit the factor is per (EA / ML / GM), None if unknown
    factor: float | None  # HCPCS units per NDC unit
    ndc_units_per_item: float | None  # e.g. 4 (mL per vial)
    ndc_units_per_package: float | None  # NDC units in the whole 11-digit NDC
    hcpcs_units_per_package: float | None
    factor_source: str | None  # "cms", "cms_items", "cms_package", "strength"
    alternatives: dict[str, float | None] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)


def _close(a: float | None, b: float | None, tol: float = FACTOR_TOLERANCE) -> bool:
    if a is None or b is None:
        return False
    if a == b:
        return True
    return abs(a - b) <= tol * max(abs(a), abs(b))


def _num(x) -> float | None:
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) else v


def _r(x: float | None, nd: int = 6) -> float | None:
    return None if x is None else round(x, nd)


# ---------------------------------------------------------------------------
# Strength cross-check
# ---------------------------------------------------------------------------

# canonical kind, multiplier to the canonical unit (mg, mL, unit, meq)
_DOSAGE_UNITS = {
    "MG": ("mass", 1.0),
    "MCG": ("mass", 0.001),
    "UG": ("mass", 0.001),
    "G": ("mass", 1000.0),
    "GM": ("mass", 1000.0),
    "GRAM": ("mass", 1000.0),
    "GRAMS": ("mass", 1000.0),
    "ML": ("volume", 1.0),
    "CC": ("volume", 1.0),
    "UNIT": ("unit", 1.0),
    "UNITS": ("unit", 1.0),
    "U": ("unit", 1.0),
    "IU": ("unit", 1.0),
    "MEQ": ("meq", 1.0),
}

_FDA_NUM_UNITS = {
    "mg": ("mass", 1.0),
    "ug": ("mass", 0.001),
    "mcg": ("mass", 0.001),
    "g": ("mass", 1000.0),
    "kg": ("mass", 1_000_000.0),
    "meq": ("meq", 1.0),
}
_FDA_DENOM_UNITS = {"ml": ("ML", 1.0), "l": ("ML", 1000.0), "g": ("GM", 1.0), "kg": ("GM", 1000.0)}

_DOSAGE = re.compile(r"^\s*(?:UP\s+TO\s+|PER\s+)?(\d[\d,]*\.?\d*|\.\d+)\s*([A-Z]+)\b")
_FDA_UNIT = re.compile(r"^\s*([^/]+?)\s*/\s*(\d*\.?\d*)\s*([A-Za-z]*)\s*$")


def parse_hcpcs_dosage(text: str | None) -> tuple[str, float] | None:
    """'10 MG' -> ('mass', 10.0); 'UP TO 80 MG' -> ('mass', 80.0); '1000 UNITS' -> ('unit', 1000.0)."""
    if not text:
        return None
    m = _DOSAGE.match(text.upper())
    if not m or m.group(2) not in _DOSAGE_UNITS:
        return None
    kind, mult = _DOSAGE_UNITS[m.group(2)]
    return kind, float(m.group(1).replace(",", "")) * mult


def _num_unit_kind(unit: str) -> tuple[str, float] | None:
    u = unit.strip()
    if u.startswith("[") or u.lower() in {"u", "unit", "units"}:  # [iU], [USP'U], [arb'U]
        return ("unit", 1.0)
    return _FDA_NUM_UNITS.get(u.lower())


def strength_per_ndc_unit(strength, strength_unit, ndc_unit: str | None) -> tuple[str, float] | None:
    """FDA labelled strength expressed per NDC unit, e.g. ('40', 'mg/mL', 'ML') -> ('mass', 40.0).

    Returns None for multi-ingredient strengths and for units we can't line up (e.g. a powder vial
    billed EA whose strength is given per reconstituted mL).
    """
    s, unit = _num(strength), (strength_unit or "")
    if s is None or not unit or ";" in str(strength) or ";" in unit or ndc_unit is None:
        return None
    m = _FDA_UNIT.match(unit)
    if not m:
        return None
    num = _num_unit_kind(m.group(1))
    if num is None:
        return None
    denom_qty = float(m.group(2)) if m.group(2) else 1.0
    denom_unit = m.group(3).lower()
    if denom_qty == 0:
        return None
    if denom_unit == "":  # "mg/1": per each
        if ndc_unit != "EA":
            return None
        per = 1.0
    else:
        if denom_unit not in _FDA_DENOM_UNITS:
            return None
        ncpdp, mult = _FDA_DENOM_UNITS[denom_unit]
        if ncpdp != ndc_unit:
            return None
        per = mult
    kind, mult = num
    return kind, s * mult / (denom_qty * per)


def strength_factor(pkg: dict | None, hcpcs_dosage: str | None, ndc_unit: str | None) -> float | None:
    """HCPCS units per NDC unit implied by FDA strength and the HCPCS dosage descriptor."""
    dose = parse_hcpcs_dosage(hcpcs_dosage)
    if dose is None or ndc_unit is None:
        return None
    if re.match(r"^\s*UP\s+TO\b", hcpcs_dosage or "", re.IGNORECASE):
        return None  # "UP TO 80 MG": any amount up to the limit is one unit, so strength can't be divided
    kind, amount = dose
    if amount <= 0:
        return None
    if kind == "volume":
        return 1.0 / amount if ndc_unit == "ML" else None
    if pkg is None:
        return None
    per = strength_per_ndc_unit(pkg.get("strength"), pkg.get("strength_unit"), ndc_unit)
    if per is None or per[0] != kind:
        return None
    return per[1] / amount


def is_multi_ingredient(pkg: dict | None) -> bool:
    return bool(pkg) and ";" in str(pkg.get("strength") or "")


# ---------------------------------------------------------------------------
# Factor estimation
# ---------------------------------------------------------------------------


def estimate_factor(row: dict, pkg: dict | None) -> FactorEstimate:
    """Work out the conversion factor for one crosswalk row, using the FDA package where we have it."""
    size, qty = _num(row.get("pkg_size")), _num(row.get("pkg_qty"))
    bu, bup = _num(row.get("bill_units")), _num(row.get("bill_units_pkg"))
    issues: list[Issue] = []

    ndc_unit = pkg.get("ncpdp_unit") if pkg else None
    fda_pkg_qty = _num(pkg.get("pkg_qty")) if pkg else None
    f_str = strength_factor(pkg, row.get("hcpcs_dosage"), ndc_unit)

    # NDC units per CMS "item". CMS doesn't say what unit PKG SIZE is in: usually mL for liquids,
    # 1 for vials and tablets, sometimes a count of tablets or a bag size. Candidates, in order of
    # preference for the product's NCPDP unit, plus the item size implied by the FDA package.
    # Pick the one the labelled strength confirms; failing that, the one that reproduces the FDA
    # package; failing that, the default.
    per_item: float | None = None
    if size is not None and qty:
        options = [size, 1.0] if ndc_unit in ("ML", "GM", None) else [1.0, size]
        if fda_pkg_qty:
            options.append(fda_pkg_qty / qty)
        options = [u for i, u in enumerate(options) if u > 0 and u not in options[:i]]
        if f_str is not None:
            per_item = next(
                (u for u in options if (bu is not None and _close(bu / u, f_str)) or (bup is not None and _close(bup / (u * qty), f_str))),
                None,
            )
        if per_item is None and fda_pkg_qty is not None:
            per_item = next((u for u in options if _close(u * qty, fda_pkg_qty)), None)
        if per_item is None:
            per_item = options[0] if options else None
        if per_item is not None and fda_pkg_qty is not None and not _close(per_item * qty, fda_pkg_qty):
            issues.append(
                Issue(
                    "package_mismatch",
                    f"CMS package ({_fmt(size)} x {_fmt(qty)}) does not match the FDA package "
                    f"({_fmt(fda_pkg_qty)} {ndc_unit}); conversion is per {ndc_unit} but package "
                    "counts may differ",
                    "penalty",
                )
            )
    if ndc_unit is None:
        issues.append(
            Issue(
                "ncpdp_unit_unknown",
                "NDC not found in the FDA directory, so the NCPDP unit is unknown; assuming the CMS "
                "package size is in NDC billing units",
                "penalty",
            )
        )

    f_item = bu / per_item if bu is not None and per_item else None
    per_pkg = per_item * qty if per_item and qty else None
    f_pkg = bup / per_pkg if bup is not None and per_pkg else None

    factor, source = _choose_factor(f_item, f_pkg, f_str, bu, bup, qty, issues)

    if f_str is not None and factor is not None and source != "strength":
        if _close(factor, f_str):
            issues.append(Issue("strength_confirms", "FDA labelled strength confirms the factor"))
        else:
            ratio = factor / f_str
            issues.append(
                Issue(
                    "strength_disagrees",
                    f"FDA labelled strength implies {_fmt(f_str)} HCPCS units per {ndc_unit}, "
                    f"crosswalk gives {_fmt(factor)}",
                    "penalty" if 0.5 <= ratio <= 2 else "edge",
                )
            )

    return FactorEstimate(
        hcpcs=str(row.get("hcpcs") or "").upper(),
        short_description=row.get("short_description"),
        hcpcs_dosage=row.get("hcpcs_dosage"),
        drug_name=row.get("drug_name"),
        ndc_unit=ndc_unit,
        factor=_r(factor),
        ndc_units_per_item=_r(per_item),
        ndc_units_per_package=_r(per_pkg),
        hcpcs_units_per_package=_r(factor * per_pkg) if factor and per_pkg else None,
        factor_source=source,
        alternatives={"cms_items": _r(f_item), "cms_package": _r(f_pkg), "strength": _r(f_str)},
        issues=issues,
    )


def _choose_factor(f_item, f_pkg, f_str, bu, bup, qty, issues: list[Issue]) -> tuple[float | None, str | None]:
    if f_item is not None and _close(f_item, f_pkg):
        return f_item, "cms"
    if f_item is not None and bup is not None and qty and bup == math.ceil(bu * qty - 1e-9):
        issues.append(
            Issue(
                "cms_rounded_up",
                f"CMS rounds billing units per NDC up ({_fmt(bu * qty)} to {_fmt(bup)}); using the exact value",
            )
        )
        return f_item, "cms_items"
    if f_item is None and f_pkg is None:
        if f_str is not None:
            issues.append(Issue("strength_only", "crosswalk has no billing units; factor from FDA strength", "penalty"))
            return f_str, "strength"
        issues.append(Issue("no_factor", "crosswalk row has no usable package size or billing units", "edge"))
        return None, None
    if f_item is None or f_pkg is None:
        issues.append(Issue("cms_partial", "only one of BILLUNITS / BILLUNITSPKG is usable", "penalty"))
        return (f_item, "cms_items") if f_item is not None else (f_pkg, "cms_package")
    # BILLUNITS and BILLUNITSPKG disagree beyond rounding: let the labelled strength decide
    if _close(f_item, f_str):
        issues.append(
            Issue("cms_inconsistent", f"BILLUNITSPKG ({_fmt(bup)}) is inconsistent; FDA strength confirms BILLUNITS", "penalty")
        )
        return f_item, "cms_items"
    if _close(f_pkg, f_str):
        issues.append(
            Issue("cms_inconsistent", f"BILLUNITS ({_fmt(bu)}) is inconsistent; FDA strength confirms BILLUNITSPKG", "penalty")
        )
        return f_pkg, "cms_package"
    issues.append(
        Issue(
            "factor_conflict",
            f"BILLUNITS gives {_fmt(f_item)} and BILLUNITSPKG gives {_fmt(f_pkg)} HCPCS units per NDC unit, "
            "and FDA strength cannot settle it; using BILLUNITSPKG",
            "edge",
        )
    )
    return f_pkg, "cms_package"


def _fmt(x: float | None) -> str:
    if x is None:
        return "?"
    return f"{x:,.4f}".rstrip("0").rstrip(".")


def hcpcs_candidates(crosswalk_rows: list[dict], pkg: dict | None) -> list[FactorEstimate]:
    """One FactorEstimate per distinct HCPCS code mapped to the NDC."""
    seen: dict[str, FactorEstimate] = {}
    for row in crosswalk_rows:
        est = estimate_factor(row, pkg)
        if est.hcpcs not in seen:
            seen[est.hcpcs] = est
    return list(seen.values())
