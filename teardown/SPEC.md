# Reverse Engineer Anything (rea): spec

Status: MVP 0.1.0, 2026-11-02. Self-contained tool. It is not part of the
reverse-skill security router and is not wired into `skills/config/routing.json`.

## Problem

Competitive teardowns are usually written fast and checked rarely. The answer
that prompted this tool showed the four usual faults:

1. **The ranking contradicted its own numbers.** Palantir at about $8.15B was
   placed below Databricks ($6.9B). Sierra (about $150M) was in the top 10
   while larger companies were left out.
2. **It mixed different measures.** ARR, revenue guidance, contract value and
   product revenue were ranked against each other.
3. **Disputed ranges were quoted as one figure.** Ranges such as $47B to $74B
   mean the sources disagree.
4. **There were no receipts.** Most figures fell after the author's knowledge
   cutoff and had no source.

## Design rules (each enforced in code and covered by a test)

| Rule | Where | Test |
|---|---|---|
| A claim with no https source never reaches the matrix or the plan; it is listed as unverified | `evidence.Ledger` | `test_unsourced_and_non_https_claims_are_unverified`, `test_unsourced_claims_never_reach_the_plan` |
| Every figure declares one of 6 metric kinds; ranking happens only within a kind | `figures.rank` | `test_ranks_only_within_one_metric_kind` |
| A range with high/low above 1.25 is DISPUTED and ranked by its low end | `Figure.disputed` | `test_wide_range_is_disputed_and_ranked_by_low_end` |
| Mixed units, unknown kinds and inverted ranges are refused | `Figure.__post_init__`, `rank` | `test_invalid_figures_and_mixed_units_are_refused` |
| Repo checks cite the API response they read and say what was not inspected | `repo_teardown` | `test_strong_repo_reads_dot_github_and_workflows` |
| Plan items come only from sourced claims, fixes before keeps, each with a measure of done | `RepoTeardown.plan`, `CompanyTeardown.plan` | `test_weak_repo_signals_and_fix_items_come_first` |
| The engine never invents company facts; a weakness without a countermeasure becomes an explicit TODO | `company.load_dossier` | `test_unsourced_claims_never_reach_the_plan` |

## What "better" means here

"100x better" is not a testable goal, so these are the measures instead:

- **Receipt coverage:** 100% of claims in the matrix and plan carry a source link.
  This is enforced, not measured.
- **Like-for-like comparison:** 0 cross-kind rankings. Also enforced.
- **Honest failure:** an unsourced, disputed or estimated figure is always
  flagged in the output, never silently averaged.
- **Actionable output:** every plan item states a step, a measure of done and
  the evidence it rests on.

## Patterns from the platforms in the pasted list

These are claims from the pasted answer and have not been verified. They shape
the roadmap, not any output:

| Pattern | Seen at (claimed) | Pro | Con | How rea applies it |
|---|---|---|---|---|
| Narrow agent per task | Harvey, Sierra | Easier to evaluate and trust | Many agents to maintain | One command per teardown type; each rule tested on its own |
| Human checkpoint where the system is least sure | Harvey, Sierra | Catches costly errors | Adds latency | Unverified and disputed items are surfaced for a person, never auto-resolved |
| Charge for work done, not seats | Sierra | Aligns price with value | Revenue is harder to forecast | Not applied (rea is a free tool) |
| Ontology plus forward-deployed engineers | Palantir | Deep fit to the customer | Expensive and slow to scale | Not applied; dossiers are the lightweight equivalent |
| Usage-based billing | Databricks, Snowflake | Low entry cost | Bill shock | Not applied |
| Agents inside the system of record | Salesforce | Data is already there | Lock-in | Output is plain Markdown/JSON, so nothing is locked in |
| Whole-codebase indexing | Cursor | Better context | Privacy and cost | `repo` reads only public API metadata today (see roadmap) |

## Roadmap (not built)

1. `repo`: read nested lockfiles and test folders in monorepos through the git tree API.
2. `repo`: dependency teardown (manifests to dependency list, with license per dependency).
3. `company`: optional fetch-and-quote check that confirms each source URL still contains its quote.
4. Diff two teardowns (yours against a competitor's) into a gap list.
5. An LLM-assisted dossier drafter whose output goes through the same ledger, so an unsourced model claim stays unverified.
