"""Simulate a one-package claim for every NDC/HCPCS pair in the crosswalk, billed in NDC units and in
HCPCS units, and report how often the converter infers the basis correctly.

  --priced   also put an allowed amount on each claim: ASP payment limit x true HCPCS units, with
             +/-10% random noise (pairs without an ASP price are skipped)

Needs the live store (python -m data.refresh). Usage:
  python scripts/simulate_packages.py [--priced] [--seed N]
"""

import argparse
import collections
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from converter.convert import ClaimLine, convert_line  # noqa: E402
from converter.reference import ReferenceStore  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--priced", action="store_true")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    rng = random.Random(args.seed)

    t = time.time()
    outcome, conf, misses, examples = collections.Counter(), collections.Counter(), collections.Counter(), {}
    with ReferenceStore() as ref:
        pairs = ref._rows("SELECT DISTINCT ndc11, hcpcs FROM asp_crosswalk WHERE ndc11 IS NOT NULL", [])
        for p in pairs:
            base = convert_line(ClaimLine(ndc=p["ndc11"], quantity=1, hcpcs=p["hcpcs"]), ref)
            if not base.factor:
                outcome["no_factor"] += 1
                continue
            est = next(c for c in base.candidates if c.hcpcs == base.hcpcs)
            price = ref.asp_price(p["hcpcs"])
            if args.priced and not (price and price.get("payment_limit")):
                outcome["no_asp_price"] += 1
                continue
            for basis in ("NDC", "HCPCS"):
                q = est.ndc_units_per_package if basis == "NDC" else est.hcpcs_units_per_package
                if not q:
                    continue
                if basis == "HCPCS":
                    q = max(1, round(q))  # claims bill whole HCPCS units
                true_hcpcs = q if basis == "HCPCS" else q * est.factor
                kw = {}
                if args.priced:
                    kw["allowed_amount"] = round(price["payment_limit"] * true_hcpcs * rng.uniform(0.9, 1.1), 2)
                r = convert_line(ClaimLine(ndc=p["ndc11"], quantity=q, hcpcs=p["hcpcs"], **kw), ref)
                got = r.inferred_basis
                k = "correct" if got in (basis, "EITHER") else ("undecided" if got is None else "WRONG")
                outcome[(basis, k)] += 1
                conf[(k, r.confidence)] += 1
                if k != "correct":
                    key = (k, basis, r.ncpdp_unit)
                    misses[key] += 1
                    examples.setdefault(key, (q, p["hcpcs"], r.ndc_hyphenated, r.factor, r.reason[:200]))

    total = sum(v for k, v in outcome.items() if isinstance(k, tuple))
    print(f"pairs {len(pairs)}, claims {total}, priced={args.priced}")
    for k, v in sorted(outcome.items(), key=str):
        print(f"  {k}: {v}")
    print("by outcome and confidence:")
    for k, v in sorted(conf.items()):
        print(f"  {k}: {v} ({100 * v / total:.1f}%)")
    print("top misses:")
    for k, v in misses.most_common(8):
        print(f"  {v} {k} e.g. {examples[k]}")
    print(f"{time.time() - t:.0f}s")


if __name__ == "__main__":
    main()
