"""Compare two teardowns (JSON from --format json) into a gap list.

Repo reports are compared signal by signal, because each signal is a defined
check. Company dossiers are free text, so they are shown side by side by kind,
with suggested matches from word overlap after mapping a small published
vocabulary of synonyms and opposites (CONCEPTS) to shared concepts. A
suggestion is a pointer for a person to confirm, never a merge: the verdict on
a pair stays "possible".
Only sourced claims are compared; unverified ones are counted, never matched.
"""

from __future__ import annotations

import re
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


# ---------- suggested matches for free-text claims ----------

STOPWORDS = set("""a an and are as at be been but by can do for from has have in into is it its of on or our
over per so than that the their them they this to up was we were what when which while who will with you your
more most less very also only just not no all any each other such""".split())
MATCH_THRESHOLD = 0.4

# (our kind, their kind) -> what a match between them means
PAIRINGS = {
	("strength", "strength"): "both have",
	("weakness", "weakness"): "both lack",
	("weakness", "strength"): "possible gap",
	("strength", "weakness"): "possible lead",
	("revenue_model", "revenue_model"): "similar model",
	("fact", "fact"): "same topic",
}


# A small, published vocabulary: each word maps to a concept, and words with an opposite
# meaning share the concept with a polarity (+ or -). Matching compares concepts, so
# "fast setup" and "slow onboarding" overlap; polarity is reported, never used to merge.
# Extend these lists as you meet new wording; there is no model behind them.
CONCEPTS: dict[str, tuple[str, str]] = {}


def _group(concept: str, words: str, polarity: str = "") -> None:
	for w in words.split():
		CONCEPTS[w] = (concept, polarity)


_group("onboarding", "onboarding onboard setup implementation implement rollout activation install installation")
_group("price", "price pricing priced cost costs fee fees billing billed charge charges subscription")
_group("customer", "customer customers client clients user users buyer buyers account accounts")
_group("support", "support service helpdesk assistance")
_group("security", "security secure compliance compliant soc privacy encryption")
_group("integration", "integration integrations integrate connector connectors api apis")
_group("speed", "fast quick quickly rapid instant minutes", "+")
_group("speed", "slow slowly lengthy long weeks months delay delays delayed", "-")
_group("cost_level", "cheap cheaper affordable inexpensive low-cost free", "+")
_group("cost_level", "expensive costly pricey premium overpriced", "-")
_group("ease", "easy easier simple intuitive straightforward self-serve", "+")
_group("ease", "hard harder difficult complex complicated confusing steep", "-")
_group("reliability", "reliable stable dependable uptime", "+")
_group("reliability", "unreliable unstable outage outages downtime buggy bugs", "-")
_group("growth", "growth grew growing increase increased rising rose", "+")
_group("growth", "decline declined declining decrease decreased shrinking fell churn", "-")


def _stem(w: str) -> str:
	for suffix, keep in (("ing", 5), ("ed", 5), ("es", 5), ("s", 4)):
		if w.endswith(suffix) and len(w) - len(suffix) >= keep - len(suffix) + 1 and not w.endswith("ss"):
			return w[: -len(suffix)]
	return w


def _concepts(text: str) -> dict[str, str]:
	"""term or concept -> polarity ("" when the word carries none)."""
	out: dict[str, str] = {}
	for w in re.findall(r"[a-z0-9$%]+(?:[.-][a-z0-9]+)*", text.lower()):
		if w in STOPWORDS or not (len(w) > 2 or any(ch.isdigit() for ch in w)):
			continue
		hit = CONCEPTS.get(w) or CONCEPTS.get(_stem(w))
		if hit:
			concept, polarity = hit
			out[concept] = out.get(concept) or polarity
		else:
			out[_stem(w)] = ""
	return out


def _terms(text: str) -> set[str]:
	return set(_concepts(text))


def similarity(a: str, b: str) -> float:
	"""Dice overlap of content words after mapping synonyms and opposites to shared concepts, 0..1."""
	ta, tb = _terms(a), _terms(b)
	return 2 * len(ta & tb) / (len(ta) + len(tb)) if ta and tb else 0.0


def opposed(a: str, b: str) -> list[str]:
	"""Shared concepts stated with opposite polarity, e.g. "speed" for "fast setup" vs "slow onboarding"."""
	ca, cb = _concepts(a), _concepts(b)
	return sorted(c for c in ca.keys() & cb.keys() if ca[c] and cb[c] and ca[c] != cb[c])


def suggest_matches(ours: list[dict], theirs: list[dict], threshold: float = MATCH_THRESHOLD) -> list[dict]:
	"""One-to-one pairs, best score first, across the kind pairings above."""
	candidates = []
	for i, a in enumerate(ours):
		for j, b in enumerate(theirs):
			meaning = PAIRINGS.get((a["kind"], b["kind"]))
			if meaning:
				score = similarity(a["text"], b["text"])
				if score >= threshold:
					candidates.append((score, i, j, meaning))
	used_a, used_b, out = set(), set(), []
	for score, i, j, meaning in sorted(candidates, key=lambda c: (-c[0], c[1], c[2])):
		if i in used_a or j in used_b:
			continue
		used_a.add(i)
		used_b.add(j)
		out.append({"meaning": meaning, "score": round(score, 2), "ours": ours[i], "theirs": theirs[j],
			"shared_words": sorted(_terms(ours[i]["text"]) & _terms(theirs[j]["text"])),
			"opposite_on": opposed(ours[i]["text"], theirs[j]["text"])})
	return out


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
		result["suggested_matches"] = suggest_matches(ours["ledger"]["sourced"], theirs["ledger"]["sourced"])
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
		matches = result.get("suggested_matches", [])
		order = ["possible gap", "possible lead", "both lack", "both have", "similar model", "same topic"]
		out += ["## Suggested matches", "",
			f"Pairs whose wording overlaps (Dice score of {MATCH_THRESHOLD} or more, after mapping a short list of synonyms and opposites to shared concepts). Suggestions only: confirm each one. \"Opposite on\" names a concept the two claims state in opposite directions (for example fast versus slow).", ""]
		if matches:
			out += ["| Meaning | Score | Ours | Theirs | Shared words | Opposite on |", "|---|---|---|---|---|---|"]
			for m in sorted(matches, key=lambda m: (order.index(m["meaning"]), -m["score"])):
				src = lambda c: "".join(f" [src]({e['url']})" for e in c.get("evidence", [])[:1])
				out.append(f"| {m['meaning']} | {m['score']:.2f} | [{m['ours']['kind']}] {m['ours']['text']}{src(m['ours'])} | "
					f"[{m['theirs']['kind']}] {m['theirs']['text']}{src(m['theirs'])} | {', '.join(m['shared_words'])} | {', '.join(m.get('opposite_on', [])) or '-'} |")
			out.append("")
		else:
			out += ["No pairs overlap enough to suggest.", ""]
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
