# rea: Reverse Engineer Anything

A teardown engine where every claim carries a receipt. Point it at a public
GitHub repository or a sourced dossier about a company or product. It gives
you a pros and cons matrix and an original build plan: what to keep, and what
to fix, each with a measure of done.

It is a standalone tool (Python 3.11+, standard library only). It is separate
from the reverse-skill security router and not routed by it. Design and rules:
[SPEC.md](SPEC.md).

## Commands

Run these from this `teardown/` directory.

```bash
python3 -m rea repo owner/name                 # live, read-only GitHub API (GITHUB_TOKEN optional)
python3 -m rea repo owner/name --format json --out report.json
python3 -m rea company examples/dossier-template.json
python3 -m rea rank examples/top-arr-2026-unverified.json
```

`repo` reads repository metadata, languages, top-level files, `.github`,
workflows, releases and contributors, then reports license, activity, tests,
CI, security policy, contributor guide, lockfiles, docs, releases and bus
factor. It only checks the top level and says so.

`company` never researches on its own. You supply claims and figures, each with
`sources: [{url, quote}]`. Claims without an https source are listed as
unverified and kept out of the matrix and the plan.

`rank` ranks figures only within the same metric kind (`arr`,
`annualized_run_rate`, `revenue_guidance`, `acv`, `product_revenue_run_rate`,
`analyst_estimate_arr`), by the low end of each range. Each row is flagged
`UNVERIFIED`, `DISPUTED` (high/low above 1.25) or `ESTIMATE` where that applies.

Exit codes: 0 ok, 1 invalid input or a GitHub/network error, 2 not found.

## Tests

```bash
python3 -m unittest discover -s tests
```

The tests use recorded fixtures and need no network.
