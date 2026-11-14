"""Unit inference and confidence scoring.

Structural checks: given a quantity whose basis is unknown, compute both readings (quantity is in
NDC units / quantity is in HCPCS units) and score how plausible each is. The financial checks
(converter/financial.py: implied price per unit vs NADAC / ASP, default +/-25% band) are merged
in by combine().

Structural checks, per reading:
  hard  HCPCS units on a claim are whole numbers, so a fractional quantity cannot be HCPCS units
  hard  the quantity must be positive, and the package count can't be absurd (> max_packages)
  soft  whole items: the reading uses whole vials / syringes / tablets            (+2)
  soft  typical size: between min_packages and typical_max_packages packages    (+2)

The reading with the higher score wins. With structural checks only, the best result is Medium
(the spec reserves High for structural and financial checks agreeing), except when the factor is 1
and both readings give the same answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from converter.crosswalk import FactorEstimate, Issue

LEVELS = ("Low", "Medium", "High")


@dataclass(frozen=True)
class Settings:
    whole_tolerance: float = 0.01  # relative distance from a whole number that still counts as whole
    min_packages: float = 0.01  # below this the reading uses a sliver of one package
    typical_max_packages: float = 12  # more than this on one line is unusual
    max_packages: float = 1000  # more than this is not credible
    tolerance_band: float = 0.25  # implied price within +/-25% of the reference fits
    edge_fraction: float = 0.8  # a fit beyond 80% of the band is "near the edge" (Medium)
    billed_markup_max: float = 5.0  # billed charges may sit up to 5x the reference price


DEFAULT_SETTINGS = Settings()


@dataclass
class Reading:
    basis: str  # "NDC" or "HCPCS": what the source quantity is assumed to be in
    ndc_units: float
    hcpcs_units: float
    items: float | None  # vials / syringes / tablets
    packages: float | None  # whole 11-digit NDC packages
    waste_items: float | None = None  # JW waste in items, when the line is an aggregated group with waste
    feasible: bool = True
    score: int = 0
    passed: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


@dataclass
class StructuralDecision:
    basis: str | None  # "NDC", "HCPCS", "EITHER" (factor 1) or None (can't tell)
    level: str  # "High" / "Medium" / "Low"
    reason: str
    readings: dict[str, Reading]


def _is_whole(x: float, tol: float) -> bool:
    if x <= 0:
        return False
    nearest = round(x)
    return nearest >= 1 and abs(x - nearest) <= tol * max(1.0, nearest)


def _fmt(x: float | None) -> str:
    if x is None:
        return "?"
    return f"{x:,.4f}".rstrip("0").rstrip(".")


def _score(r: Reading, est: FactorEstimate, unit: str, s: Settings) -> Reading:
    items = "items" if unit == "EA" else f"items of {_fmt(est.ndc_units_per_item)} {unit}"
    if r.ndc_units <= 0:
        r.feasible = False
        r.failed.append("quantity must be positive")
        return r
    if r.basis == "HCPCS" and not _is_whole(r.hcpcs_units, 1e-9):
        r.feasible = False
        r.failed.append(f"{_fmt(r.hcpcs_units)} is not a whole number, and HCPCS units are billed whole")
    if r.packages is not None and r.packages > s.max_packages:
        r.feasible = False
        r.failed.append(f"{_fmt(r.packages)} packages is not credible on one line")

    if r.items is not None:
        if _is_whole(r.items, s.whole_tolerance):
            r.score += 2
            r.passed.append(f"{_fmt(r.items)} whole {items}")
        else:
            r.failed.append(f"{_fmt(r.items)} {items}, not a whole number")
    if r.waste_items is not None:
        if 0 < r.waste_items < 1 - s.whole_tolerance:
            r.score += 2
            r.passed.append(f"JW waste is {_fmt(r.waste_items)} of one item")
        else:
            r.failed.append(f"JW waste of {_fmt(r.waste_items)} items would discard at least one whole item")
    if r.packages is not None:
        if s.min_packages <= r.packages <= s.typical_max_packages:
            r.score += 2
            r.passed.append(f"{_fmt(r.packages)} packages, a typical size")
        elif r.packages < s.min_packages:
            r.failed.append(f"only {_fmt(r.packages)} of a package")
        else:
            r.failed.append(f"{_fmt(r.packages)} packages is unusually large for one line")
    return r


def structural_readings(
    quantity: float, est: FactorEstimate, settings: Settings = DEFAULT_SETTINGS, waste: float | None = None
) -> dict[str, Reading]:
    """waste: the JW part of an aggregated quantity (same unknown basis as the quantity)."""
    f = est.factor
    if not f:
        raise ValueError("factor estimate has no factor")
    unit = est.ndc_unit or "NDC unit"
    per_item = est.ndc_units_per_item
    per_pkg = est.ndc_units_per_package

    def build(basis: str, ndc_units: float, hcpcs_units: float, waste_ndc: float | None) -> Reading:
        r = Reading(
            basis=basis,
            ndc_units=round(ndc_units, 6),
            hcpcs_units=round(hcpcs_units, 6),
            items=round(ndc_units / per_item, 6) if per_item else None,
            packages=round(ndc_units / per_pkg, 6) if per_pkg else None,
            waste_items=round(waste_ndc / per_item, 6) if waste_ndc is not None and per_item else None,
        )
        return _score(r, est, unit, settings)

    return {
        "NDC": build("NDC", quantity, quantity * f, waste),
        "HCPCS": build("HCPCS", quantity / f, quantity, waste / f if waste is not None else None),
    }


def decide(
    quantity: float,
    est: FactorEstimate,
    settings: Settings = DEFAULT_SETTINGS,
    waste: float | None = None,
    uom_hint: str | None = None,
) -> StructuralDecision:
    """uom_hint ("NDC" / "HCPCS", from converter.uom) adds +1 to that reading: weaker than a +2 check,
    so it breaks ties but can't overturn a structural preference."""
    readings = structural_readings(quantity, est, settings, waste)
    ndc, hc = readings["NDC"], readings["HCPCS"]
    if uom_hint in readings and readings[uom_hint].feasible:
        readings[uom_hint].score += 1
        readings[uom_hint].passed.append("matches the claim's unit of measure")

    if abs(est.factor - 1) < 1e-9:  # type: ignore[operator]
        return StructuralDecision(
            "EITHER", "High", "1 HCPCS unit = 1 NDC unit for this product, so both readings agree", readings
        )
    if not ndc.feasible and not hc.feasible:
        return StructuralDecision(None, "Low", "neither reading is plausible: " + "; ".join(ndc.failed + hc.failed), readings)
    if ndc.feasible != hc.feasible:
        win, lose = (ndc, hc) if ndc.feasible else (hc, ndc)
        return StructuralDecision(
            win.basis,
            "Medium",
            f"quantity read as {win.basis} units: the {lose.basis} reading fails ({'; '.join(lose.failed)})",
            readings,
        )
    diff = ndc.score - hc.score
    if diff == 0:
        return StructuralDecision(
            None,
            "Low",
            "structural checks can't tell the readings apart; financials or modifier lines are needed",
            readings,
        )
    win, lose = (ndc, hc) if diff > 0 else (hc, ndc)
    why = "; ".join(win.passed) or "fewer failed checks"
    against = "; ".join(lose.failed)
    return StructuralDecision(
        win.basis,
        "Medium",
        f"quantity read as {win.basis} units ({why}); the {lose.basis} reading gives {against}",
        readings,
    )


def _down(level: str) -> str:
    return LEVELS[max(0, LEVELS.index(level) - 1)]


def combine(structural: StructuralDecision, fc) -> tuple[str | None, str, list[str]]:
    """Merge the structural decision with a converter.financial.FinancialCheck.

    High    structural and financial checks agree, variance inside the band (and not near its edge)
    Medium  only one check is decisive, the fit is near the band edge, or only a billed charge was available
    Low     the checks conflict, or neither reading is plausible
    Returns (basis, level, reasons).
    """
    s_basis, s_level = structural.basis, structural.level
    reasons = [structural.reason]
    if fc is None or not fc.available:
        why = fc.reason if fc is not None and fc.reason else "not run"
        reasons.append(f"structural checks only (no financial check: {why})")
        return s_basis, s_level, reasons
    reasons.append(fc.reason)
    f = fc.verdict
    soft = fc.near_edge or fc.one_sided  # weaker financial evidence caps at Medium

    if s_basis == "EITHER":
        if f in ("NDC", "HCPCS", "BOTH"):
            return "EITHER", "Medium" if soft else "High", reasons
        return "EITHER", "Medium", reasons

    if f in ("NDC", "HCPCS"):
        if s_basis == f:
            return f, "Medium" if soft else "High", reasons
        if s_basis is None:  # structure couldn't tell; price alone decides
            return f, "Medium", reasons
        # the checks disagree
        if not structural.readings[f].feasible:
            reasons.append(f"checks conflict: the price fits the {f} reading, which is structurally impossible")
            return None, "Low", reasons
        reasons.append(f"checks conflict: structure suggests {s_basis}, price suggests {f}; going with price")
        return f, "Low", reasons

    if f == "BOTH":
        return s_basis, s_level, reasons
    # NEITHER
    reasons.append("price does not support either reading; check the NDC, HCPCS code and amounts")
    return s_basis, _down(s_level), reasons


def apply_issues(level: str, issues: list[Issue]) -> tuple[str, list[str]]:
    """Edge cases force Low; any penalty drops one level. Returns (level, reasons)."""
    reasons: list[str] = []
    if any(i.severity == "edge" for i in issues):
        reasons += [i.message for i in issues if i.severity == "edge"]
        return "Low", reasons
    penalties = [i for i in issues if i.severity == "penalty"]
    if penalties:
        reasons += [i.message for i in penalties]
        level = LEVELS[max(0, LEVELS.index(level) - 1)]
    return level, reasons
