"""Compare two teardowns (JSON from --format json) into a gap list.

Repo reports are compared signal by signal, because each signal is a defined
check. Company dossiers are free text, so they are shown side by side by kind:
deciding whether two sentences describe the same thing is left to a person.
Only sourced claims are compared; unverified ones are counted, never matched.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Each check as (good signal, bad signal, label).
PAIRS = [
	("license_permissive", "license_missing", "Clear permissive license"),
	("active", "stale", "Recent activity"),
	("tests_present", "tests_missing", "Tests"),
	("ci_present", "ci_missing", "CI workflows"),
	("security_policy", "security_policy_missing", "Security policy"),
	("contributing", "contributing_missing", "Contributor guide"),
	("lockfile", "lockfile_missing", "Lockfile"),
	("releases", "releases_missing", "Tagged releases"),
	("docs", "docs_thin", "Documentation"),
	("contributors_broad", "bus_factor_low", "Contributor spread"),
	("deps_licenses_clear", "deps_license_unknown", "Dependency licenses identified"),
]


@dataclass
class Row:
	label: str
	ours: str  # has | lacks | unknown
	theirs: str
	ours_text: str | None = None
	theirs_text: str | None = None
	ours_evidence: list[str] = field(default_factory=list)
	theirs_evidence: list[str] = field(default_factory=list)

	@property
	def verdict(self) -> str:
		if self.ours == "lacks" and self.theirs == "has":
			return "gap"
		if self.ours == "has" and self.theirs == "lacks":
			return "lead"
		if self.ours == self.theirs == "has":
			return "both have"
		if self.ours == self.theirs == "lacks":
			return "both lack"
		return "not comparable"


def _report_kind(report: dict) -> str:
	if "repo" in report and "ledger" in report:
		return "repo"
	if "entity" in report and "ledger" in report:
		return "company"
	raise ValueError("not a rea JSON report (expected output of `rea repo|company --format json`)")


def _signals(report: dict) -> dict[str, dict]:
	return {c["signal"]: c for c in report["ledger"]["sourced"] if c.get("signal")}


def _state(signals: dict, good: str, bad: str) -> tuple[str, dict | None]:
	if good in signals:
		return "has", signals[good]
	if bad in signals:
		return "lacks", signals[bad]
	return "unknown", None


def diff_repos(ours: dict, theirs: dict) -> list[Row]:
	a, b = _signals(ours), _signals(theirs)
	rows = []
	for good, bad, label in PAIRS:
		sa, ca = _state(a, good, bad)
		sb, cb = _state(b, good, bad)
		if sa == sb == "unknown":
			continue
		rows.append(Row(label, sa, sb, ca and ca["text"], cb and cb["text"],
			[e["url"] for e in (ca or {}).get("evidence", [])], [e["url"] for e in (cb or {}).get("evidence", [])]))
	order = {"gap": 0, "lead": 1, "both lack": 2, "not comparable": 3, "both have": 4}
	return sorted(rows, key=lambda r: order[r.verdict])


def diff(ours: dict, theirs: dict) -> dict:
	kind_a, kind_b = _report_kind(ours), _report_kind(theirs)
	if kind_a != kind_b:
		raise ValueError(f"cannot compare a {kind_a} report with a {kind_b} report")
	name = (lambda r: r.get("repo") or r.get("entity"))
	result = {"kind": kind_a, "ours": name(ours), "theirs": name(theirs),
		"unverified": {"ours": len(ours["ledger"]["unverified"]), "theirs": len(theirs["ledger"]["unverified"])}}
	if kind_a == "repo":
		result["rows"] = diff_repos(ours, theirs)
	else:
		by_kind = {}
		for kind in ("strength", "weakness", "revenue_model", "fact"):
			by_kind[kind] = {side: [c for c in rep["ledger"]["sourced"] if c["kind"] == kind]
				for side, rep in (("ours", ours), ("theirs", theirs))}
		result["side_by_side"] = by_kind
	return result


def diff_md(result: dict) -> str:
	out = [f"# Diff: {result['ours']} (ours) vs {result['theirs']} (theirs)", ""]
	u = result["unverified"]
	if u["ours"] or u["theirs"]:
		out += [f"> Unverified claims are not compared: {u['ours']} in ours, {u['theirs']} in theirs.", ""]
	if result["kind"] == "repo":
		rows = result["rows"]
		gaps = [r for r in rows if r.verdict == "gap"]
		out += [f"{len(gaps)} gap(s) where they have something you lack.", "",
			"| Check | Verdict | Ours | Theirs |", "|---|---|---|---|"]
		for r in rows:
			cite = lambda text, ev: (text or "not checked") + "".join(f" [src]({u})" for u in ev[:1])
			out.append(f"| {r.label} | {r.verdict} | {cite(r.ours_text, r.ours_evidence)} | {cite(r.theirs_text, r.theirs_evidence)} |")
		out.append("")
		if gaps:
			out += ["## Close the gaps", ""] + [f"{i}. {r.label}: they have \"{r.theirs_text}\"" for i, r in enumerate(gaps, 1)] + [""]
	else:
		out += ["Free-text claims are listed side by side; matching them is left to you.", ""]
		titles = {"strength": "Pros", "weakness": "Cons", "revenue_model": "How it makes money", "fact": "Facts"}
		for kind, sides in result["side_by_side"].items():
			if not sides["ours"] and not sides["theirs"]:
				continue
			out += [f"## {titles[kind]}", "", "**Ours**", ""]
			out += [f"- {c['text']}" for c in sides["ours"]] or ["- none sourced"]
			out += ["", "**Theirs**", ""]
			out += [f"- {c['text']}" for c in sides["theirs"]] or ["- none sourced"]
			out.append("")
	return "\n".join(out)
