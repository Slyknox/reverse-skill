# rea: Reverse Engineer Anything

A teardown engine where every claim carries a receipt. Point it at a public
GitHub repository or a sourced dossier about a company or product. It gives
you a pros and cons matrix and an original build plan: what to keep, and what
to fix, each with a measure of done.

It is a standalone tool (Python 3.11+). Everything uses only the standard
library except `draft` (needs the Anthropic SDK), PDF sources (need pypdf) and
scanned PDFs (also need Pillow and the `tesseract` command). It is separate from the
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
`Cargo.toml`, `go.mod`, `pom.xml`, `*.csproj` and `Gemfile` anywhere in the tree
(at most 10 manifests). It then looks up each runtime dependency's license:
npm, PyPI, crates.io and RubyGems directly, and Go, Maven and NuGet through
deps.dev. Every answer links to the response it came from. deps.dev answers
per version. A declared version that names one release is looked up as is. A
Maven or NuGet range such as `[3.1,4.0)` resolves to the version the tool
itself would pick from the published list (Maven's order is a port of Maven
3.9.9's ComparableVersion, checked pair by pair against that class): the highest for Maven, the lowest
for NuGet. NuGet leaves out pre-releases unless a bound is one, and Maven, as
Maven does, can pick a pre-release of the upper bound. An unresolved property
uses the package's default version. The dependency table says which happened. When several licenses are listed, all of them
apply. It flags copyleft licenses, dependencies with no identified license, and
packages not on the registry. For Maven, a dependency without a version gets
one the way Maven would: from the parent POM (read from the repository when
`relativePath` points at it, else from Maven Central), from BOMs imported in
`dependencyManagement`, with `${properties}` taken from the whole chain and
the child's values winning. The table names the POM each version came from.

Maven profiles are applied the way Maven would for a build you describe:
`-P id` / `-P !id`, `-D name=value`, `--jdk 21`, `--os-family unix`,
`--os-name`, `--os-arch`. JDK prefixes and ranges, OS, property and file
conditions are evaluated. File conditions are checked against the repository's
file listing, and all conditions in one profile must hold. A condition your
flags do not decide is "unknown". Such a profile is not applied, and the report
says which flag would decide it. An `activeByDefault` profile applies only when
no other profile in that POM is on, and the report warns when an unknown one
could turn it off.
BOMs whose groupId matches an unmanaged dependency are read first, and
`--max-pom-fetches` (default 25) caps Maven Central reads. Anything left over
is reported as unresolved, not guessed. The project's own local and workspace packages
(Cargo `path`, npm `workspace:`/`file:`, Gemfile `path:`) are marked `local`
and never looked up. `--max-lookups` (default 40) caps calls, and
`--include-dev` adds dev dependencies.

**`company`** never researches on its own. You supply claims and figures, each
with `sources: [{url, quote}]`. Claims without an https source are listed as
unverified and kept out of the matrix and the plan. With `--verify`, each
source page is fetched, and a claim keeps its place only if every quote is
found on its page.

**`verify`** runs that check on its own and exits 3 if any quote is not
confirmed, which makes it usable in CI. Pages are fetched only over https from
public hosts (a redirect anywhere else is refused), up to 5 MB each. Matching
is exact after evening out case, whitespace, curly quotes, dashes and HTML
entities. PDF sources (up to 25 MB and 500 pages) are read with the optional
`pypdf` package (`pip install pypdf`). A word broken across lines with a hyphen
matches with the hyphen dropped ("million") or kept ("cloud-based"). Without
pypdf, or for an encrypted PDF, the source is reported as needing a check by
hand rather than guessed. A scanned PDF with no text layer (up to 30 pages) is
read with OCR when the `tesseract` command and Pillow are installed. A match in
OCR text is still marked for confirming by eye, because OCR can misread.

**`diff`** compares two reports made with `--format json`. Repo reports are
compared check by check (gap, lead, both have, both lack). Company reports are
shown side by side by kind. Above that is a list of suggested matches: pairs of
sourced claims whose content words overlap (Dice score of 0.4 or more),
one-to-one. Before scoring, a short published vocabulary (`CONCEPTS` in
`rea/diff.py`) maps synonyms and opposites to shared concepts, so "setup" and
"onboarding" match, and "slow" and "fast" both count as speed. A weakness of
yours paired with a strength of theirs is a possible gap, and the reverse is a
possible lead. An "opposite on" column names concepts the two claims state in
opposite directions. These are suggestions to confirm. Wording outside the
vocabulary still has to overlap word for word, unless you add `--judge`.

With `--judge` (company dossiers only, same setup as `draft`), the sourced
claims the vocabulary left unmatched are sent to Claude, which proposes pairs
by id. Code drops invented ids, claims already used, and pairs whose kinds are
not a reported pairing. The meaning (gap, lead...) still comes from the two
claims' kinds, not from the model. Model-proposed pairs have no score and
show the model's one-line reason. With more than 300 claims on a side, nothing
is sent.

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
