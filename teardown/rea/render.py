"""Markdown and JSON output."""

from __future__ import annotations

import json
from dataclasses import asdict

from .evidence import Claim, Ledger
from .figures import METRIC_KINDS, Figure, rank


def _cite(claim: Claim) -> str:
	return " ".join(f"[{i + 1}]({e.url})" for i, e in enumerate(claim.evidence))


def _matrix(ledger: Ledger) -> list[str]:
	out = []
	for kind, title in (("strength", "Pros"), ("weakness", "Cons"), ("revenue_model", "How it makes money"), ("fact", "Facts")):
		claims = ledger.of_kind(kind)
		if claims:
			out += [f"### {title}", ""] + [f"- {c.text} {_cite(c)}" for c in claims] + [""]
	if ledger.unverified:
		out += ["### Unverified (not used in the matrix or the plan)", ""]
		out += [f"- [{c.kind}] {c.text}" for c in ledger.unverified] + [""]
	return out


def _plan(items: list[dict]) -> list[str]:
	if not items:
		return ["No plan items: no sourced strengths or weaknesses.", ""]
	out = ["| # | Action | Step | Measure of done | Because |", "|---|---|---|---|---|"]
	for i, it in enumerate(items, 1):
		because = it["because"] + " " + " ".join(f"[src]({u})" for u in it["evidence"])
		out.append(f"| {i} | {it['action']} | {it['step']} | {it.get('measure', 'define one')} | {because} |")
	return out + [""]


def _money(v: float) -> str:
	return f"${v / 1000:.2f}B" if v >= 1000 else f"${v:.0f}M"


def figures_md(figures: list[Figure], title: str = "Ranking by metric kind") -> list[str]:
	if not figures:
		return []
	out = [f"## {title}", "",
		"Each table ranks only figures of the same kind, by the low end of the range. Tables are not comparable with each other.", ""]
	unverified = sum(1 for f in figures if not f.verified)
	if unverified:
		out += [f"> {unverified} of {len(figures)} figures have no source and are marked UNVERIFIED. Do not quote them.", ""]
	for kind, group in rank(figures).items():
		out += [f"### {METRIC_KINDS[kind]}", "", "| Rank | Entity | Figure | As of | Flags | Sources |", "|---|---|---|---|---|---|"]
		for i, f in enumerate(group, 1):
			value = _money(f.low) if f.low == f.high else f"{_money(f.low)} to {_money(f.high)}"
			srcs = " ".join(f"[{j + 1}]({s.url})" for j, s in enumerate(f.sources)) or "none"
			out.append(f"| {i} | {f.entity} | {value} | {f.as_of or 'unknown'} | {', '.join(f.flags) or 'ok'} | {srcs} |")
		out.append("")
	return out


def repo_md(t) -> str:
	out = [f"# Teardown: {t.repo}", "", f"Source: {t.html_url}", "",
		"Every line below cites the GitHub API response it came from. Checks cover what the API shows at the top level; deeper structure is not inspected.", "",
		"## Pros and cons", ""]
	out += _matrix(t.ledger)
	out += ["## Original build plan", "", "Fix weaknesses first, then keep strengths. Each item names how you will know it is done.", ""]
	out += _plan(t.plan())
	return "\n".join(out)


def company_md(t) -> str:
	out = [f"# Teardown: {t.entity}", "", f"Dossier as of: {t.as_of or 'unknown'}", "", "## Pros and cons", ""]
	out += _matrix(t.ledger)
	out += figures_md(t.figures, "Figures")
	out += ["## Original build plan", ""] + _plan(t.plan())
	return "\n".join(out)


def to_json(obj) -> str:
	payload = asdict(obj)
	if hasattr(obj, "plan"):
		payload["plan"] = obj.plan()
	return json.dumps(payload, indent=2, default=str)


def ranking_json(figures: list[Figure]) -> str:
	return json.dumps({kind: [{**asdict(f), "rank": i + 1, "flags": f.flags} for i, f in enumerate(group)]
		for kind, group in rank(figures).items()}, indent=2)
