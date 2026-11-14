"""Tabular input and output for the UI and batch use: CSV rows in, flat result rows out."""

from __future__ import annotations

import re

import pandas as pd

from converter.aggregate import GroupResult
from converter.convert import _PATIENT_FIELD, ClaimLine, ConversionResult, PatientDataError

INPUT_COLUMNS = [
    "claim_id", "line_no", "ndc", "quantity", "unit_of_measure", "hcpcs", "modifiers", "date_of_service",
    "ingredient_cost", "allowed_amount", "billed_amount", "fee",
]

# Common header spellings -> ClaimLine field
_ALIASES = {
    "ndc": "ndc", "ndc11": "ndc", "ndc_code": "ndc", "national_drug_code": "ndc",
    "quantity": "quantity", "qty": "quantity", "units": "quantity", "billed_units": "quantity",
    "quantity_dispensed": "quantity", "service_units": "quantity",
    "hcpcs": "hcpcs", "hcpcs_code": "hcpcs", "procedure_code": "hcpcs", "cpt_hcpcs": "hcpcs",
    "modifiers": "modifiers", "modifier": "modifiers", "mods": "modifiers",
    "date_of_service": "date_of_service", "dos": "date_of_service", "service_date": "date_of_service",
    "fill_date": "date_of_service",
    "ingredient_cost": "ingredient_cost", "ingredient_cost_paid": "ingredient_cost",
    "allowed_amount": "allowed_amount", "allowed": "allowed_amount",
    "billed_amount": "billed_amount", "billed": "billed_amount", "charge": "billed_amount", "charges": "billed_amount",
    "fee": "fee", "dispensing_fee": "fee", "admin_fee": "fee", "administration_fee": "fee",
    "unit_of_measure": "unit_of_measure", "uom": "unit_of_measure", "unit": "unit_of_measure",
    "unit_qualifier": "unit_of_measure", "ndc_unit_qualifier": "unit_of_measure", "quantity_qualifier": "unit_of_measure",
    "basis_of_measure": "unit_of_measure", "ncpdp_unit_of_measure": "unit_of_measure",
    "claim_id": "claim_id", "claim": "claim_id", "claim_number": "claim_id",
    "line_no": "line_no", "line": "line_no", "line_number": "line_no",
}


def _norm(col: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(col).strip().lower()).strip("_")


def check_columns(columns) -> None:
    """Refuse patient-like columns before reading any values."""
    bad = [c for c in columns if _PATIENT_FIELD.search(str(c)) or _PATIENT_FIELD.search(_norm(c))]
    if bad:
        raise PatientDataError(
            f"Columns {bad} look like patient data. This tool does not accept patient identifiers; "
            "remove those columns and upload again."
        )


def lines_from_dataframe(df: pd.DataFrame) -> tuple[list[ClaimLine], list[dict], list[str]]:
    """Map a claims DataFrame to ClaimLines.

    Returns (lines, errors, ignored_columns); errors are {"row": n, "error": message} for rows that
    could not be read. Raises PatientDataError for patient-like columns.
    """
    check_columns(df.columns)
    mapping, ignored = {}, []
    for col in df.columns:
        field = _ALIASES.get(_norm(col))
        if field and field not in mapping.values():
            mapping[col] = field
        else:
            ignored.append(str(col))
    if "ndc" not in mapping.values() or "quantity" not in mapping.values():
        raise ValueError("The file needs at least an NDC column and a quantity column.")

    lines, errors = [], []
    renamed = df.rename(columns=mapping)[list(mapping.values())]
    for i, row in enumerate(renamed.to_dict("records"), start=1):
        try:
            lines.append(ClaimLine.from_mapping(row))
        except (ValueError, TypeError) as e:
            errors.append({"row": i, "error": str(e)})
    return lines, errors, ignored


def _num(x):
    return None if x is None else round(float(x), 4)


def result_row(r: ConversionResult) -> dict:
    """One flat row for a conversion result."""
    fc = r.financial_check
    price = r.price_summary
    asp = next((p for p in price if p["reference"] == "ASP"), None)
    nadac = next((p for p in price if p["reference"] == "NADAC"), None)
    return {
        "claim_id": r.line.claim_id,
        "line_no": r.line.line_no,
        "ndc_input": r.line.ndc,
        "ndc11": r.ndc_hyphenated,
        "product": r.product_description,
        "ncpdp_unit": r.ncpdp_unit,
        "package_size": _num(r.package_size),
        "hcpcs": r.hcpcs,
        "hcpcs_dosage": r.hcpcs_dosage,
        "hcpcs_per_ndc_unit": _num(r.factor),
        "modifiers": " ".join(r.line.modifiers) or None,
        "quantity": _num(r.line.quantity),
        "unit_of_measure": r.line.unit_of_measure,
        "inferred_basis": r.inferred_basis or "unknown",
        "ndc_units": _num(r.ndc_units),
        "hcpcs_units": _num(r.hcpcs_units),
        "reading_if_ndc_units": _num(r.readings["NDC"].hcpcs_units) if "NDC" in r.readings else None,
        "reading_if_hcpcs_units": _num(r.readings["HCPCS"].ndc_units) if "HCPCS" in r.readings else None,
        "confidence": r.confidence,
        "amount_used": fc.amount_field if fc and fc.amount_field else None,
        "asp_per_hcpcs_unit": asp["reference_price"] if asp else None,
        "implied_per_hcpcs_unit": asp["implied_price"] if asp else None,
        "asp_variance_pct": asp["variance_pct"] if asp else None,
        "nadac_per_ndc_unit": nadac["reference_price"] if nadac else None,
        "implied_per_ndc_unit": nadac["implied_price"] if nadac else None,
        "nadac_variance_pct": nadac["variance_pct"] if nadac else None,
        "flags": "; ".join(sorted({i.code for i in r.issues if i.severity != "info"})) or None,
        "reason": r.reason,
    }


def group_rows(groups: list[GroupResult]) -> pd.DataFrame:
    """One row per converted group (an aggregated claim line or a standalone line)."""
    rows = []
    for g in groups:
        row = result_row(g.result)
        row["lines_combined"] = len(g.lines)
        row["related"] = "; ".join(f"{k[2]}" for k in g.related) or None
        rows.append(row)
    return pd.DataFrame(rows)


def line_rows(groups: list[GroupResult]) -> pd.DataFrame:
    """One row per original input line, with its share of the group in both unit bases."""
    rows = []
    for g in groups:
        for s in g.lines:
            rows.append({
                "claim_id": s.line.claim_id,
                "line_no": s.line.line_no,
                "ndc_input": s.line.ndc,
                "hcpcs": s.line.hcpcs,
                "modifiers": " ".join(s.line.modifiers) or None,
                "role": s.role,
                "quantity": _num(s.line.quantity),
                "unit_of_measure": s.line.unit_of_measure,
                "ndc_units": _num(s.ndc_units),
                "hcpcs_units": _num(s.hcpcs_units),
                "group_basis": g.result.inferred_basis or "unknown",
                "group_confidence": g.result.confidence,
            })
    return pd.DataFrame(rows)


def template_csv() -> str:
    return ",".join(INPUT_COLUMNS) + "\n"
