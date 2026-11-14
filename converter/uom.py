"""Unit of measure reported on the claim line, as a hint about the quantity's unit basis.

Claims often carry a unit with the drug quantity:
  NCPDP (pharmacy)   Unit of Measure: EA, ML, GM
  837 (medical)      NDC unit qualifier: UN (unit), ML, GR (gram), F2 (international unit), ME (milligram)

How it is used:
  EA / ML / GM matching the product's NCPDP unit  -> hints the quantity is in NDC units
  EA / ML / GM not matching it                     -> flagged: a common billing error (e.g. ML on a
                                                      powder vial that is billed EA); no hint
  UN                                               -> no hint: UN is used both for "each" and for HCPCS
                                                      service units, and many systems default to it
  ME / F2 with a HCPCS descriptor of exactly 1 mg / 1 unit -> hints HCPCS units (mg or IU = HCPCS units)
  ME / F2 otherwise                                -> no hint

It is weaker evidence than the structural or price checks, because billing systems often fill it in by default.
"""

from __future__ import annotations

from converter.crosswalk import Issue, parse_hcpcs_dosage

_ALIASES = {
    "EA": "EA", "EACH": "EA",
    "ML": "ML", "MILLILITER": "ML", "MILLILITRE": "ML", "MILLILITERS": "ML",
    "GM": "GM", "GR": "GM", "G": "GM", "GRAM": "GM", "GRAMS": "GM",
    "UN": "UN", "UNIT": "UN", "UNITS": "UN",
    "F2": "F2", "IU": "F2", "INTERNATIONAL UNIT": "F2",
    "ME": "ME", "MG": "ME", "MILLIGRAM": "ME", "MILLIGRAMS": "ME",
}
VALID = ("EA", "ML", "GM", "UN", "F2", "ME")
LABELS = {"EA": "EA (each)", "ML": "ML (milliliter)", "GM": "GM / GR (gram)", "UN": "UN (unit)",
          "F2": "F2 (international unit)", "ME": "ME (milligram)"}


def normalize_uom(raw) -> str | None:
    """'gr' -> 'GM', 'Each' -> 'EA', '' -> None. Unrecognised values come back upper-cased as given."""
    if raw is None:
        return None
    text = str(raw).strip().upper()
    if not text or text in ("NAN", "NONE"):
        return None
    return _ALIASES.get(text, text)


def uom_hint(uom: str | None, ncpdp_unit: str | None, hcpcs_dosage: str | None) -> tuple[str | None, list[Issue]]:
    """Return (hinted basis "NDC" / "HCPCS" / None, issues)."""
    if not uom:
        return None, []
    if uom not in VALID:
        return None, [Issue("uom_unrecognised", f"unit of measure {uom!r} not recognised; ignored")]

    if uom in ("EA", "ML", "GM"):
        if ncpdp_unit is None:
            return None, [Issue("uom_unchecked", f"unit of measure {uom} can't be checked: NCPDP unit unknown")]
        if uom == ncpdp_unit:
            return "NDC", [Issue("uom_matches", f"claim unit of measure {uom} matches the NCPDP billing unit, which points to NDC units")]
        return None, [Issue(
            "uom_mismatch",
            f"claim unit of measure {uom} does not match the product's NCPDP billing unit {ncpdp_unit}",
            "penalty",
        )]

    if uom == "UN":
        return None, [Issue("uom_un", "unit of measure UN is used for both 'each' and HCPCS service units, so it can't tell the basis")]

    # ME (milligram) / F2 (international unit): only a hint when one HCPCS unit is exactly 1 mg / 1 unit
    dose = parse_hcpcs_dosage(hcpcs_dosage)
    kind = "mass" if uom == "ME" else "unit"
    if dose and dose[0] == kind and abs(dose[1] - 1) < 1e-9:
        return "HCPCS", [Issue("uom_dosage_match", f"unit of measure {uom} with a HCPCS descriptor of {hcpcs_dosage}: one HCPCS unit per {'mg' if uom == 'ME' else 'unit'}, which points to HCPCS units")]
    return None, [Issue("uom_no_hint", f"unit of measure {uom} with HCPCS descriptor {hcpcs_dosage or 'unknown'} doesn't map to either reading")]
