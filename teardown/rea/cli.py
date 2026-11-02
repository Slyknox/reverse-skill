"""rea: Reverse Engineer Anything, a sourced teardown engine.

  python -m rea repo OWNER/NAME [--fixture FILE] [--format md|json] [--out FILE]
  python -m rea company DOSSIER.json [--format md|json] [--out FILE]
  python -m rea rank FIGURES.json [--format md|json] [--out FILE]
"""

from __future__ import annotations

import argparse
import json
import sys

from . import render
from .company import load_dossier_file
from .evidence import EvidenceError
from .figures import Figure
from .github import Client, FixtureClient, GitHubError, NotFound
from .repo_teardown import teardown_repo


def _write(text: str, out: str | None) -> None:
	if out:
		with open(out, "w", encoding="utf-8") as fh:
			fh.write(text if text.endswith("\n") else text + "\n")
	else:
		sys.stdout.write(text if text.endswith("\n") else text + "\n")


def main(argv: list[str] | None = None) -> int:
	p = argparse.ArgumentParser(prog="rea", description="Sourced teardowns: every claim carries a receipt.")
	sub = p.add_subparsers(dest="cmd", required=True)
	r = sub.add_parser("repo", help="tear down a public GitHub repository")
	r.add_argument("repo", help="owner/name")
	r.add_argument("--fixture", help="recorded API responses (JSON) instead of the network")
	c = sub.add_parser("company", help="tear down a company or product from a sourced dossier")
	c.add_argument("dossier")
	k = sub.add_parser("rank", help="rank figures within each metric kind")
	k.add_argument("figures")
	for sp in (r, c, k):
		sp.add_argument("--format", choices=("md", "json"), default="md")
		sp.add_argument("--out")
	args = p.parse_args(argv)

	try:
		if args.cmd == "repo":
			if args.fixture:
				with open(args.fixture, encoding="utf-8") as fh:
					client = FixtureClient(json.load(fh))
			else:
				client = Client()
			t = teardown_repo(client, args.repo)
			_write(render.repo_md(t) if args.format == "md" else render.to_json(t), args.out)
		elif args.cmd == "company":
			t = load_dossier_file(args.dossier)
			_write(render.company_md(t) if args.format == "md" else render.to_json(t), args.out)
		else:
			with open(args.figures, encoding="utf-8") as fh:
				figures = [Figure.from_dict(f) for f in json.load(fh)["figures"]]
			text = "\n".join(render.figures_md(figures)) if args.format == "md" else render.ranking_json(figures)
			_write(text, args.out)
	except NotFound as err:
		print(f"rea: not found: {err}", file=sys.stderr)
		return 2
	except (GitHubError, EvidenceError, ValueError, OSError) as err:
		print(f"rea: {err}", file=sys.stderr)
		return 1
	return 0
