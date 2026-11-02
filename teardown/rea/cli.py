"""rea: Reverse Engineer Anything, a sourced teardown engine.

  python -m rea repo OWNER/NAME [--fixture FILE] [--format md|json] [--out FILE]
  python -m rea deps OWNER/NAME [--max-lookups N] [--include-dev] [--fixture FILE] [--registry-fixture FILE]
  python -m rea company DOSSIER.json [--format md|json] [--out FILE]
  python -m rea verify DOSSIER.json [--fixture PAGES.json]       (exit 3 if any quote is unconfirmed)
  python -m rea diff OURS.json THEIRS.json                         (two reports from --format json)
  python -m rea draft --entity NAME --source URL [--source URL ...]  (needs `pip install anthropic`)
  python -m rea rank FIGURES.json [--format md|json] [--out FILE]
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

from . import render
from .company import load_dossier_file
from .diff import diff, diff_md
from .draft import DEFAULT_MODEL, DraftError, draft_dossier, sdk_complete
from .deps import FixtureRegistry, Registry, deps_for_repo, teardown_deps
from .evidence import EvidenceError
from .figures import Figure
from .github import Client, FixtureClient, GitHubError, NotFound
from .repo_teardown import teardown_repo
from .verify import Fetcher, FixtureFetcher


def _write(text: str, out: str | None) -> None:
	if out:
		with open(out, "w", encoding="utf-8") as fh:
			fh.write(text if text.endswith("\n") else text + "\n")
	else:
		sys.stdout.write(text if text.endswith("\n") else text + "\n")


def _load(path: str):
	with open(path, encoding="utf-8") as fh:
		return json.load(fh)


def main(argv: list[str] | None = None) -> int:
	p = argparse.ArgumentParser(prog="rea", description="Sourced teardowns: every claim carries a receipt.")
	sub = p.add_subparsers(dest="cmd", required=True)
	r = sub.add_parser("repo", help="tear down a public GitHub repository")
	r.add_argument("repo", help="owner/name")
	r.add_argument("--fixture", help="recorded API responses (JSON) instead of the network")
	r.add_argument("--deps", action="store_true", help="also tear down dependencies and their licenses")
	dp = sub.add_parser("deps", help="dependency list with a license per dependency")
	dp.add_argument("repo", help="owner/name")
	dp.add_argument("--fixture", help="recorded GitHub API responses (JSON)")
	for sp in (r, dp):
		sp.add_argument("--registry-fixture", help="recorded registry responses (JSON: url -> body)")
		sp.add_argument("--max-lookups", type=int, default=40, help="registry lookups at most (default 40)")
		sp.add_argument("--include-dev", action="store_true", help="also look up dev dependencies")
		sp.add_argument("--max-pom-fetches", type=int, default=25, help="Maven parent/BOM POMs read from Maven Central at most (default 25)")
	c = sub.add_parser("company", help="tear down a company or product from a sourced dossier")
	c.add_argument("dossier")
	c.add_argument("--verify", action="store_true", help="fetch each source and keep a claim only if its quote is on the page")
	v = sub.add_parser("verify", help="check that every source page still contains its quote")
	v.add_argument("dossier")
	for sp in (c, v):
		sp.add_argument("--pages-fixture", help="recorded pages (JSON: url -> text) instead of the network")
	df = sub.add_parser("diff", help="compare two JSON teardowns into a gap list")
	df.add_argument("ours")
	df.add_argument("theirs")
	dr = sub.add_parser("draft", help="draft a sourced dossier from web pages with Claude; every quote is checked on its page")
	dr.add_argument("--entity", required=True)
	dr.add_argument("--source", action="append", default=[], help="page URL (repeat for several)")
	dr.add_argument("--as-of")
	dr.add_argument("--model", default=DEFAULT_MODEL)
	dr.add_argument("--pages-fixture", help="recorded pages (JSON: url -> text) instead of the network")
	dr.add_argument("--out")
	k = sub.add_parser("rank", help="rank figures within each metric kind")
	k.add_argument("figures")
	for sp in (r, dp, c, v, df, k):
		sp.add_argument("--format", choices=("md", "json"), default="md")
		sp.add_argument("--out")
	args = p.parse_args(argv)

	try:
		if args.cmd in ("repo", "deps"):
			client = FixtureClient(_load(args.fixture)) if args.fixture else Client()
			registry = FixtureRegistry(_load(args.registry_fixture)) if args.registry_fixture else Registry()
		if args.cmd == "repo":
			t = teardown_repo(client, args.repo)
			if args.deps:
				d = teardown_deps(client, t.repo, t.tree, t.top_level, registry, args.max_lookups, args.include_dev, args.max_pom_fetches)
				for claim in d.ledger.sourced + d.ledger.unverified:
					t.ledger.add(claim)
			_write(render.repo_md(t) if args.format == "md" else render.to_json(t), args.out)
		elif args.cmd == "deps":
			d = deps_for_repo(client, args.repo, registry, args.max_lookups, args.include_dev, args.max_pom_fetches)
			_write(render.deps_md(d) if args.format == "md" else render.to_json(d), args.out)
		elif args.cmd in ("company", "verify"):
			fetcher = None
			if args.cmd == "verify" or args.verify:
				fetcher = FixtureFetcher(_load(args.pages_fixture)) if args.pages_fixture else Fetcher()
			t = load_dossier_file(args.dossier, fetcher)
			if args.cmd == "company":
				_write(render.company_md(t) if args.format == "md" else render.to_json(t), args.out)
			else:
				text = "\n".join(render.checks_md(t.checks)) or "No sources to check."
				_write(text if args.format == "md" else json.dumps([asdict(c) for c in t.checks], indent=2), args.out)
				if any(c.status != "verified" for c in t.checks):
					return 3
		elif args.cmd == "draft":
			fetcher = FixtureFetcher(_load(args.pages_fixture)) if args.pages_fixture else Fetcher()
			res = draft_dossier(args.entity, args.source, fetcher, sdk_complete(args.model), args.as_of)
			_write(json.dumps(res.dossier, indent=2), args.out)
			print(f"rea: {res.kept} item(s) kept with a confirmed quote, {len(res.rejected)} left unverified; model {res.model}", file=sys.stderr)
			for f in res.failed_sources:
				print(f"rea: could not fetch {f['url']}: {f['reason']}", file=sys.stderr)
		elif args.cmd == "diff":
			result = diff(_load(args.ours), _load(args.theirs))
			if args.format == "md":
				_write(diff_md(result), args.out)
			else:
				if "rows" in result:
					result["rows"] = [{**asdict(r), "verdict": r.verdict} for r in result["rows"]]
				_write(json.dumps(result, indent=2), args.out)
		else:
			with open(args.figures, encoding="utf-8") as fh:
				figures = [Figure.from_dict(f) for f in json.load(fh)["figures"]]
			text = "\n".join(render.figures_md(figures)) if args.format == "md" else render.ranking_json(figures)
			_write(text, args.out)
	except NotFound as err:
		print(f"rea: not found: {err}", file=sys.stderr)
		return 2
	except (GitHubError, EvidenceError, DraftError, ValueError, OSError) as err:
		print(f"rea: {err}", file=sys.stderr)
		return 1
	return 0
