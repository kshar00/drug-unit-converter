"""Single claim line conversion: NDC units <-> HCPCS billing units, with confidence.

    from converter.convert import ClaimLine, convert_line
    from converter.reference import ReferenceStore

    with ReferenceStore() as ref:
        result = convert_line(ClaimLine(ndc="0006-3026-02", quantity=200, hcpcs="J9271"), ref)
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field, fields
from datetime import date

from converter.crosswalk import UNCLASSIFIED_HCPCS, FactorEstimate, Issue, hcpcs_candidates, is_multi_ingredient
from converter.financial import FinancialCheck, financial_check
from converter.infer import DEFAULT_SETTINGS, Reading, Settings, apply_issues, combine, decide
from converter.ndc import NDCFormatError
from converter.uom import normalize_uom, uom_hint

# Column names that suggest patient-level data. The tool refuses them rather than silently dropping.
_PATIENT_FIELD = re.compile(
    r"patient|member|subscriber|beneficiary|\bdob\b|birth|first_?name|last_?name|ssn|mrn|"
    r"prescriber|\bnpi\b|rx_?(number|num|no)\b|cardholder|address|phone",
    re.IGNORECASE,
)


class PatientDataError(ValueError):
    """Raised when input looks like it contains patient identifiers."""


def _opt_float(x) -> float | None:
    if x is None or (isinstance(x, str) and not x.strip()):
        return None
    v = float(str(x).replace(",", "").replace("$", "")) if isinstance(x, str) else float(x)
    return None if math.isnan(v) else v


@dataclass
class ClaimLine:
    ndc: str
    quantity: float
    hcpcs: str | None = None
    modifiers: tuple[str, ...] = ()
    date_of_service: date | None = None
    ingredient_cost: float | None = None
    allowed_amount: float | None = None
    billed_amount: float | None = None
    fee: float | None = None  # dispensing or admin fee
    unit_of_measure: str | None = None  # claim's unit for the quantity: EA, ML, GM, UN, F2, ME (see converter.uom)
    claim_id: str | None = None  # synthetic, for grouping lines
    line_no: str | None = None

    @classmethod
    def from_mapping(cls, data: dict) -> "ClaimLine":
        """Build from a dict (e.g. a CSV row). Unknown columns are ignored; patient-like columns are refused."""
        bad = [k for k in data if _PATIENT_FIELD.search(str(k))]
        if bad:
            raise PatientDataError(
                f"columns {bad} look like patient data; this tool does not accept patient identifiers"
            )
        known = {f.name for f in fields(cls)}
        d = {k: v for k, v in data.items() if k in known}

        def blank(v) -> bool:
            return v is None or (isinstance(v, float) and math.isnan(v)) or (isinstance(v, str) and not v.strip())

        d = {k: v for k, v in d.items() if not blank(v)}
        if "ndc" not in d or "quantity" not in d:
            raise ValueError("ndc and quantity are required")
        d["quantity"] = float(str(d["quantity"]).replace(",", ""))
        if "modifiers" in d and isinstance(d["modifiers"], str):
            d["modifiers"] = tuple(m for m in re.split(r"[\s,;|]+", d["modifiers"].upper()) if m)
        if "date_of_service" in d and isinstance(d["date_of_service"], str):
            d["date_of_service"] = date.fromisoformat(d["date_of_service"].strip()[:10])
        for k in ("ingredient_cost", "allowed_amount", "billed_amount", "fee"):
            if k in d:
                d[k] = _opt_float(d[k])
        if "unit_of_measure" in d:
            d["unit_of_measure"] = normalize_uom(d["unit_of_measure"])
        for k in ("hcpcs", "claim_id", "line_no", "ndc"):
            if k in d:
                d[k] = str(d[k]).strip()
        return cls(**d)


@dataclass
class ConversionResult:
    line: ClaimLine
    # product
    ndc11: str | None = None
    ndc_hyphenated: str | None = None
    product_description: str | None = None
    ncpdp_unit: str | None = None
    package_size: float | None = None  # FDA package quantity, in NCPDP units
    package_description: str | None = None
    # HCPCS
    hcpcs: str | None = None
    hcpcs_dosage: str | None = None
    hcpcs_description: str | None = None
    factor: float | None = None  # HCPCS units per NDC unit
    candidates: list[FactorEstimate] = field(default_factory=list)
    # quantities
    inferred_basis: str | None = None  # "NDC", "HCPCS", "EITHER" or None
    basis_is_guess: bool = False  # True when the checks couldn't decide and the basis is the billing-convention default
    ndc_units: float | None = None
    hcpcs_units: float | None = None
    readings: dict[str, Reading] = field(default_factory=dict)
    # confidence
    confidence: str = "Low"
    reasons: list[str] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    financial_check: FinancialCheck | None = None

    @property
    def price_summary(self) -> list[dict]:
        """Implied price vs reference price and % variance for the inferred reading."""
        if not self.financial_check:
            return []
        basis = "NDC" if self.inferred_basis == "EITHER" else self.inferred_basis
        return self.financial_check.summary(basis)

    @property
    def reason(self) -> str:
        return "; ".join(self.reasons)


def _describe(pkg: dict | None, xw: list[dict]) -> str | None:
    if pkg:
        name = pkg.get("proprietary_name") or pkg.get("nonproprietary_name")
        parts = [name]
        if pkg.get("nonproprietary_name") and pkg.get("nonproprietary_name", "").lower() != str(name).lower():
            parts.append(f"({pkg['nonproprietary_name']})")
        if pkg.get("strength") and pkg.get("strength_unit"):
            parts.append(f"{pkg['strength']} {pkg['strength_unit']}")
        if pkg.get("dosage_form"):
            parts.append(pkg["dosage_form"].lower())
        return " ".join(p for p in parts if p)
    if xw:
        return xw[0].get("drug_name")
    return None


def _quarter_covers(quarter: str | None, dos: date | None) -> bool | None:
    if not quarter or not dos:
        return None
    m = re.match(r"^(\d{4})-Q([1-4])$", quarter)
    if not m:
        return None
    return dos.year == int(m.group(1)) and (dos.month - 1) // 3 + 1 == int(m.group(2))


def _price_fit(c: FactorEstimate, line: ClaimLine, ref, nadac: dict | None, settings: Settings) -> int:
    """2 = the price picks out one reading, 1 = fits both, 0 = fits neither or no price data."""
    fc = financial_check(line, decide(line.quantity, c, settings).readings, c.ndc_unit, ref.asp_price(c.hcpcs), nadac, settings)
    return {"NDC": 2, "HCPCS": 2, "BOTH": 1}.get(fc.verdict or "", 0)


def _pick_candidate(
    cands: list[FactorEstimate], line: ClaimLine, settings: Settings, issues: list[Issue], ref=None,
    nadac: dict | None = None,
) -> FactorEstimate | None:
    usable = [c for c in cands if c.factor]
    if line.hcpcs:
        code = line.hcpcs.strip().upper()
        match = next((c for c in usable if c.hcpcs == code), None)
        if match:
            return match
        listed = ", ".join(c.hcpcs for c in cands) or "none"
        issues.append(
            Issue(
                "hcpcs_not_in_crosswalk",
                f"claim HCPCS {code} is not mapped to this NDC in the CMS crosswalk (crosswalk has: {listed})",
                "edge",
            )
        )
    if not usable:
        return None
    if len(usable) == 1:
        return usable[0]
    # several HCPCS codes for one NDC: rank by price fit first, then structural fit
    def structural_fit(c: FactorEstimate) -> int:
        scores = [r.score for r in decide(line.quantity, c, settings).readings.values() if r.feasible]
        return max(scores, default=-1)

    def price_fit(c: FactorEstimate) -> int:
        return _price_fit(c, line, ref, nadac, settings) if ref is not None else 0

    keyed = [(price_fit(c), structural_fit(c), c) for c in usable]
    keyed.sort(key=lambda k: (k[0], k[1]), reverse=True)  # stable: ties keep crosswalk order
    best = keyed[0]
    how = "price fit" if best[0] > 0 and (len(keyed) == 1 or best[0] > keyed[1][0]) else "structural fit"
    issues.append(
        Issue(
            "multiple_hcpcs",
            f"NDC maps to {len(usable)} HCPCS codes ({', '.join(c.hcpcs for c in usable)}); "
            f"ranked {', '.join(k[2].hcpcs for k in keyed)} by price then structural fit and picked "
            f"{best[2].hcpcs} on {how}; add the HCPCS code to confirm",
            "edge",
        )
    )
    return best[2]


def convert_line(
    line: ClaimLine,
    ref,
    settings: Settings = DEFAULT_SETTINGS,
    extra_issues: list[Issue] = (),
    waste_quantity: float | None = None,
) -> ConversionResult:
    """Convert one claim line. `ref` is a converter.reference.ReferenceStore (or anything with its methods).

    extra_issues (e.g. from modifier aggregation) count towards the confidence like any other issue.
    waste_quantity: for an aggregated line, the JW part of line.quantity; adds the "waste is less
    than one vial" structural check.
    """
    res = ConversionResult(line=line)
    issues = res.issues
    issues.extend(extra_issues)

    if line.quantity is None or not line.quantity > 0:
        issues.append(Issue("bad_quantity", "quantity must be a positive number", "edge"))
        res.confidence, res.reasons = "Low", [i.message for i in issues]
        return res

    # 1. NDC
    try:
        resolved = ref.resolve(line.ndc)
    except NDCFormatError as e:
        issues.append(Issue("bad_ndc", str(e), "edge"))
        res.confidence, res.reasons = "Low", [str(e)]
        return res
    for w in resolved.normalized.warnings:
        issues.append(Issue("ndc_format", w, "info" if resolved.ndc11 else "edge"))
    if resolved.ndc11 is None:
        issues.append(Issue("ndc_unresolved", resolved.reason, "edge"))
        res.confidence, res.reasons = "Low", [i.message for i in issues if i.severity != "info"]
        return res
    if resolved.reason and resolved.found_in:
        issues.append(Issue("ndc_resolved", resolved.reason))
    res.ndc11 = resolved.ndc11
    res.ndc_hyphenated = f"{res.ndc11[:5]}-{res.ndc11[5:9]}-{res.ndc11[9:]}"

    # 2. FDA package
    pkg = ref.package(res.ndc11)
    xw = ref.crosswalk(res.ndc11)
    res.product_description = _describe(pkg, xw)
    if pkg:
        res.ncpdp_unit = pkg.get("ncpdp_unit")
        res.package_size = pkg.get("pkg_qty")
        res.package_description = pkg.get("package_description")
        if pkg.get("source") == "excluded":
            issues.append(Issue("fda_excluded", "NDC is in the FDA excluded/delisted file"))
        if pkg.get("is_kit"):
            issues.append(Issue("kit", "kit / multi-component package", "edge"))
        for w in filter(None, (pkg.get("parse_warnings") or "").split("; ")):
            if "kit" not in w:
                issues.append(Issue("package_parse", w, "edge" if "could not parse" in w else "penalty"))
        if is_multi_ingredient(pkg):
            issues.append(Issue("multi_strength", "multi-ingredient / multi-strength product", "edge"))
    elif not resolved.found_in:
        issues.append(Issue("ndc_not_found", resolved.reason or "NDC not found in reference data", "edge"))

    # 3. HCPCS candidates
    if line.hcpcs and line.hcpcs.strip().upper() in UNCLASSIFIED_HCPCS:
        issues.append(
            Issue("unclassified_hcpcs", f"{line.hcpcs.upper()} is an unclassified (NOC) code with no fixed billing unit", "edge")
        )
    if not xw:
        issues.append(Issue("no_crosswalk", "NDC has no CMS NDC-HCPCS crosswalk row, so there is no HCPCS conversion", "edge"))
        if not line.hcpcs:  # no HCPCS on the line either: the quantity can only be NDC units
            res.ndc_units = line.quantity
            res.inferred_basis = "NDC"
        res.confidence, res.reasons = apply_issues("Low", issues)
        return res

    res.candidates = hcpcs_candidates(xw, pkg)
    quarter = xw[0].get("quarter")
    covers = _quarter_covers(quarter, line.date_of_service)
    if covers is False:
        issues.append(
            Issue("dos_outside_quarter", f"date of service is outside the loaded crosswalk quarter ({quarter})")
        )

    nadac = ref.nadac(res.ndc11, line.date_of_service)
    if nadac and nadac.get("_after_dos"):
        issues.append(Issue("nadac_after_dos", "no NADAC price on or before the date of service; used the earliest loaded"))

    est = _pick_candidate(res.candidates, line, settings, issues, ref, nadac)
    if est is None:
        issues.append(Issue("no_factor", "no usable conversion factor in the crosswalk", "edge"))
        res.confidence, res.reasons = apply_issues("Low", issues)
        return res

    res.hcpcs, res.hcpcs_dosage, res.hcpcs_description = est.hcpcs, est.hcpcs_dosage, est.short_description
    res.factor = est.factor
    issues.extend(est.issues)
    if res.ncpdp_unit is None:
        res.ncpdp_unit = est.ndc_unit

    # 4. Both readings: structural decision, then financial check
    uom = normalize_uom(line.unit_of_measure)
    hint, uom_issues = uom_hint(uom, res.ncpdp_unit, est.hcpcs_dosage)
    issues.extend(uom_issues)
    if uom == "EA" and res.ncpdp_unit in ("ML", "GM") and est.ndc_units_per_item and est.factor:
        # EA on a liquid / weighed product usually means whole vials (or tubes) were counted
        per = est.ndc_units_per_item
        ndc_if_items = line.quantity * per
        issues.append(Issue(
            "uom_ea_items",
            f"if {_fmt_q(line.quantity)} EA counts whole items of {_fmt_q(per)} {res.ncpdp_unit}, that is "
            f"{_fmt_q(ndc_if_items)} {res.ncpdp_unit} = {_fmt_q(ndc_if_items * est.factor)} {est.hcpcs} units",
            "penalty",
        ))
    decision = decide(line.quantity, est, settings, waste_quantity, hint)
    res.readings = decision.readings
    fc = financial_check(line, decision.readings, res.ncpdp_unit, ref.asp_price(est.hcpcs), nadac, settings)
    res.financial_check = fc
    issues.extend(fc.issues)
    basis, level, reasons = combine(decision, fc)
    if hint and basis in ("NDC", "HCPCS") and basis != hint:
        issues.append(Issue(
            "uom_conflict",
            f"the claim's unit of measure ({line.unit_of_measure}) points to {hint} units, but the checks say {basis}",
            "penalty",
        ))
    if basis is None:
        guess = best_guess(line, decision.readings)
        if guess:
            basis, level = guess, "Low"
            res.basis_is_guess = True
            reasons.append(
                f"best guess: {guess} units, because "
                + ("the line has a HCPCS code, and quantities on medical claim lines are usually HCPCS units"
                   if guess == "HCPCS" and line.hcpcs else
                   "the line has no HCPCS code, and pharmacy claim quantities are usually NDC units"
                   if guess == "NDC" and not line.hcpcs else
                   "the other reading is not possible")
                + f"; the other reading would give {_other_reading(decision.readings, guess)}"
            )
    res.inferred_basis = basis
    if basis in ("NDC", "HCPCS", "EITHER"):
        r = decision.readings["NDC" if basis == "EITHER" else basis]
        res.ndc_units, res.hcpcs_units = r.ndc_units, r.hcpcs_units

    level, extra = apply_issues(level, issues)
    res.confidence = level
    res.reasons = reasons + extra
    return res


def _fmt_q(x: float) -> str:
    return f"{x:,.4f}".rstrip("0").rstrip(".")


def best_guess(line: ClaimLine, readings: dict[str, Reading]) -> str | None:
    """Default basis when the checks can't decide, so the user always gets both numbers.

    Billing convention: a line with a HCPCS code is a medical claim line, whose quantity is normally in
    HCPCS units; a line without one is normally a pharmacy line, in NDC units. A reading that is not
    possible (e.g. a fractional quantity read as HCPCS units) is never chosen.
    """
    preferred = ["HCPCS", "NDC"] if line.hcpcs else ["NDC", "HCPCS"]
    for basis in preferred:
        r = readings.get(basis)
        if r is not None and r.feasible:
            return basis
    return None


def _other_reading(readings: dict[str, Reading], chosen: str) -> str:
    other = readings["NDC" if chosen == "HCPCS" else "HCPCS"]
    return f"{_fmt_q(other.ndc_units)} NDC units = {_fmt_q(other.hcpcs_units)} HCPCS units"
