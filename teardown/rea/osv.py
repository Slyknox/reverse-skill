"""Known-vulnerability lookups through OSV.dev.

Only dependencies with an exact version (declared or locked) are queried: OSV
answers per version, and a range says nothing about what is installed. Each
advisory is reported with its OSV link, summary and severity where OSV states
them. OSV not answering is reported as "not checked", never as "no
vulnerabilities".
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .evidence import Claim, Evidence

QUERYBATCH = "https://api.osv.dev/v1/querybatch"
QUERY = "https://api.osv.dev/v1/query"  # one package: full advisory records
VULN = "https://api.osv.dev/v1/vulns/"
LINK = "https://osv.dev/vulnerability/"
BATCH = 1000  # OSV's limit per querybatch call
MAX_QUERIES = 5000  # dependencies per teardown (5 batches); the rest are reported as not checked
MAX_DETAIL_PACKAGES = 200  # affected packages whose full advisories are fetched (one call each)

OSV_ECOSYSTEM = {"npm": "npm", "pypi": "PyPI", "crates": "crates.io", "go": "Go", "maven": "Maven", "nuget": "NuGet",
	"rubygems": "RubyGems", "packagist": "Packagist", "pub": "Pub", "hex": "Hex"}


@dataclass
class Advisory:
	id: str
	summary: str | None = None
	severity: str | None = None
	aliases: list[str] = field(default_factory=list)

	@property
	def link(self) -> str:
		return LINK + self.id


@dataclass
class Finding:
	name: str
	version: str
	ecosystem: str
	advisories: list[Advisory]
	scope: str = "runtime"
	via: str | None = None  # the lockfile a transitive package came from


@dataclass
class OsvResult:
	checked: int = 0
	unchecked: int = 0  # no exact version, or an ecosystem OSV does not index
	skipped_cap: int = 0
	failed: bool = False
	findings: list[Finding] = field(default_factory=list)


def _osv_package(dep) -> dict | None:
	if dep.ecosystem == "swift" and dep.url and "github.com/" in dep.url:
		return {"name": "github.com/" + dep.url.split("github.com/", 1)[1].removesuffix("/").removesuffix(".git"), "ecosystem": "SwiftURL"}
	eco = OSV_ECOSYSTEM.get(dep.ecosystem)
	return {"name": dep.name, "ecosystem": eco} if eco else None


# CVSS v3.0/v3.1 base score, from the specification's formula (first.org/cvss/v3.1/specification-document).
_CVSS3 = {"AV": {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2}, "AC": {"L": 0.77, "H": 0.44}, "UI": {"N": 0.85, "R": 0.62},
	"C": {"H": 0.56, "L": 0.22, "N": 0.0}, "I": {"H": 0.56, "L": 0.22, "N": 0.0}, "A": {"H": 0.56, "L": 0.22, "N": 0.0}}
_PR = {"U": {"N": 0.85, "L": 0.62, "H": 0.27}, "C": {"N": 0.85, "L": 0.68, "H": 0.5}}


def _roundup(x: float) -> float:
	n = round(x * 100000)
	return n / 100000 if n % 10000 == 0 else (n // 10000 + 1) / 10


def cvss3_base_score(vector: str) -> float | None:
	"""Base score of a CVSS:3.0 or CVSS:3.1 vector string, or None when it cannot be read."""
	parts = vector.strip().split("/")
	if not parts or parts[0] not in ("CVSS:3.0", "CVSS:3.1"):
		return None
	m = dict(p.split(":", 1) for p in parts[1:] if ":" in p)
	try:
		scope = m["S"]
		iss = 1 - (1 - _CVSS3["C"][m["C"]]) * (1 - _CVSS3["I"][m["I"]]) * (1 - _CVSS3["A"][m["A"]])
		impact = 6.42 * iss if scope == "U" else 7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15
		expl = 8.22 * _CVSS3["AV"][m["AV"]] * _CVSS3["AC"][m["AC"]] * _PR[scope][m["PR"]] * _CVSS3["UI"][m["UI"]]
	except KeyError:
		return None
	if impact <= 0:
		return 0.0
	return _roundup(min(impact + expl, 10)) if scope == "U" else _roundup(min(1.08 * (impact + expl), 10))


def cvss_rating(score: float) -> str:
	return "None" if score == 0 else "Low" if score < 4 else "Medium" if score < 7 else "High" if score < 9 else "Critical"


def _severity(detail: dict) -> str | None:
	ds = (detail.get("database_specific") or {}).get("severity")
	if isinstance(ds, str) and ds:
		return ds.capitalize()
	for sev in detail.get("severity") or []:
		vector = sev.get("score") or ""
		if sev.get("type") == "CVSS_V3":
			score = cvss3_base_score(vector)
			if score is not None:
				return f"{cvss_rating(score)} (CVSS 3 base score {score})"
		if sev.get("type", "").startswith("CVSS") and vector:
			return f"{sev['type']} vector {vector} (score not computed)"
	return None


def check(deps, registry, effective_version) -> OsvResult:
	"""Query OSV for every dependency with an exact version; registry.post_json / get_json do the HTTP."""
	result = OsvResult()
	queries, owners = [], []
	for dep in deps:
		if dep.scope in ("local", "vcs"):
			continue
		version = effective_version(dep)
		package = _osv_package(dep)
		if not version or package is None:
			result.unchecked += 1
			continue
		if len(queries) >= MAX_QUERIES:
			result.skipped_cap += 1
			continue
		queries.append({"package": package, "version": version})
		owners.append((dep, version))
	seen: dict[str, Advisory] = {}
	for start in range(0, len(queries), BATCH):
		chunk = queries[start:start + BATCH]
		answer = registry.post_json(QUERYBATCH, {"queries": chunk})
		results = answer.get("results") if isinstance(answer, dict) else None
		if not isinstance(results, list) or len(results) != len(chunk):
			result.failed = True
			result.unchecked += len(queries) - start
			break
		result.checked += len(chunk)
		for (dep, version), item in zip(owners[start:start + BATCH], results):
			ids = [v["id"] for v in (item or {}).get("vulns") or [] if isinstance(v, dict) and v.get("id")]
			if ids:
				advisories = [seen.setdefault(i, Advisory(i)) for i in ids]
				result.findings.append(Finding(dep.name, version, dep.ecosystem, advisories, dep.scope,
					dep.lock_source if dep.scope.startswith("transitive") else None))
	# Full records, one call per affected package (the batch call returns ids only).
	for f, (dep, version) in [(f, o) for f in result.findings for o in owners if o[0].name == f.name and o[1] == f.version][:MAX_DETAIL_PACKAGES]:
		answer = registry.post_json(QUERY, {"package": _osv_package(dep), "version": version})
		for detail in (answer or {}).get("vulns") or [] if isinstance(answer, dict) else []:
			adv = seen.get(detail.get("id"))
			if adv is not None and adv.summary is None:
				adv.summary = (detail.get("summary") or "").strip()[:200] or None
				adv.severity = _severity(detail)
				adv.aliases = [a for a in detail.get("aliases") or [] if isinstance(a, str)][:5]
	return result


def claims(result: OsvResult, repo_url: str) -> list[Claim]:
	out: list[Claim] = []
	for f in result.findings:
		parts = []
		for a in f.advisories[:6]:
			label = a.id + (f" ({a.severity})" if a.severity else "")
			parts.append(f"{label}: {a.summary}" if a.summary else label)
		more = f"; and {len(f.advisories) - 6} more" if len(f.advisories) > 6 else ""
		where = f" (transitive, resolved in {f.via})" if f.via else ""
		out.append(Claim("weakness", f"Known vulnerabilities in {f.name} {f.version} ({f.ecosystem}{where}): " + "; ".join(parts) + more + ".",
			[Evidence(a.link) for a in f.advisories[:5]], signal="deps_vulnerable"))
	if result.failed:
		out.append(Claim("fact", f"OSV.dev could not be reached, so {result.unchecked} dependencies were not checked for known vulnerabilities.", [Evidence(repo_url)]))
	elif result.checked:
		text = f"{result.checked} dependencies with an exact or locked version were checked against OSV.dev"
		text += f"; {len(result.findings)} have known vulnerabilities" if result.findings else "; none has a known vulnerability listed there"
		if result.unchecked:
			text += f". {result.unchecked} were not checked: no exact version (OSV answers per version), or an ecosystem OSV does not index"
		if result.skipped_cap:
			text += f". {result.skipped_cap} more were not checked: over the {MAX_QUERIES}-query cap"
		out.append(Claim("fact", text + ".", [Evidence("https://osv.dev/")]))
	elif result.unchecked:
		out.append(Claim("fact", f"No dependency had an exact or locked version, so none could be checked against OSV.dev ({result.unchecked} skipped).", [Evidence(repo_url)]))
	return out
