# Drug Unit Converter

Takes a drug claim line whose quantity unit basis is unknown and returns the NDC (NCPDP) unit
equivalent, the HCPCS billing unit equivalent, which one the quantity most likely was, and a
confidence level with reasons. Built only on public data (FDA NDC Directory, CMS ASP NDC-HCPCS
crosswalk and payment limits, Medicaid NADAC) and synthetic claims.

**No patient data.** The tool never asks for or stores patient name, ID, DOB, prescriber or Rx number,
and refuses uploads with patient-like columns.

See [CLAUDE.md](CLAUDE.md) for the full spec, method notes and milestone status.

## What it does

- Normalises any NDC format to 11 digits, including ambiguous unhyphenated 10-digit NDCs.
- Reads the FDA package description to get the package size and NCPDP unit (EA / ML / GM).
- Gets the HCPCS code and billing units from the CMS crosswalk, and cross-checks the conversion
  factor against the FDA labelled strength.
- Scores both readings of the quantity on structure (whole vials, package counts, whole HCPCS units,
  JW waste under one vial) and price (implied price vs ASP payment limit and NADAC, ±25% band).
- Combines JW / JZ waste lines, RT / LT laterality lines and links KP / KQ lines before converting.
- Single-line form and CSV batch upload with downloadable results.

On a simulation of one-package claims for all 7,402 NDC/HCPCS pairs in the Q4 2026 crosswalk, it
infers the right unit basis for 99.7% of claims when an allowed amount is present (70% without amounts,
with the rest flagged as undecided rather than guessed).

## Run locally

```bash
py -3.11 -m venv .venv
.venv/Scripts/python -m pip install -r requirements.txt
.venv/Scripts/python -m streamlit run app.py
```

The app uses `data/reference.duckdb` if you have built it, otherwise it builds an indexed store from
the bundled Parquet files in `data/app/` on first start (a few seconds).

## Refresh the reference data

```bash
.venv/Scripts/python -m data.refresh
```

Downloads the latest public files into `data/raw/` and loads `data/reference.duckdb` (about a minute).
Options: `--sources fda crosswalk asp nadac`, `--offline` (reuse downloads), `--crosswalk-url / --asp-url
/ --nadac-url` (pin a file). Then regenerate the bundled copy the deployed app uses:

```bash
.venv/Scripts/python -m data.build_app_store
```

## Deploy (Streamlit Community Cloud)

1. Push this repository to GitHub (the bundled `data/app/*.parquet`, ~20 MB, is committed; the raw
   downloads and the full DuckDB file are not).
2. On share.streamlit.io, create an app from the repository, main file `app.py`, Python 3.11.
3. The first start builds the reference store from `data/app/` and caches it for the container.

To update the deployed data: run the two commands above, commit `data/app/`, push.

## Tests and validation

```bash
.venv/Scripts/python -m pytest
.venv/Scripts/python scripts/simulate_packages.py --priced
```

The simulation needs the full local store (`python -m data.refresh`).
