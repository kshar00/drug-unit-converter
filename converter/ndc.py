"""NDC normalisation and FDA package-description parsing.

NDCs arrive in many shapes on claims: labeler-product-package with hyphens in 4-4-2, 5-3-2 or
5-4-1 form (the 10-digit FDA formats), 11-digit 5-4-2 (the HIPAA / NCPDP billing format), or as
bare digits, sometimes with leading zeros lost after a trip through Excel. Everything is
normalised to 11 digits without hyphens.

An unhyphenated 10-digit NDC is ambiguous: the missing zero could belong to any of the three
segments. We return all three candidates and let the reference store pick the one that exists.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

__all__ = [
    "NDCFormatError",
    "NormalizedNDC",
    "normalize_ndc",
    "format_ndc11",
    "PackageLevel",
    "PackageInfo",
    "parse_package_description",
]


class NDCFormatError(ValueError):
    """The input cannot be read as an NDC."""


@dataclass(frozen=True)
class NormalizedNDC:
    raw: str
    ndc11: str | None  # 11 digits, no hyphens; None when ambiguous
    source_format: str  # "4-4-2", "5-3-2", "5-4-1", "5-4-2", "11", "10-unhyphenated", "short"
    candidates: tuple[str, ...]  # possible 11-digit values (one unless ambiguous)
    warnings: tuple[str, ...] = ()

    @property
    def is_ambiguous(self) -> bool:
        return self.ndc11 is None

    @property
    def hyphenated(self) -> str | None:
        return format_ndc11(self.ndc11) if self.ndc11 else None


def format_ndc11(ndc11: str) -> str:
    """'00002143361' -> '00002-1433-61'."""
    if len(ndc11) != 11 or not ndc11.isdigit():
        raise NDCFormatError(f"not an 11-digit NDC: {ndc11!r}")
    return f"{ndc11[:5]}-{ndc11[5:9]}-{ndc11[9:]}"


_HYPHENS = re.compile(r"[‐-―−]")  # en/em dashes and minus signs from copy-paste

# segment lengths -> (format name, index of the segment that needs a leading zero, or None)
_HYPHEN_PATTERNS: dict[tuple[int, int, int], tuple[str, int | None]] = {
    (4, 4, 2): ("4-4-2", 0),
    (5, 3, 2): ("5-3-2", 1),
    (5, 4, 1): ("5-4-1", 2),
    (5, 4, 2): ("5-4-2", None),
}


def _to_text(raw: object) -> str:
    if raw is None or (isinstance(raw, float) and math.isnan(raw)):
        raise NDCFormatError("NDC is empty")
    if isinstance(raw, float):
        if not raw.is_integer():
            raise NDCFormatError(f"NDC is not a whole number: {raw!r}")
        return str(int(raw))
    return str(raw)


def normalize_ndc(raw: object) -> NormalizedNDC:
    """Normalise any common NDC representation to 11 digits (5-4-2, no hyphens)."""
    text = _to_text(raw)
    cleaned = _HYPHENS.sub("-", text).strip().replace(" ", "")
    if not cleaned:
        raise NDCFormatError("NDC is empty")

    if "-" in cleaned:
        parts = cleaned.split("-")
        if len(parts) != 3 or not all(p.isdigit() for p in parts):
            raise NDCFormatError(f"expected labeler-product-package digits, got {text!r}")
        key = tuple(len(p) for p in parts)
        if key not in _HYPHEN_PATTERNS:
            raise NDCFormatError(
                f"segment lengths {'-'.join(map(str, key))} are not a valid NDC layout in {text!r}"
            )
        name, pad = _HYPHEN_PATTERNS[key]
        if pad is not None:
            parts[pad] = "0" + parts[pad]
        ndc11 = "".join(parts)
        return NormalizedNDC(text, ndc11, name, (ndc11,))

    if not cleaned.isdigit():
        raise NDCFormatError(f"NDC contains non-digit characters: {text!r}")

    n = len(cleaned)
    if n == 11:
        return NormalizedNDC(text, cleaned, "11", (cleaned,))
    if n == 10:
        candidates = _dedupe(
            [
                "0" + cleaned,  # 4-4-2
                cleaned[:5] + "0" + cleaned[5:],  # 5-3-2
                cleaned[:9] + "0" + cleaned[9:],  # 5-4-1
            ]
        )
        return NormalizedNDC(
            text,
            None,
            "10-unhyphenated",
            tuple(candidates),
            ("10-digit NDC without hyphens is ambiguous; resolve against the reference data",),
        )
    if 6 <= n < 10:
        ndc11 = cleaned.zfill(11)
        return NormalizedNDC(
            text,
            ndc11,
            "short",
            (ndc11,),
            (f"{n}-digit NDC assumed to be an 11-digit NDC with leading zeros dropped",),
        )
    raise NDCFormatError(f"NDC must have 10 or 11 digits, got {n} in {text!r}")


def _dedupe(items: list[str]) -> list[str]:
    seen: list[str] = []
    for item in items:
        if item not in seen:
            seen.append(item)
    return seen


# ---------------------------------------------------------------------------
# FDA package descriptions
# ---------------------------------------------------------------------------
# Examples from the FDA NDC Directory (package.txt, PACKAGEDESCRIPTION):
#   "30 TABLET in 1 BOTTLE (0002-4184-30)"
#   "10 VIAL, SINGLE-DOSE in 1 CARTON (0002-1433-61)  / 2 mL in 1 VIAL, SINGLE-DOSE"
#   "1 KIT in 1 CARTON (0009-7168-01)  *  1 mL in 1 VIAL (0009-5177-02)  *  1 mL in 1 SYRINGE"
# Levels are separated by " / " (older files used " > "); kit components by " * ".

# NCPDP billing units: ML for liquids, GM for solids/semi-solids by weight, EA for everything else.
_MEASURE_UNITS: dict[str, tuple[str, float]] = {
    "ml": ("ML", 1.0),
    "l": ("ML", 1000.0),
    "g": ("GM", 1.0),
    "gm": ("GM", 1.0),
    "kg": ("GM", 1000.0),
}

# Innermost "units" that describe strength or duration rather than a countable/billable amount
# (e.g. "300 mg in 1 VIAL", "24 h in 1 PATCH", "30 [hp_C] in 1 TUBE"). We count the container.
_STRENGTH_UNITS = {"mg", "mcg", "ug", "ng", "h", "d", "u", "meq", "mmol"}

# Metered products: NCPDP often bills these by labelled GM/ML, which the FDA directory doesn't give.
_METERED_WORDS = {"SPRAY", "AEROSOL", "INHALANT"}

# Count units where the volume/weight is not stated, so EA may not match the NCPDP unit.
_UNSTATED_AMOUNT_WORDS = {
    "CREAM", "GEL", "LIQUID", "PASTE", "OINTMENT", "LOTION", "SOLUTION", "SUSPENSION",
    "EMULSION", "SHAMPOO", "OIL", "JELLY", "FOAM",
}
_UNSTATED_AMOUNT_UNITS = {"INJECTION", "INJECTION, SOLUTION", "INJECTION, SUSPENSION", "INJECTION, EMULSION"}

_EMBEDDED_NDC = re.compile(r"\(\s*\d[\d-]*\s*\)")
_LEVEL_SPLIT = re.compile(r"\s+[/>]\s+")
_LEVEL = re.compile(
    r"^(?P<qty>\d[\d,]*\.?\d*|\.\d+)\s+(?P<unit>.+?)\s+in\s+"
    r"(?P<cqty>\d[\d,]*\.?\d*|\.\d+)\s+(?P<container>.+)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PackageLevel:
    quantity: float
    unit: str
    container_quantity: float
    container: str


@dataclass
class PackageInfo:
    description: str
    levels: list[PackageLevel] = field(default_factory=list)
    package_quantity: float | None = None  # in NCPDP units
    ncpdp_unit: str | None = None  # "EA", "ML" or "GM"
    is_kit: bool = False
    notes: list[str] = field(default_factory=list)  # informational
    warnings: list[str] = field(default_factory=list)  # edge cases: lower the confidence

    @property
    def parsed(self) -> bool:
        return self.package_quantity is not None and self.ncpdp_unit is not None


def _num(text: str) -> float:
    return float(text.replace(",", ""))


def _parse_level(text: str) -> PackageLevel | None:
    m = _LEVEL.match(_EMBEDDED_NDC.sub("", text).strip())
    if not m:
        return None
    return PackageLevel(
        quantity=_num(m["qty"]),
        unit=m["unit"].strip(),
        container_quantity=_num(m["cqty"]),
        container=m["container"].strip(),
    )


def _product(levels: list[PackageLevel]) -> float:
    total = 1.0
    for level in levels:
        if level.container_quantity == 0:
            raise ZeroDivisionError
        total *= level.quantity / level.container_quantity
    return total


def _clean(x: float) -> float:
    """Round away float noise such as 0.1 * 3 = 0.30000000000000004."""
    return round(x, 6)


def _is_powder_for_injection(dosage_form: str | None, container: str) -> bool:
    """Injectable powders are billed EA per vial under NCPDP, even though FDA lists a reconstituted mL."""
    if not isinstance(dosage_form, str) or not dosage_form:  # None or NaN from a failed join
        return False
    form = dosage_form.upper()
    return "POWDER" in form and ("INJECTION" in form or "VIAL" in container.upper())


def parse_package_description(description: str | None, dosage_form: str | None = None) -> PackageInfo:
    """Work out package quantity and NCPDP billing unit from an FDA package description.

    dosage_form (FDA product DOSAGEFORMNAME) is optional; it lets us spot powders for injection,
    where FDA gives a reconstituted volume but NCPDP bills each vial as EA.
    """
    info = PackageInfo(description=description or "")
    if not description or not description.strip():
        info.warnings.append("no package description")
        return info

    components = [c.strip() for c in description.split("*") if c.strip()]
    level_texts = _LEVEL_SPLIT.split(components[0])
    levels = [_parse_level(t) for t in level_texts]
    if any(lv is None for lv in levels):
        bad = next(t for t, lv in zip(level_texts, levels) if lv is None)
        info.warnings.append(f"could not parse package level {bad.strip()!r}")
        return info
    info.levels = levels  # type: ignore[assignment]

    try:
        kit_index = next(
            (i for i, lv in enumerate(info.levels) if lv.unit.upper() == "KIT"), None
        )
        if len(components) > 1 or kit_index is not None:
            info.is_kit = True
            upto = info.levels if kit_index is None else info.levels[: kit_index + 1]
            info.package_quantity = _clean(_product(upto))
            info.ncpdp_unit = "EA"
            info.warnings.append("kit / multi-component package: billed as EA per kit")
            return info

        inner = info.levels[-1]
        unit_key = inner.unit.strip().lower()
        first_word = inner.unit.split(",")[0].strip().upper()

        if unit_key in _MEASURE_UNITS and _MEASURE_UNITS[unit_key][0] == "ML" and _is_powder_for_injection(
            dosage_form, inner.container
        ):
            info.ncpdp_unit = "EA"
            info.package_quantity = _clean(_product(info.levels[:-1]) / inner.container_quantity)
            info.notes.append(
                f"powder for injection ({dosage_form}): billed EA per {inner.container}; "
                f"{inner.quantity:g} {inner.unit} is the reconstituted volume"
            )
        elif unit_key in _MEASURE_UNITS:
            ncpdp, factor = _MEASURE_UNITS[unit_key]
            info.ncpdp_unit = ncpdp
            info.package_quantity = _clean(_product(info.levels) * factor)
        elif unit_key in _STRENGTH_UNITS or unit_key.startswith("["):
            # "300 mg in 1 VIAL": count the vials, the mg is a strength
            info.ncpdp_unit = "EA"
            info.package_quantity = _clean(_product(info.levels[:-1]) / inner.container_quantity)
            info.notes.append(
                f"innermost unit {inner.unit!r} is a strength or duration; counted "
                f"{inner.container} as EA"
            )
        else:
            info.ncpdp_unit = "EA"
            info.package_quantity = _clean(_product(info.levels))
            if first_word in _METERED_WORDS or "METERED" in inner.unit.upper():
                info.warnings.append(
                    f"metered product ({inner.unit}): NCPDP unit may be GM or ML of labelled "
                    "content, which the FDA directory does not give"
                )
            elif first_word in _UNSTATED_AMOUNT_WORDS or inner.unit.upper() in _UNSTATED_AMOUNT_UNITS:
                info.warnings.append(
                    f"volume/weight not stated for {inner.unit}; counted as EA"
                )
    except ZeroDivisionError:
        info.package_quantity = None
        info.ncpdp_unit = None
        info.warnings.append("zero container quantity in package description")
    return info
