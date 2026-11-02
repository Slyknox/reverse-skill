# Reverse Engineer Anything (rea): spec

Status: 0.9.0, 2026-11-02 (eight roadmap rounds built). Self-contained tool. It is not part of the
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
| Tests and lockfiles are found anywhere in the tree; vendored folders are skipped; a truncated listing is disclosed | `tree.Tree`, `repo_teardown` | `test_nested_tests_and_lockfiles_found_and_vendored_folders_skipped`, `test_truncated_listing_is_disclosed` |
| Every dependency license links to the registry answer; packages not on the registry and unidentified licenses are kept apart; the lookup cap is reported | `deps` | `test_teardown_reads_nested_manifest_skips_vendored_and_cites_registry`, `test_lookup_cap_counts_the_rest` |
| With `--verify`, a claim stays sourced only if every quote is found on its fetched page; only https on public hosts is fetched | `verify`, `company.load_dossier(fetcher=...)` | `test_company_verify_moves_unconfirmed_claims_to_unverified`, `test_live_fetcher_refuses_non_public_and_plain_http` |
| Diffs compare only sourced claims and refuse to compare different report kinds | `diff` | `test_repo_gaps_and_leads`, `test_kind_mismatch_and_bad_input_are_refused` |
| A model-drafted claim keeps a source only if its quote is on a page the user named; nothing is truncated | `draft.draft_dossier` | `test_only_quotes_found_on_their_page_keep_a_source`, `test_refuses_without_pages_and_never_truncates` |
| Go, Maven and NuGet licenses come from deps.dev for the declared version; a range uses the default version and says so; several licenses all apply | `deps.resolve_license` | `test_exact_version_is_one_lookup`, `test_range_falls_back_to_default_version_and_says_so`, `test_multiple_licenses_are_all_applied` |
| The project's own local and workspace packages are never looked up | `deps.parse_manifest` | `test_local_path_dependencies_are_marked_local` |
| PDF quotes are checked on the whole document or not at all; a missing reader is reported, not guessed | `verify.pdf_to_text` | `test_quotes_match_across_line_end_hyphens_in_both_readings`, `test_missing_pypdf_is_reported_not_guessed` |
| Company diff suggests one-to-one matches from sourced claims only, labelled as suggestions | `diff.suggest_matches` | `test_pairs_are_one_to_one_and_cross_kind_pairs_mean_gap_or_lead`, `test_company_diff_shows_suggestions_from_sourced_claims_only` |
| Maven versions follow parent POMs and imported BOMs, with the child's properties winning; each version names its source POM; caps and failures are reported | `maven.Resolver` | `test_parent_management_uses_the_childs_property_override`, `test_imported_bom_supplies_versions_and_unrelated_boms_are_not_fetched_first`, `test_fetch_cap_is_reported_not_guessed` |
| A scanned PDF is OCRed only when tesseract is installed, and an OCR match is labelled for confirming by eye | `verify.ocr_pages` | `test_scanned_pdf_is_ocred_and_labelled`, `test_without_tesseract_a_scanned_pdf_is_reported` |
| Matching maps a published vocabulary of synonyms and opposites to concepts and names any concept stated in opposite directions | `diff.CONCEPTS`, `diff.opposed` | `test_synonyms_and_opposites_share_a_concept`, `test_gap_found_through_synonyms` |
| A Maven or NuGet range resolves to the release the tool would pick (Maven highest, NuGet lowest), and says so; an empty range says so | `versions.pick`, `deps.resolve_license` | `test_maven_takes_highest_nuget_takes_lowest`, `test_nuget_range_resolves_to_its_lowest_version_and_empty_range_says_so` |
| activeByDefault Maven profiles are applied and named; profiles that depend on the build machine are listed, not applied | `maven.parse_pom` | `test_active_by_default_profile_is_applied_and_others_are_listed` |
| A model judge only proposes pairs by id among unmatched sourced claims; ids, reuse and kind pairings are checked in code; nothing is sent past the size limit | `judge.judge_matches` | `test_model_adds_pairs_the_vocabulary_misses_and_only_sees_unmatched_claims`, `test_invented_ids_reuse_and_disallowed_kind_pairs_are_dropped`, `test_nothing_sent_when_too_many_claims_or_nothing_left` |
| Maven version order matches Maven 3.9.9's ComparableVersion on every recorded pair | `versions.maven_compare` | `test_every_recorded_pair_matches_maven`, `test_maven_test_suite_orders` |
| Profiles are evaluated against the build context given; an undecided condition is unknown, never assumed; activeByDefault is a fallback | `activation.evaluate`, `maven.parse_pom` | `test_conditions_with_a_full_context`, `test_default_profile_applies_only_when_nothing_else_is_on`, `test_unknown_conditions_are_reported_not_guessed` |
| `env.X` conditions use only the variables given with --env; `packaging` uses the POM's own packaging | `activation._property`, `activation._packaging` | `test_env_conditions_use_only_listed_variables`, `test_packaging_condition_uses_the_poms_packaging` |
| NuGet ordering, ranges, floats and best match follow NuGet.Versioning; every extracted row of its unit tests passes | `nuget` | `NuGetTestSuiteTests`, `NuGetBestMatchTests` |
| A lockfile beside the manifest (else at the root) pins the version every lookup is about, and the report says so | `deps._pin_from_lockfiles`, `lockfiles` | `test_lockfile_pins_the_version_and_the_lookup_uses_it`, `test_lockfile_in_the_manifests_folder_beats_the_root_one` |
| Composer, Swift, pub and Hex dependencies are parsed; vcs and local ones are never looked up; a Composer constraint resolves as Composer does | `manifests`, `deps.resolve_license` | `ParserTests`, `ComposerConstraintTests`, `test_vcs_and_local_dependencies_are_never_looked_up` |
| Only exact or locked versions are queried on OSV; every advisory is linked; OSV failing is "not checked", never "none" | `osv.check`, `osv.claims` | `test_only_exact_or_locked_versions_are_queried_and_findings_cite_osv`, `test_osv_failure_is_reported_as_not_checked` |
| The HTML report is self-contained and escapes every value | `report.render` | `test_each_kind_renders_self_contained_html` |
| Every package a lockfile resolved is a dependency (transitive, or transitive-dev where marked); declared ones are looked up first; OSV covers the whole tree and names the lockfile | `deps._pin_from_lockfiles`, `osv.Finding.via` | `test_lockfile_packages_become_transitive_dependencies`, `test_declared_dependencies_are_looked_up_before_transitive_ones`, `test_osv_covers_the_transitive_tree_and_names_the_lockfile` |
| Files past the contents API's 1 MB limit are read through the blob API by sha | `deps.teardown_deps.read_file` | `test_files_over_the_contents_limit_are_read_through_the_blob_api` |

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

## Roadmap

Built in 0.2.0:

1. `repo` reads tests and lockfiles across the whole tree through the git tree API.
2. `deps`: dependency teardown with a license per dependency from npm, PyPI and crates.io.
3. `verify` and `company --verify`: fetch each source and confirm its quote is on the page.
4. `diff`: two teardowns into a gap list.
5. `draft`: Claude proposes claims; each goes through the same quote check, so an unconfirmed claim stays unverified.

Built in 0.3.0:

6. License lookups for Go, Maven and NuGet (deps.dev) and RubyGems (rubygems.org); local and workspace packages excluded.
7. PDF sources for `verify` through the optional pypdf package.
8. Suggested matches between free-text claims in company diffs.

Built in 0.4.0:

9. Maven parent POMs and imported BOMs (in-repository and Maven Central), with Maven's property and management precedence.
10. OCR for scanned PDFs through the optional `tesseract` command.
11. Synonym- and opposite-aware claim matching from a small published vocabulary.

Built in 0.5.0:

12. Maven and NuGet version ranges resolved the way each tool resolves them.
13. Maven profiles marked activeByDefault applied; other profiles listed.
14. `diff --judge`: a model proposes matches beyond the vocabulary, checked in code and kept as suggestions.

Built in 0.6.0:

15. Maven profile activation evaluated against a build context you give (-P, -D, --jdk, --os-*), with file conditions checked against the repository.
16. Maven version order ported from Maven 3.9.9's ComparableVersion and checked against that class on 4,000 recorded pairs (all 323,610 pairs of an 805-version corpus matched during the build); the recorder is `tests/tools/maven_order/regenerate.py`. Maven's order is not transitive in some edge cases; the port reproduces that, pair by pair.

Built in 0.7.0:

17. `env.X` profile conditions from `--env NAME=value`, and Maven 4 `packaging` conditions from the POM itself.
18. NuGet: a port of NuGet.Versioning (version parsing and ordering, one-interval ranges, floating versions, IsBetter/FindBestMatch), checked against 144 InlineData rows from NuGet's own unit tests (`tests/tools/nuget_cases/extract.py` rebuilds the fixture). Running NuGet's real library was not possible in the build session, so this is checked against NuGet's test data, not against the library itself as Maven's port was.

Built in 0.8.0:

19. Composer, Swift Package Manager, pub and Hex manifests, with licenses from Packagist, GitHub, pub.dev and hex.pm.
20. Lockfiles for every ecosystem: versions are pinned before any lookup, and license lookups are version-specific where the registry allows.
21. OSV.dev known-vulnerability checks for every exact or locked version, with a plan item.
22. `rea report` and `--format html`: a self-contained HTML page for any result.

Built in 0.9.0:

23. The full lockfile tree: transitive packages (with the lockfile's own dev marking) checked against OSV and, within the cap, for licenses; OSV caps raised to 1,000 per batch and 5,000 per run; blob API fallback for files over 1 MB.

Blocked, not built:

- `draft` and `diff --judge` have not been run against the live API: the build session has no Anthropic credential (`ANTHROPIC_API_KEY` or an `ant auth login` profile). Both are tested with a fake model client, and SDK 2.3.0 accepted the request shape.
- A differential run against the real NuGet.Versioning library: the build session's policy refused both Microsoft's `dotnet-install.sh` and the direct download of the SHA-512-verified SDK archive. It needs a permission rule for that download, or a machine that has .NET.
- Swift packages hosted outside GitHub have no license lookup; GitHub lookups report the default branch's license, not the pinned release's.
