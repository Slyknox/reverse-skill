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
		"Every line below cites the GitHub API response it came from. Tests and lockfiles are looked for across the whole file listing; vendored and generated folders are skipped.", "",
		"## Pros and cons", ""]
	out += _matrix(t.ledger)
	out += ["## Original build plan", "", "Fix weaknesses first, then keep strengths. Each item names how you will know it is done.", ""]
	out += _plan(t.plan())
	return "\n".join(out)


def checks_md(checks) -> list[str]:
	if not checks:
		return []
	from .verify import STATUSES

	ok = sum(1 for c in checks if c.status == "verified")
	out = ["## Quote check", "", f"{ok} of {len(checks)} source quotes were found on their pages. A claim with any unconfirmed quote is moved to Unverified.", "",
		"| Status | Source | Quote | Detail |", "|---|---|---|---|"]
	for c in checks:
		quote = (c.quote or "").replace("|", "\\|")
		quote = quote if len(quote) <= 80 else quote[:77] + "..."
		out.append(f"| {c.status} | {c.url} | {quote or '-'} | {c.reason or STATUSES.get(c.status, '')} |")
	return out + [""]


def company_md(t) -> str:
	out = [f"# Teardown: {t.entity}", "", f"Dossier as of: {t.as_of or 'unknown'}", ""]
	out += checks_md(t.checks)
	out += ["## Pros and cons", ""]
	out += _matrix(t.ledger)
	out += figures_md(t.figures, "Figures")
	out += ["## Original build plan", ""] + _plan(t.plan())
	return "\n".join(out)


def to_json(obj) -> str:
	payload = asdict(obj)
	payload.pop("tree", None)
	payload.pop("top_level", None)
	if hasattr(obj, "plan"):
		payload["plan"] = obj.plan()
	return json.dumps(payload, indent=2, default=str)


def ranking_json(figures: list[Figure]) -> str:
	return json.dumps({kind: [{**asdict(f), "rank": i + 1, "flags": f.flags} for i, f in enumerate(group)]
		for kind, group in rank(figures).items()}, indent=2)


def deps_md(d) -> str:
	out = [f"# Dependencies: {d.repo}", "", "Licenses come from the package registry; each is linked to the registry answer it came from.", "",
		"## Findings", ""]
	out += _matrix(d.ledger)
	if d.dependencies:
		out += ["## Dependency list", "", "| Name | Version spec | Ecosystem | Scope | Manifest | License | Class |", "|---|---|---|---|---|---|---|"]
		for dep in d.dependencies:
			lic = f"[{dep.license or 'not stated'}]({dep.license_source})" if dep.license_source else "not looked up"
			if dep.license_note:
				lic += f" ({dep.license_note})"
			out.append(f"| {dep.name} | `{dep.spec}` | {dep.ecosystem} | {dep.scope} | {dep.manifest} | {lic} | {dep.license_class if dep.looked_up else 'n/a'} |")
		out.append("")
	return "\n".join(out)
