"""Financial check: does the claim's price make sense under each reading of the quantity?

Amount used, in order of preference:
  ingredient_cost        pharmacy ingredient cost (excludes the dispensing fee by definition)
  allowed_amount - fee   allowed amount with the dispensing / admin fee stripped
  billed_amount - fee    billed charge; providers mark charges up, so this gets a one-sided markup test

Implied price per unit = amount / quantity, under each reading:
  per NDC unit    compared with NADAC per unit (when NADAC prices the NDC in the same NCPDP unit)
  per HCPCS unit  compared with the CMS ASP payment limit for the HCPCS code

A reading "fits" a reference when the variance is inside the tolerance band (default +/-25%), or,
for billed charges, when implied/reference lies between (1 - band) and the markup ceiling (default 5x).
A fit with |variance| above edge_fraction x band (default 80% of the band) is "near the edge".
"""

from __future__ import annotations

from dataclasses import dataclass, field

from converter.crosswalk import Issue
from converter.infer import DEFAULT_SETTINGS, Reading, Settings


@dataclass
class Comparison:
    reference: str  # "ASP" or "NADAC"
    unit: str  # "HCPCS unit" or the NCPDP unit, e.g. "ML"
    reference_price: float
    implied: dict[str, float] = field(default_factory=dict)  # reading basis -> implied price per unit
    variance: dict[str, float] = field(default_factory=dict)  # reading basis -> implied / reference - 1
    fits: dict[str, bool] = field(default_factory=dict)
    near_edge: dict[str, bool] = field(default_factory=dict)

    def verdict(self) -> str:
        ok = [b for b in ("NDC", "HCPCS") if self.fits.get(b)]
        if len(ok) == 2:
            return "BOTH"
        if len(ok) == 1:
            return ok[0]
        return "NEITHER"


@dataclass
class FinancialCheck:
    amount_field: str | None = None  # "ingredient_cost", "allowed_amount" or "billed_amount"
    amount: float | None = None  # after fee stripping
    fee_stripped: float | None = None
    one_sided: bool = False  # billed charge: markup test instead of a symmetric band
    comparisons: list[Comparison] = field(default_factory=list)
    verdict: str | None = None  # "NDC", "HCPCS", "BOTH", "NEITHER", or None (no check possible)
    near_edge: bool = False
    reference_conflict: bool = False  # ASP and NADAC disagreed; verdict follows the primary reference
    reason: str = ""
    issues: list[Issue] = field(default_factory=list)

    @property
    def available(self) -> bool:
        return self.verdict is not None

    def summary(self, basis: str | None) -> list[dict]:
        """Implied price vs reference and % variance for one reading, for display."""
        if basis not in ("NDC", "HCPCS"):
            return []
        return [
            {
                "reference": c.reference,
                "unit": c.unit,
                "reference_price": round(c.reference_price, 4),
                "implied_price": round(c.implied[basis], 4),
                "variance_pct": round(100 * c.variance[basis], 1),
                "within_band": c.fits[basis],
            }
            for c in self.comparisons
            if basis in c.implied
        ]


def choose_amount(line, issues: list[Issue]) -> tuple[str | None, float | None, float | None, bool]:
    """(field, net amount, fee stripped, one_sided)."""
    fee = line.fee if line.fee and line.fee > 0 else None
    if line.ingredient_cost and line.ingredient_cost > 0:
        return "ingredient_cost", line.ingredient_cost, None, False
    for name, one_sided in (("allowed_amount", False), ("billed_amount", True)):
        amount = getattr(line, name)
        if amount and amount > 0:
            if fee:
                if fee >= amount:
                    issues.append(Issue("fee_exceeds_amount", f"fee ({fee:,.2f}) is not less than {name} ({amount:,.2f}); not stripped", "penalty"))
                    return name, amount, None, one_sided
                return name, amount - fee, fee, one_sided
            return name, amount, None, one_sided
    return None, None, None, False


def _compare(reference: str, unit: str, ref_price: float, amount: float, units_by_basis: dict[str, float],
             one_sided: bool, s: Settings) -> Comparison:
    c = Comparison(reference, unit, ref_price)
    for basis, units in units_by_basis.items():
        if not units or units <= 0:
            continue
        implied = amount / units
        v = implied / ref_price - 1
        c.implied[basis], c.variance[basis] = implied, v
        if one_sided:
            ratio = implied / ref_price
            c.fits[basis] = (1 - s.tolerance_band) <= ratio <= s.billed_markup_max
            c.near_edge[basis] = c.fits[basis] and ratio < 1 - s.edge_fraction * s.tolerance_band
        else:
            c.fits[basis] = abs(v) <= s.tolerance_band
            c.near_edge[basis] = c.fits[basis] and abs(v) > s.edge_fraction * s.tolerance_band
    return c


def financial_check(
    line,
    readings: dict[str, Reading],
    ncpdp_unit: str | None,
    asp: dict | None,
    nadac: dict | None,
    settings: Settings = DEFAULT_SETTINGS,
) -> FinancialCheck:
    fc = FinancialCheck()
    fc.amount_field, fc.amount, fc.fee_stripped, fc.one_sided = choose_amount(line, fc.issues)
    if fc.amount is None:
        fc.reason = "no amounts on the line"
        return fc
    if line.fee and fc.amount_field == "ingredient_cost":
        fc.issues.append(Issue("fee_separate", "ingredient cost used; it excludes the fee, so nothing was stripped"))

    if asp and asp.get("payment_limit"):
        fc.comparisons.append(
            _compare("ASP", "HCPCS unit", float(asp["payment_limit"]), fc.amount,
                     {b: r.hcpcs_units for b, r in readings.items()}, fc.one_sided, settings)
        )
    if nadac and nadac.get("nadac_per_unit"):
        unit = (nadac.get("pricing_unit") or "").upper()
        if ncpdp_unit and unit and unit != ncpdp_unit:
            fc.issues.append(
                Issue(
                    "nadac_unit_mismatch",
                    f"NADAC prices this NDC per {unit} but the FDA package reads as {ncpdp_unit}; NADAC check skipped",
                    "penalty",
                )
            )
        else:
            fc.comparisons.append(
                _compare("NADAC", unit or ncpdp_unit or "NDC unit", float(nadac["nadac_per_unit"]), fc.amount,
                         {b: r.ndc_units for b, r in readings.items()}, fc.one_sided, settings)
            )

    if not fc.comparisons:
        fc.reason = "no reference price (ASP or NADAC) for this NDC / HCPCS"
        return fc

    verdicts = {c.reference: c.verdict() for c in fc.comparisons}
    decisive = {v for v in verdicts.values() if v in ("NDC", "HCPCS")}
    if len(decisive) == 2:
        # ASP and NADAC disagree. Follow the reference that matches the amount: pharmacy ingredient
        # cost tracks acquisition cost (NADAC); medical allowed / billed amounts track ASP.
        primary = "NADAC" if fc.amount_field == "ingredient_cost" else "ASP"
        fc.verdict = verdicts[primary]
        fc.reference_conflict = True
        other = "ASP" if primary == "NADAC" else "NADAC"
        fc.issues.append(
            Issue(
                "reference_conflict",
                f"ASP and NADAC point to different readings; followed {primary} because the "
                f"{fc.amount_field.replace('_', ' ')} tracks it ({other} disagrees)",
                "penalty",
            )
        )
    elif decisive:
        fc.verdict = decisive.pop()
    elif "BOTH" in verdicts.values():
        fc.verdict = "BOTH"
    else:
        fc.verdict = "NEITHER"
    if fc.verdict in ("NDC", "HCPCS", "BOTH"):
        winners = ("NDC", "HCPCS") if fc.verdict == "BOTH" else (fc.verdict,)
        fc.near_edge = any(c.near_edge.get(b) for c in fc.comparisons for b in winners if c.fits.get(b))

    fc.reason = _explain(fc, verdicts, settings)
    return fc


def _pct(v: float) -> str:
    return f"{100 * v:+.0f}%"


def _explain(fc: FinancialCheck, verdicts: dict[str, str], s: Settings) -> str:
    label = fc.amount_field.replace("_", " ") if fc.amount_field else "amount"
    if fc.fee_stripped:
        label += f" less {fc.fee_stripped:,.2f} fee"
    test = f"markup test (x{1 - s.tolerance_band:g} to x{s.billed_markup_max:g})" if fc.one_sided else f"+/-{100 * s.tolerance_band:.0f}% band"
    parts = []
    for c in fc.comparisons:
        bits = ", ".join(
            f"{b} reading {c.implied[b]:,.2f} ({_pct(c.variance[b])})" for b in ("NDC", "HCPCS") if b in c.implied
        )
        parts.append(f"{c.reference} {c.reference_price:,.2f}/{c.unit}: {bits}")
    head = {
        "NDC": "price fits the NDC reading",
        "HCPCS": "price fits the HCPCS reading",
        "BOTH": "price fits both readings (factor too close to 1 to tell them apart)",
        "NEITHER": "price fits neither reading",
    }[fc.verdict]  # type: ignore[index]
    edge = "; near the edge of the band" if fc.near_edge else ""
    return f"{head} ({label}, {test}{edge}): " + "; ".join(parts)
