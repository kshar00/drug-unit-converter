"""Medical line aggregation before conversion.

Lines are grouped by claim ID + date of service + HCPCS code (+ NDC, since the conversion factor is
per NDC). Within a group:
  JW         discarded drug: added to the administered units, so the total covers whole vials
  JZ         attests no waste: marks the administered line; JW and JZ together are contradictory
  RT / LT    laterality (e.g. one injection per eye): both sides are added together
  (none)     administered
KP / KQ (first / subsequent drug of a multi-drug unit-dose formulation) are different HCPCS codes, so
they stay in separate groups and are linked as related; when they share an NDC, both must imply the
same NDC quantity.

The aggregated total is converted once; each line then gets its share in NDC and HCPCS units, using
the group's inferred unit basis.

    from converter.aggregate import convert_lines
    groups = convert_lines([ClaimLine(...), ...], ref)
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace

from converter.convert import ClaimLine, ConversionResult, convert_line
from converter.crosswalk import Issue
from converter.infer import DEFAULT_SETTINGS, LEVELS, Settings, apply_issues
from converter.ndc import NDCFormatError, normalize_ndc

WASTE, NO_WASTE = "JW", "JZ"
SIDES = ("RT", "LT")
MULTI_DRUG = ("KP", "KQ")
_AMOUNT_FIELDS = ("ingredient_cost", "allowed_amount", "billed_amount", "fee")
_SINGLE_DOSE_WORDS = ("SINGLE-DOSE", "SINGLE-USE", "SINGLE DOSE", "SINGLE USE")


def role(line: ClaimLine) -> str:
    """What a line contributes to its group."""
    mods = {m.upper() for m in line.modifiers}
    if WASTE in mods:
        return "waste"
    for side in SIDES:
        if side in mods:
            return side
    return "administered"


@dataclass
class LineShare:
    """One original line within a converted group."""

    line: ClaimLine
    role: str  # "administered", "waste", "RT", "LT"
    ndc_units: float | None = None
    hcpcs_units: float | None = None


@dataclass
class GroupResult:
    key: tuple  # (claim_id, date_of_service, hcpcs, ndc) or ("line", index) for a standalone line
    lines: list[LineShare]
    result: ConversionResult  # conversion of the aggregated total
    related: list[tuple] = field(default_factory=list)  # keys of KP/KQ partner groups

    @property
    def aggregated(self) -> bool:
        return len(self.lines) > 1


def _ndc_key(raw) -> str:
    try:
        n = normalize_ndc(raw)
        return n.ndc11 or str(raw).strip()
    except NDCFormatError:
        return str(raw).strip()


def group_lines(lines: list[ClaimLine]) -> list[tuple[tuple, list[ClaimLine]]]:
    """Group by claim ID + DOS + HCPCS + NDC. Lines without a claim ID stand alone. Order is preserved."""
    groups: dict[tuple, list[ClaimLine]] = {}
    for i, line in enumerate(lines):
        if not line.claim_id:
            key: tuple = ("line", i)
        else:
            hcpcs = (line.hcpcs or "").strip().upper() or None
            key = (line.claim_id, line.date_of_service, hcpcs, _ndc_key(line.ndc))
        groups.setdefault(key, []).append(line)
    return list(groups.items())


def _sum_amounts(lines: list[ClaimLine], issues: list[Issue]) -> dict:
    """Sum each amount field across the group, only when every line has it."""
    out = {}
    for name in _AMOUNT_FIELDS:
        values = [getattr(l, name) for l in lines]
        present = [v for v in values if v is not None]
        if not present:
            out[name] = None
        elif len(present) == len(values):
            out[name] = round(sum(present), 2)
        else:
            out[name] = None
            issues.append(
                Issue("partial_amounts", f"{name.replace('_', ' ')} is on only some lines of the group; not used")
            )
    return out


def _group_issues(lines: list[ClaimLine]) -> list[Issue]:
    issues: list[Issue] = []
    roles = Counter(role(l) for l in lines)
    mods = {m.upper() for l in lines for m in l.modifiers}
    if roles["waste"] and NO_WASTE in mods:
        issues.append(Issue("jw_and_jz", "group has a JW waste line and a JZ (no waste) attestation", "edge"))
    if roles["waste"] and not (roles["administered"] or roles["RT"] or roles["LT"]):
        issues.append(Issue("waste_only", "JW waste line without an administered line", "edge"))
    if roles["administered"] > 1:
        issues.append(Issue("duplicate_lines", f"{roles['administered']} administered lines for the same HCPCS; possible duplicate", "penalty"))
    for side in SIDES:
        if roles[side] > 1:
            issues.append(Issue("duplicate_side", f"{roles[side]} lines with {side}; possible duplicate", "penalty"))
    if roles["waste"] > 1:
        issues.append(Issue("duplicate_waste", f"{roles['waste']} JW lines; possible duplicate", "penalty"))
    uoms = {l.unit_of_measure for l in lines if l.unit_of_measure}
    if len(uoms) > 1:
        issues.append(Issue("uom_mixed", f"lines in the group report different units of measure ({', '.join(sorted(uoms))})", "penalty"))
    if len(lines) > 1:
        parts = ", ".join(f"{n} {r}" for r, n in roles.items())
        issues.append(Issue("aggregated", f"{len(lines)} lines combined ({parts})"))
    return issues


def _aggregate_line(lines: list[ClaimLine], issues: list[Issue]) -> ClaimLine:
    if len(lines) == 1:
        return lines[0]
    first = next((l for l in lines if role(l) != "waste"), lines[0])
    mods = tuple(dict.fromkeys(m.upper() for l in lines for m in l.modifiers))
    return replace(
        first,
        quantity=sum(l.quantity for l in lines),
        modifiers=mods,
        line_no=",".join(str(l.line_no) for l in lines if l.line_no) or None,
        **_sum_amounts(lines, issues),
    )


def _down(result: ConversionResult, issue: Issue) -> None:
    """Add a late issue (found after conversion) and lower the confidence accordingly."""
    result.issues.append(issue)
    if issue.severity == "edge":
        result.confidence = "Low"
    elif issue.severity == "penalty":
        result.confidence = LEVELS[max(0, LEVELS.index(result.confidence) - 1)]
    if issue.severity != "info":
        result.reasons.append(issue.message)


def _waste_checks(lines: list[ClaimLine], result: ConversionResult) -> None:
    """Single-dose containers: JZ means whole vials were used; no modifier at all is worth a note."""
    desc = (result.package_description or "").upper()
    if not any(w in desc for w in _SINGLE_DOSE_WORDS) or result.inferred_basis is None:
        return
    reading = result.readings.get("NDC" if result.inferred_basis == "EITHER" else result.inferred_basis)
    if reading is None or reading.items is None:
        return
    whole = abs(reading.items - round(reading.items)) <= 0.01 * max(1, round(reading.items)) and round(reading.items) >= 1
    mods = {m.upper() for l in lines for m in l.modifiers}
    if NO_WASTE in mods and not whole:
        _down(result, Issue(
            "jz_partial_vial",
            f"JZ says no waste, but the quantity is {reading.items:g} single-dose vials; a JW line may be missing",
            "penalty",
        ))
    elif WASTE not in mods and NO_WASTE not in mods and not whole and result.line.hcpcs:
        result.issues.append(Issue(
            "waste_modifier_missing",
            f"single-dose product with {reading.items:g} vials used and no JW/JZ modifier (Medicare requires one)",
        ))


def _shares(lines: list[ClaimLine], result: ConversionResult) -> list[LineShare]:
    basis, f = result.inferred_basis, result.factor
    out = []
    for l in lines:
        s = LineShare(l, role(l))
        if f and basis in ("HCPCS",):
            s.hcpcs_units, s.ndc_units = l.quantity, round(l.quantity / f, 6)
        elif f and basis in ("NDC", "EITHER"):
            s.ndc_units, s.hcpcs_units = l.quantity, round(l.quantity * f, 6)
        elif basis == "NDC":  # no crosswalk: NDC units only
            s.ndc_units = l.quantity
        out.append(s)
    return out


def _link_multi_drug(groups: list[GroupResult]) -> None:
    """Link KP/KQ groups on the same claim + DOS; check shared-NDC quantities agree."""
    by_visit: dict[tuple, list[GroupResult]] = defaultdict(list)
    for g in groups:
        mods = {m.upper() for s in g.lines for m in s.line.modifiers}
        if g.key[0] != "line" and mods & set(MULTI_DRUG):
            by_visit[(g.key[0], g.key[1])].append(g)
    for members in by_visit.values():
        mods = [{m.upper() for s in g.lines for m in s.line.modifiers} for g in members]
        has_kp = any("KP" in m for m in mods)
        has_kq = any("KQ" in m for m in mods)
        for g in members:
            g.related = [o.key for o in members if o is not g]
            if not (has_kp and has_kq):
                g.result.issues.append(Issue("multi_drug_unpaired", "KP/KQ line without its partner (needs both KP and KQ)"))
        by_ndc: dict[str, list[GroupResult]] = defaultdict(list)
        for g in members:
            if g.result.ndc11:
                by_ndc[g.result.ndc11].append(g)
        for same in by_ndc.values():
            if len(same) > 1:
                _reconcile_shared_ndc(same)


def _close(a: float, b: float, tol: float = 0.01) -> bool:
    return abs(a - b) <= tol * max(abs(a), abs(b), 1e-9)


def _reconcile_shared_ndc(same: list[GroupResult]) -> None:
    """KP and KQ lines for one combination NDC dispense the same NDC quantity.

    Decided lines must agree; an undecided line takes whichever reading matches its decided partner.
    """
    # a best-guess basis is not evidence: treat those lines as undecided
    decided = [g for g in same if g.result.ndc_units is not None and not g.result.basis_is_guess]
    undecided = [g for g in same if g.result.readings and (g.result.ndc_units is None or g.result.basis_is_guess)]
    qtys = sorted({round(g.result.ndc_units, 3) for g in decided})

    def mismatch(groups, values):
        for g in groups:
            _down(g.result, Issue(
                "multi_drug_mismatch",
                f"KP/KQ lines share NDC {g.result.ndc_hyphenated} but imply different NDC quantities "
                f"({', '.join(f'{q:g}' for q in values)})",
                "penalty",
            ))

    if len(qtys) > 1:
        mismatch(same, qtys)
        return
    if not qtys:
        return
    target = qtys[0]
    for g in undecided:
        r = g.result
        match = [b for b, rd in r.readings.items() if rd.feasible and _close(rd.ndc_units, target)]
        if len(match) != 1:
            mismatch([g, *decided], sorted({target, *(round(rd.ndc_units, 3) for rd in r.readings.values())}))
            continue
        rd = r.readings[match[0]]
        r.inferred_basis, r.ndc_units, r.hcpcs_units = match[0], rd.ndc_units, rd.hcpcs_units
        r.basis_is_guess = False
        r.issues.append(Issue("resolved_by_partner", f"unit basis taken from the KP/KQ partner line ({target:g} NDC units)"))
        level, extra = apply_issues("Medium", r.issues)
        r.confidence = level
        kept = [x for x in r.reasons[1:] if not x.startswith("best guess")]
        r.reasons = [f"{match[0]} reading chosen to match the KP/KQ partner's {target:g} NDC units", *kept, *extra]
        g.lines = _shares([s.line for s in g.lines], r)
    for g in same:
        if g.result.ndc_units is not None:
            g.result.issues.append(Issue("multi_drug_consistent", "KP/KQ lines agree on the NDC quantity"))


def convert_lines(lines: list[ClaimLine], ref, settings: Settings = DEFAULT_SETTINGS) -> list[GroupResult]:
    """Group, aggregate, convert and break down a batch of claim lines."""
    out: list[GroupResult] = []
    for key, members in group_lines(lines):
        issues = _group_issues(members)
        agg = _aggregate_line(members, issues)
        has_admin = any(role(l) != "waste" for l in members)
        waste = sum(l.quantity for l in members if role(l) == "waste") if has_admin and len(members) > 1 else None
        result = convert_line(agg, ref, settings, issues, waste_quantity=waste or None)
        _waste_checks(members, result)
        out.append(GroupResult(key, _shares(members, result), result))
    _link_multi_drug(out)
    return out
