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
VULN = "https://api.osv.dev/v1/vulns/"
LINK = "https://osv.dev/vulnerability/"
BATCH = 100
MAX_QUERIES = 300  # dependencies per teardown; the rest are reported as not checked
MAX_DETAILS = 30  # advisories whose summary and severity are fetched one by one

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


def _severity(detail: dict) -> str | None:
	ds = (detail.get("database_specific") or {}).get("severity")
	if isinstance(ds, str) and ds:
		return ds.capitalize()
	for sev in detail.get("severity") or []:
		if sev.get("type", "").startswith("CVSS") and sev.get("score"):
			return f"{sev['type']} {sev['score']}"
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
				result.findings.append(Finding(dep.name, version, dep.ecosystem, advisories))
	for adv in list(seen.values())[:MAX_DETAILS]:
		detail = registry.get_json(VULN + adv.id)
		if isinstance(detail, dict):
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
		out.append(Claim("weakness", f"Known vulnerabilities in {f.name} {f.version} ({f.ecosystem}): " + "; ".join(parts) + more + ".",
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
