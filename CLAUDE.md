# Drug Unit Converter – Project Spec (CLAUDE.md)

## Purpose

A public web tool that takes a single drug claim line where the quantity's unit basis is unknown, and returns both the NDC (NCPDP) unit equivalent and the HCPCS billing unit equivalent, with a confidence level. Where financials are supplied, it uses them to validate and disambiguate. Portfolio project showcasing PBM / specialty pharmacy domain expertise. Built only on public data and synthetic claims.

## Privacy rule

No patient data. The tool never asks for or stores patient name, ID, DOB, prescriber or Rx number. The UI must state this clearly.

## Inputs (per line)

- NDC (any format: 10-digit with hyphens, 11-digit)
- Source quantity (unit basis unknown)
- Optional: HCPCS code
- Optional: modifiers (JW, JZ, RT, LT, KP, KQ, etc.)
- Optional: date of service
- Optional financials: ingredient cost, allowed amount, billed amount, dispensing fee / admin fee
- Optional: claim ID (synthetic) for grouping multiple lines

## Outputs

- Normalised 11-digit NDC and product description
- NCPDP billing unit (EA / ML / GM) and package size
- Candidate HCPCS code(s) with dosage descriptor
- Quantity expressed in NDC units and in HCPCS units
- Inferred source unit basis (NDC or HCPCS)
- Confidence level (High / Medium / Low) plus reason text
- Financial check: implied price per unit vs reference price, % variance

## Public data sources

- FDA NDC Directory: product and package descriptions, package quantity and unit
- CMS ASP NDC-HCPCS crosswalk (quarterly): HCPCS code, dosage descriptor, billing units per package
- CMS ASP pricing file (quarterly): payment limit per HCPCS unit
- NADAC (Medicaid, weekly): price per NDC unit
- NLM RxNorm (optional): strength and ingredient normalisation

All files are loaded into a local reference store (DuckDB or SQLite) with a refresh script.

## Conversion logic

1. Normalise the NDC to 11-digit 5-4-2 format (pad the correct segment based on 4-4-2, 5-3-2, 5-4-1 patterns).
2. Look up the product in the FDA directory. Parse the package description to get package quantity and NCPDP unit (EA, ML, GM).
3. Look up the crosswalk. Get the HCPCS code(s), dosage descriptor and billing units per package.
4. Conversion factor: HCPCS units per NDC unit = billing units per package / package quantity in NDC units.
5. Compute both readings of the source quantity:
   - If the quantity is in NDC units, HCPCS units = qty × factor.
   - If the quantity is in HCPCS units, NDC units = qty / factor.

## Medical line aggregation (before conversion)

- Group lines by claim ID + date of service + HCPCS code.
- Combine JW (discarded waste) and JZ (no waste) lines with the administered line to get the total units.
- Combine RT / LT laterality lines.
- Handle KP / KQ (multi-drug unit-dose formulations) as related lines.
- Convert the aggregated total, and keep the line-level breakdown in the output.

## Unit inference and confidence scoring

- Structural checks: does each reading give a plausible quantity (whole vials or packages, within typical dose range)?
- Financial checks (when amounts are present):
  - Strip dispensing and admin fees where identifiable.
  - Implied price per unit = amount / quantity.
  - Compare with NADAC per NDC unit and with the ASP payment limit per HCPCS unit.
  - The reading whose implied price falls within a tolerance band (default ±25%, configurable) wins.
- Confidence:
  - High: structural and financial checks agree, variance inside the band.
  - Medium: only one check available, or variance near the edge of the band.
  - Low: checks conflict, no crosswalk match, or edge case.

## Edge cases (v1: flag as Low confidence)

- Kits and multi-component packages
- NDCs that map to multiple HCPCS codes: score each candidate with the financial check and rank them
- Multi-strength products
- NDCs missing from the crosswalk or NADAC
- Unclassified codes (J3490, J3590, J9999)

## Tech stack

- Python 3.11+
- Streamlit UI (single-line form plus CSV batch upload)
- DuckDB for reference data
- pandas for processing
- pytest for tests
- Deploy: Streamlit Community Cloud

## Suggested repo structure

- app.py (Streamlit UI)
- converter/ndc.py (normalisation, package parsing)
- converter/crosswalk.py (HCPCS lookup, factors)
- converter/aggregate.py (modifier roll-up)
- converter/infer.py (unit inference, confidence)
- data/refresh.py (download and load public files)
- tests/ (unit tests with synthetic claims)
- samples/synthetic\_claims.csv

## Milestones

1. Load the reference data and normalise NDCs.
2. Build the core conversion for single lines with no financials.
3. Add the financial validation and confidence scoring.
4. Add modifier aggregation for medical lines.
5. Add the Streamlit UI with CSV upload, then deploy.
6. Write up the project as a LinkedIn post with a demo.

## Milestone status

| # | Milestone | Status |
| --- | --- | --- |
| 1 | Reference data + NDC normalisation | Done (2026-11-14) |
| 2 | Core conversion, no financials | Done (2026-11-14) |
| 3 | Financial validation + confidence | Done (2026-11-14) |
| 4 | Modifier aggregation | Done (2026-11-14) |
| 5 | Streamlit UI + CSV upload + deploy | UI done (2026-11-14); deploy waiting on GitHub push + Streamlit Cloud setup |
| 6 | LinkedIn write-up | Not started |

### Milestone 1 notes

- Build the store: `.venv/Scripts/python -m data.refresh` (about 1 minute; `--offline` reuses `data/raw/`).
  It finds the newest CMS quarter from the ASP pricing page and the newest NADAC year on data.medicaid.gov.
- DuckDB tables: `fda_product`, `fda_package` (active + excluded/delisted NDCs, with parsed `pkg_qty`,
  `ncpdp_unit`, `is_kit`, `parse_notes`, `parse_warnings`), `asp_crosswalk`, `asp_pricing`, `nadac`
  (+ view `nadac_latest`), `refresh_log`. Every NDC column is `ndc11` (11 digits, no hyphens).
- `converter/reference.py` holds the read-only lookups, including resolving an ambiguous unhyphenated
  10-digit NDC against the reference data.
- First load (Q4 2026 CMS files): 605,683 FDA packages, 7,403 crosswalk rows (7,221 NDCs, 877 HCPCS),
  918 ASP prices, 1,358,735 NADAC rows.
- Data findings to handle in milestones 2–3:
  - Powders for injection: FDA gives the reconstituted mL, NCPDP bills EA per vial. The parser uses the
    FDA dosage form to switch these to EA.
  - 328 crosswalk rows where CMS `PKG SIZE x PKG QTY` disagrees with the FDA mL package size
    (e.g. Xgeva 55513-0730-01: CMS 1, FDA 1.7 mL). Treat that as a confidence penalty, not an error.
  - 138 NDCs map to more than one HCPCS code.
  - Only 1,639 of 7,221 crosswalk NDCs have a NADAC price (NADAC is retail-pharmacy focused), so for most
    Part B drugs the financial check will rely on ASP alone.
  - Metered inhalers / sprays and products without a stated volume are counted as EA and flagged.

### Milestone 2 notes

- Entry point: `converter.convert.convert_line(ClaimLine(...), ReferenceStore())` returns a
  `ConversionResult` with both readings, inferred basis, confidence and reasons.
  `ClaimLine.from_mapping` refuses columns that look like patient data (`PatientDataError`).
- Conversion factor (`converter/crosswalk.py`): CMS `BILLUNITS` / NDC units per item. CMS's `PKG SIZE`
  unit isn't stated, so the item size is chosen from {PKG SIZE, 1, FDA package / PKG QTY} by: first the
  one the FDA labelled strength confirms (strength / HCPCS dosage descriptor), then the one that
  reproduces the FDA package, then the default. `BILLUNITSPKG` is rounded up by CMS; when it disagrees
  beyond rounding, strength decides. On the Q4 2026 file: strength confirms the factor on 5,316 of
  7,403 rows; 25 rows disagree with strength (12 badly, flagged Low); 12 unresolvable conflicts.
  "UP TO x MG" descriptors skip the strength check.
- Structural inference (`converter/infer.py`): a fractional quantity can't be HCPCS units; +2 for whole
  items, +2 for a typical package count (0.01–12). Without financials the best result is Medium,
  except factor = 1 (both readings agree, High). Edge-case issues force Low; any penalty drops a level.
- Issues carry a severity: info / penalty / edge. Edge (Low): kit, multi-ingredient, unclassified
  HCPCS, HCPCS not in crosswalk for the NDC, multiple HCPCS without a code on the line, no crosswalk,
  factor conflict, unresolved NDC.
- `ReferenceStore` lookups are indexed equality queries plus a per-store cache (refresh builds the
  indexes); about 16 ms per new NDC, near zero for repeats.
- `scripts/simulate_packages.py` (needs the live store, ~1 min): one-package claims for all 7,402
  NDC/HCPCS pairs in both unit bases. 14,804 claims: 10,306 correct, 4,494 undecided (Low), 4 wrong.
  Undecided cases are products where both readings give whole vials (e.g. factor 2 with 5 mL vials);
  that is what the milestone 3 price check has to resolve.
- Avastin JW sample lines stay undecided until milestone 3 (price) / 4 (35 + 5 JW = 4 whole vials).

### Milestone 3 notes

- `converter/financial.py`: amount used is ingredient cost (excludes the fee), else allowed amount
  minus fee, else billed amount minus fee. Implied price per HCPCS unit vs ASP payment limit and per
  NDC unit vs NADAC (NADAC in effect on the date of service). Fits when inside +/-25% (configurable
  `Settings.tolerance_band`); "near the edge" beyond 80% of the band. Billed charges get a one-sided
  markup test (0.75x to 5x the reference) instead, and cap confidence at Medium.
- NADAC's pricing unit is the NCPDP billing unit; where it disagrees with the FDA-derived unit
  (~530 NDCs) the NADAC check is skipped with a penalty. Possible later improvement: trust NADAC's unit.
- When ASP and NADAC point to different readings, follow the reference that matches the amount
  (ingredient cost -> NADAC, allowed/billed -> ASP) and drop one level. Real example: Remicade NADAC is
  ~$1,136/vial but ASP is ~$299/vial (ASP is net of rebates), so they can disagree legitimately.
- `infer.combine`: High = structure and price agree inside the band; Medium = only one is decisive,
  near the band edge, or billed-only; Low = they conflict (we follow price unless the price reading is
  structurally impossible). Price fitting neither reading drops one level. Edge-case issues still force Low.
- Multi-HCPCS NDCs without a code on the line are ranked by price fit, then structural fit (still Low).
- Priced simulation (`scripts/simulate_packages.py --priced`, allowed = ASP x true units +/-10%):
  14,804 claims, 14,762 correct (99.7%; 58% High, 31% Medium, 10% Low because of edge-case flags),
  41 undecided (factor close to 1, e.g. 1.08, where price can't separate the readings), 1 wrong.
  Unpriced, the same run gives 70% correct, 30% undecided.
- Only one ASP quarter is loaded; a date of service outside it gets an info note. Loading several
  quarters would let the ASP check use the price in effect on the DOS.

### Milestone 4 notes

- Entry point for batches (and the UI): `converter.aggregate.convert_lines(lines, ref)` returns one
  `GroupResult` per group: the conversion of the aggregated total plus a `LineShare` per original
  line (role, NDC units, HCPCS units) and `related` keys for KP/KQ partners.
- Groups: claim ID + DOS + HCPCS + 11-digit NDC (the factor is per NDC). Lines without a claim ID,
  and ambiguous 10-digit NDCs, stand alone. Amounts are summed only when every line in the group has them.
- JW waste is added to the administered units, and adds a structural check: under the right reading
  the waste is less than one vial (nobody discards a full unopened vial). This settles cases where both
  readings give whole vials (e.g. Avastin 35 + 5 JW: 0.5 vial of waste vs 1.25 vials).
- Flags: JW + JZ together, JW without an administered line (Low); duplicate administered / same-side /
  JW lines (penalty); JZ on a single-dose product with a partial vial (penalty, a JW line may be
  missing); single-dose partial vial with no JW/JZ on a medical line (info: Medicare requires one).
- RT + LT lines are summed and broken down per eye.
- KP / KQ stay separate (different HCPCS) but are linked per claim + DOS. When they share a
  combination NDC, both must imply the same NDC quantity; an undecided line (e.g. albuterol, factor
  0.83/mL, too close to 1) takes the reading that matches its partner. Combination NDCs are
  multi-ingredient, so these lines still end Low under the v1 edge-case rule. The ASP crosswalk has no
  combination NDCs (inhalation combos are mostly DME-priced), so this is tested on synthetic data only.
- Samples: all 12 lines (10 claims) in `samples/synthetic_claims.csv` get the expected basis, including
  JW, RT/LT and JZ claims.

### Milestone 5 notes

- `app.py`: sidebar (privacy notice, tolerance band and typical-packages settings, data vintage),
  tabs Single line (form with examples, optional JW waste quantity and financials), Batch CSV (upload
  or run the sample, summary metrics, results table, per-line breakdown, CSV downloads, drill-down
  into one result) and How it works. Batch is capped at 5,000 lines; results are cached per file and
  settings. One DuckDB connection is shared behind a lock.
- `converter/report.py`: CSV header aliases (qty, dos, hcpcs_code, allowed…), patient-column refusal,
  per-row read errors, flat result rows for groups and lines.
- Deployment data: the full store is ~230 MB, so `python -m data.build_app_store` exports a slim copy to
  `data/app/*.parquet` (~20 MB, committed: only the columns the converter reads, NADAC deduped to one
  row per NDC and effective date). The app materialises an indexed DuckDB file from it in a temp dir on
  first start (~4 s). Locally the full store wins; `DUC_USE_BUNDLED=1` forces the bundled path
  (`.claude/launch.json` config "streamlit-bundled").
- Checked in the browser against the bundled store: Avastin 35 + 5 JW example (16 mL / 40 units, High),
  sample batch (12 lines, 10 results, 7 High / 3 Medium), result drill-down (Eylea RT/LT, High).
- Deploy steps are in README.md: push to GitHub, create the app on share.streamlit.io (main file
  `app.py`, Python 3.11).
- Live on Streamlit Community Cloud from GitHub `kshar00/drug-unit-converter`, branch `main`
  (deployed with Streamlit's default Python rather than 3.11; works).
- A UI redesign for a more professional look is running in its own session (owns `app.py` and
  `.streamlit/`), including the unit-of-measure select in the single-line form.

### Unit of measure (added after milestone 5)

- New optional input `ClaimLine.unit_of_measure` (CSV column `unit_of_measure`, aliases uom / unit /
  unit_qualifier…): NCPDP EA / ML / GM or 837 qualifiers UN / ML / GR / F2 / ME. Logic in `converter/uom.py`.
- Used as a weak hint (+1 to a reading, vs +2 for structural checks), so it breaks ties but can't
  overturn structure or price:
  - EA / ML / GM equal to the product's NCPDP unit -> hints NDC units.
  - EA / ML / GM not equal to it -> penalty `uom_mismatch` (e.g. ML on a powder vial billed EA), no hint.
  - UN -> no hint (used for both "each" and HCPCS service units, often a default).
  - ME / F2 when the HCPCS descriptor is exactly 1 mg / 1 unit -> hints HCPCS units.
  - Final basis disagreeing with the hint -> penalty `uom_conflict`; mixed units within a group -> penalty.
- Samples now carry a unit_of_measure column; SYN-0011 shows the ML-on-powder-vial mismatch.

## Agent team (Claude Code subagents)

Recommendation: start with two or three agents (data engineer, developer, code reviewer), get the flow working, then add the data analyst. Each extra agent adds token cost and coordination overhead.

Subagents live as markdown files in `.claude/agents/`. The main Claude Code session acts as the project lead: it reads this spec, breaks milestones into tasks, delegates to the agents, and keeps the plan up to date.

| Agent | File | Responsibility | Tools |
| --- | --- | --- | --- |
| Project lead | main session | Plans milestones, delegates, merges work, keeps CLAUDE.md current | All |
| Data engineer | `.claude/agents/data-engineer.md` | Downloads FDA NDC Directory, CMS crosswalk, ASP and NADAC files; cleans them and loads them into DuckDB; owns `data/refresh.py` | Read, Write, Edit, Bash |
| Developer | `.claude/agents/developer.md` | Writes conversion, aggregation and inference code in `converter/`, plus unit tests | Read, Write, Edit, Bash |
| Code reviewer | `.claude/agents/code-reviewer.md` | Read-only review of logic, edge cases, tests and readability; reports issues, never edits | Read, Grep, Glob |
| Data analyst (add later) | `.claude/agents/data-analyst.md` | Builds synthetic claims, runs them through the tool, checks conversions and confidence scores, reports anomalies | Read, Bash, Write (samples only) |

### Example agent file (code reviewer)

```markdown
---
name: code-reviewer
description: Reviews Python changes in converter/ for correctness against the spec. Use after the developer finishes a task.
tools: Read, Grep, Glob
---
You are a senior Python reviewer with PBM domain knowledge.
Check every change against CLAUDE.md: NDC normalisation, conversion factor maths,
modifier aggregation (JW, JZ, RT, LT, KP, KQ), confidence scoring and tolerance bands.
Flag missing tests and unhandled edge cases (kits, multi-HCPCS NDCs, missing crosswalk rows).
Never edit files. Return a short list of issues, most serious first.
```

### Workflow

1. Lead picks the next milestone and splits it into tasks.
2. Data engineer prepares any reference data the task needs.
3. Developer implements and writes tests.
4. Code reviewer checks the change; developer fixes issues.
5. Data analyst (once added) validates results on synthetic claims.
6. Lead updates milestone status in this file.
