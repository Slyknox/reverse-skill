# rea: Reverse Engineer Anything

A teardown engine where every claim carries a receipt. Point it at a public
GitHub repository or a sourced dossier about a company or product. It gives
you a pros and cons matrix and an original build plan: what to keep, and what
to fix, each with a measure of done.

It is a standalone tool (Python 3.11+). Everything except `draft` uses only
the standard library; `draft` needs the Anthropic SDK. It is separate from the
reverse-skill security router and not routed by it. Design and rules:
[SPEC.md](SPEC.md).

## Commands

Run these from this `teardown/` directory. Every command takes
`--format md|json` (except `draft`, which always writes JSON) and `--out FILE`.

```bash
python3 -m rea repo owner/name                    # GitHub API teardown (GITHUB_TOKEN optional)
python3 -m rea repo owner/name --deps             # ... plus dependency licenses
python3 -m rea deps owner/name                    # dependency list, a license per dependency
python3 -m rea company dossier.json [--verify]    # teardown from a sourced dossier
python3 -m rea verify dossier.json                # does each source page still contain its quote?
python3 -m rea diff ours.json theirs.json         # two JSON reports -> gap list
python3 -m rea draft --entity "Acme" --source https://... --out dossier.json
python3 -m rea rank examples/top-arr-2026-unverified.json
```

**`repo`** reads repository metadata, languages, the recursive file listing,
`.github`, workflows, releases and contributors. It reports license, activity,
tests and lockfiles anywhere in the tree (vendored and generated folders such
as `node_modules` are skipped), CI, security policy, contributor guide, docs,
releases and bus factor. If GitHub truncates the listing, the report says so.

**`deps`** finds `package.json`, `pyproject.toml`, `requirements*.txt`,
`Cargo.toml` and `go.mod` anywhere in the tree (at most 10 manifests). It then
asks npm, PyPI or crates.io for each runtime dependency's license, linking
every answer to the registry response it came from. It flags copyleft
licenses, dependencies with no identified license, and packages not on the
registry. Go modules have no license API, so they are reported as not looked
up. `--max-lookups` (default 40) caps registry calls, and `--include-dev` adds
dev dependencies.

**`company`** never researches on its own. You supply claims and figures, each
with `sources: [{url, quote}]`. Claims without an https source are listed as
unverified and kept out of the matrix and the plan. With `--verify`, each
source page is fetched, and a claim keeps its place only if every quote is
found on its page.

**`verify`** runs that check on its own and exits 3 if any quote is not
confirmed, which makes it usable in CI. Pages are fetched only over https from
public hosts (a redirect anywhere else is refused), up to 5 MB each. Matching
is exact after evening out case, whitespace, curly quotes, dashes and HTML
entities. PDFs are reported as needing a check by hand.

**`diff`** compares two reports made with `--format json`. Repo reports are
compared check by check (gap, lead, both have, both lack). Company reports are
shown side by side by kind, because deciding whether two free-text claims mean
the same thing is left to you.

**`draft`** fetches the pages you name and asks Claude (`claude-opus-5-5` by
default, `--model` to change it) to propose claims and figures, each with a
word-for-word quote. Every quote is then checked against the fetched text. A
claim whose quote is not on its page, or that points at a page you did not
give, is written with no source, so `company` treats it as unverified. A
figure also needs a number in its quote. If the pages are too long, the
command refuses rather than cutting them. If the model declines a request, the
API's automatic fallback re-runs it on another model.

Setup for `draft` only:

```bash
pip install anthropic
```

The SDK finds credentials in `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, or
an `ant auth login` profile.

**`rank`** ranks figures only within the same metric kind (`arr`,
`annualized_run_rate`, `revenue_guidance`, `acv`, `product_revenue_run_rate`,
`analyst_estimate_arr`), by the low end of each range. Each row is flagged
`UNVERIFIED`, `DISPUTED` (high/low above 1.25) or `ESTIMATE` where that applies.

Exit codes: 0 ok, 1 invalid input or a network/API error, 2 not found, 3
(`verify` only) at least one quote not confirmed.

## A typical competitor teardown

```bash
python3 -m rea draft --entity "Competitor" --source https://competitor.example/pricing --source https://competitor.example/about --out competitor.json
# edit competitor.json by hand: fix, delete or re-source the unverified claims
python3 -m rea company competitor.json --verify
```

## Tests

```bash
python3 -m unittest discover -s tests
```

The tests use recorded fixtures and a fake model client, so they need no
network, no SDK and no API key.
