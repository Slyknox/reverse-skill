"""Dependency teardown: manifests -> dependency list -> license per dependency.

Manifests are read through the GitHub contents API. Licenses come from the
package registry (npm, PyPI, crates.io, RubyGems) or, for Go, Maven and NuGet,
from deps.dev, each with the URL that said so. deps.dev answers per version: a
declared version that names one release is looked up as is; a range or an
unresolved property falls back to the package's default version, and the
dependency says so. Lookups are capped; anything past the cap is counted and
listed as not checked.
"""

from __future__ import annotations

import base64
import posixpath
import json
import re
import tomllib
import xml.etree.ElementTree as ET
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from .evidence import Claim, Evidence, Ledger
from .github import NotFound
from .maven import Resolver, parse_pom, resolve_dependencies, wanted_artifacts
from .tree import Tree
from .nuget import pick as nuget_pick
from .versions import is_range, pick

MANIFESTS = {"package.json": "npm", "pyproject.toml": "pypi", "Cargo.toml": "crates", "go.mod": "go",
	"pom.xml": "maven", "Gemfile": "rubygems"}
# Ecosystems whose licenses come from deps.dev, which needs a version (system name on deps.dev).
DEPS_DEV = {"go": "go", "maven": "maven", "nuget": "nuget"}
REQUIREMENTS = re.compile(r"^requirements[\w.-]*\.txt$")
MAX_MANIFESTS = 10

PERMISSIVE = {"MIT", "MIT-0", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "BSD", "ISC", "0BSD", "Unlicense",
	"Zlib", "Python-2.0", "PSF-2.0", "CC0-1.0", "BSL-1.0", "Unicode-3.0", "Unicode-DFS-2016", "BlueOak-1.0.0",
	"MIT-CMU", "HPND", "X11", "NCSA", "PostgreSQL", "CC-BY-4.0", "Artistic-2.0", "BSD-3-Clause-Clear", "WTFPL"}
WEAK_COPYLEFT = {"LGPL-2.1", "LGPL-3.0", "MPL-2.0", "EPL-2.0", "EPL-1.0", "CDDL-1.0"}
STRONG_COPYLEFT = {"GPL-2.0", "GPL-3.0", "AGPL-3.0", "SSPL-1.0", "EUPL-1.2"}
CLASSIFIERS = {
	"MIT License": "MIT", "Apache Software License": "Apache-2.0", "BSD License": "BSD", "ISC License (ISCL)": "ISC",
	"Mozilla Public License 2.0 (MPL 2.0)": "MPL-2.0", "GNU General Public License v3 (GPLv3)": "GPL-3.0",
	"GNU General Public License v2 (GPLv2)": "GPL-2.0", "GNU Affero General Public License v3": "AGPL-3.0",
	"GNU Lesser General Public License v3 (LGPLv3)": "LGPL-3.0", "GNU Lesser General Public License v2 or later (LGPLv2+)": "LGPL-2.1",
	"Python Software Foundation License": "PSF-2.0", "The Unlicense (Unlicense)": "Unlicense",
}
_TOKEN = re.compile(r"[A-Za-z0-9.+-]+")


# Free-text spellings registries commonly return, mapped to SPDX ids. Matched on the whole string only.
ALIASES = {
	"apache 2.0": "Apache-2.0", "apache-2": "Apache-2.0", "apache 2": "Apache-2.0", "apache license 2.0": "Apache-2.0",
	"apache license, version 2.0": "Apache-2.0", "apache software license": "Apache-2.0", "apache": "Apache-2.0",
	"mit license": "MIT", "the mit license": "MIT", "bsd license": "BSD", "new bsd": "BSD-3-Clause", "new bsd license": "BSD-3-Clause",
	"bsd-3": "BSD-3-Clause", "3-clause bsd": "BSD-3-Clause", "2-clause bsd": "BSD-2-Clause", "isc license": "ISC",
	"mpl 2.0": "MPL-2.0", "mozilla public license 2.0": "MPL-2.0", "gplv3": "GPL-3.0", "gplv2": "GPL-2.0", "agplv3": "AGPL-3.0",
	"lgplv3": "LGPL-3.0", "lgplv2.1": "LGPL-2.1", "psf": "PSF-2.0", "python software foundation license": "PSF-2.0",
}


def normalize(expr: str) -> str:
	return ALIASES.get(expr.strip().lower(), expr.strip())


def classify(expr: str | None) -> str:
	"""permissive | weak_copyleft | copyleft | unknown, from an SPDX-ish expression."""
	if not expr:
		return "unknown"
	expr = normalize(expr)
	tokens = [t.removesuffix("-only").removesuffix("-or-later").removesuffix("+") for t in _TOKEN.findall(expr)
		if t.upper() not in {"OR", "AND", "WITH"}]
	known = [t for t in tokens if t in PERMISSIVE | WEAK_COPYLEFT | STRONG_COPYLEFT]
	if not known:
		return "unknown"
	if " OR " in f" {expr.upper()} " and any(t in PERMISSIVE for t in known):
		return "permissive"
	if any(t in STRONG_COPYLEFT for t in known):
		return "copyleft"
	if any(t in WEAK_COPYLEFT for t in known):
		return "weak_copyleft"
	return "permissive"


@dataclass
class Dependency:
	name: str
	spec: str
	ecosystem: str
	scope: str  # runtime | dev | optional | indirect | local (the project's own code, never looked up)
	manifest: str
	license: str | None = None
	license_class: str = "unknown"
	license_source: str | None = None
	looked_up: bool = False
	on_registry: bool = True
	license_note: str | None = None
	version_from: str | None = None  # where a version that is not written in the manifest came from
	manifest_url: str | None = None


@dataclass
class DepsTeardown:
	repo: str
	manifests: list[str] = field(default_factory=list)
	dependencies: list[Dependency] = field(default_factory=list)
	not_checked: int = 0
	ledger: Ledger = field(default_factory=Ledger)


# ---------- manifest parsing (pure) ----------

_REQ_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?\s*(.*)$")


def _pep508(line: str) -> tuple[str, str] | None:
	line = line.split("#", 1)[0].split(";", 1)[0].strip()
	if not line or line.startswith(("-", "git+", "http://", "https://", ".")):
		return None
	m = _REQ_NAME.match(line)
	return (m.group(1), m.group(3).strip()) if m else None


def parse_manifest(path: str, text: str, resolver=None) -> list[Dependency]:
	name = path.rsplit("/", 1)[-1]
	out: list[Dependency] = []

	def add(n, spec, eco, scope):
		out.append(Dependency(n, str(spec), eco, scope, path))

	if name == "package.json":
		data = json.loads(text)
		for key, scope in (("dependencies", "runtime"), ("optionalDependencies", "optional"), ("devDependencies", "dev")):
			for n, spec in (data.get(key) or {}).items():
				local = isinstance(spec, str) and spec.startswith(("file:", "link:", "workspace:", "portal:"))
				add(n, spec, "npm", "local" if local else scope)
	elif name == "pyproject.toml":
		data = tomllib.loads(text)
		project = data.get("project", {})
		for line in project.get("dependencies", []):
			if (p := _pep508(line)):
				add(*p, "pypi", "runtime")
		for lines in project.get("optional-dependencies", {}).values():
			for line in lines:
				if (p := _pep508(line)):
					add(*p, "pypi", "optional")
		for lines in data.get("dependency-groups", {}).values():
			for line in lines:
				if isinstance(line, str) and (p := _pep508(line)):
					add(*p, "pypi", "dev")
		poetry = data.get("tool", {}).get("poetry", {})
		for n, spec in poetry.get("dependencies", {}).items():
			if n.lower() != "python":
				add(n, spec if isinstance(spec, str) else json.dumps(spec), "pypi", "runtime")
	elif REQUIREMENTS.match(name):
		scope = "dev" if re.search(r"dev|test|lint|doc", name) else "runtime"
		for line in text.splitlines():
			if (p := _pep508(line)):
				add(*p, "pypi", scope)
	elif name == "Cargo.toml":
		data = tomllib.loads(text)
		for key, scope in (("dependencies", "runtime"), ("dev-dependencies", "dev"), ("build-dependencies", "dev")):
			for n, spec in (data.get(key) or {}).items():
				if isinstance(spec, dict) and "path" in spec:
					add(n, f"path = {spec['path']}", "crates", "local")
				else:
					add(n, spec if isinstance(spec, str) else spec.get("version", "*") if isinstance(spec, dict) else "*", "crates", scope)
	elif name == "go.mod":
		in_block = False
		for raw in text.splitlines():
			line = raw.strip()
			if line.startswith("require ("):
				in_block = True
				continue
			if in_block and line == ")":
				in_block = False
				continue
			if line.startswith("require ") or in_block:
				body = line.removeprefix("require ").split("//")[0].split()
				if len(body) >= 2:
					add(body[0], body[1], "go", "indirect" if "// indirect" in line else "runtime")
	elif name == "pom.xml":
		out.extend(_parse_pom(path, text, resolver))
	elif name.endswith(".csproj"):
		root = ET.fromstring(text)
		for ref in root.iter():
			if _local(ref.tag) == "PackageReference" and ref.get("Include"):
				version = ref.get("Version") or next((c.text for c in ref if _local(c.tag) == "Version"), None)
				add(ref.get("Include"), (version or "").strip() or "*", "nuget", "runtime")
	elif name == "Gemfile":
		groups: list[bool] = []  # one entry per open `group ... do` block: True if it is a dev/test group
		for raw in text.splitlines():
			line = raw.split("#", 1)[0].strip()
			if re.match(r"^group\b.*\bdo$", line):
				groups.append(bool(re.search(r":(development|test)\b", line)))
				continue
			if line == "end" and groups:
				groups.pop()
				continue
			m = re.match(r"""^gem\s+['"]([^'"]+)['"](?:\s*,\s*['"]([^'"]+)['"])?""", line)
			if m:
				dev = any(groups) or bool(re.search(r"group:\s*\[?:(development|test)\b", line))
				local = bool(re.search(r"\bpath:", line))
				add(m.group(1), m.group(2) or "*", "rubygems", "local" if local else "dev" if dev else "runtime")
	return out


def _local(tag: str) -> str:
	return tag.rsplit("}", 1)[-1]


def _parse_pom(path: str, text: str, resolver=None) -> list[Dependency]:
	"""Without a resolver, only the file's own properties and management are used (offline, no parent)."""
	if resolver is None:
		resolver = Resolver(lambda _p: None, lambda _u: None, max_fetches=0)
	pom = parse_pom(text, resolver.ctx, posixpath.dirname(path), resolver.file_exists)
	res = resolver.resolve(pom, path, path, wanted_artifacts(pom))
	if pom.applied_profiles or pom.skipped_profiles:
		resolver.notes.append(f"{path} with {resolver.ctx.describe()}: applied {', '.join(pom.applied_profiles) or 'none'}; "
			f"not applied {', '.join(pom.skipped_profiles) or 'none'}")
	out = []
	for ga, version, scope, origin in resolve_dependencies(pom, res):
		dep = Dependency(ga, version, "maven", scope, path)
		dep.version_from = origin
		out.append(dep)
	return out


def _is_manifest(name: str) -> bool:
	return name in MANIFESTS or bool(REQUIREMENTS.match(name)) or name.endswith(".csproj")


def find_manifests(tree: Tree | None, top_level: set[str]) -> list[str]:
	paths = tree.files if tree is not None else sorted(top_level)
	found = [p for p in paths if _is_manifest(p.rsplit("/", 1)[-1])]
	return sorted(found, key=lambda p: (p.count("/"), p))[:MAX_MANIFESTS]


# ---------- registry lookups ----------

class Registry:
	def __init__(self, timeout: float = 15.0):
		self.timeout = timeout

	def get_json(self, url: str):
		req = urllib.request.Request(url, headers={"User-Agent": "rea-teardown/0.1 (license lookup)", "Accept": "application/json"})
		try:
			with urllib.request.urlopen(req, timeout=self.timeout) as resp:
				return json.load(resp)
		except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
			return None


	def get_text(self, url: str, max_bytes: int = 2_000_000) -> str | None:
		"""Plain text (Maven POMs). Retries 429 and 503 up to three times with growing waits, honouring Retry-After up to 10 s."""
		import time

		for attempt in range(4):
			req = urllib.request.Request(url, headers={"User-Agent": "rea-teardown/0.4 (pom resolution)"})
			try:
				with urllib.request.urlopen(req, timeout=self.timeout) as resp:
					body = resp.read(max_bytes + 1)
					return None if len(body) > max_bytes else body.decode("utf-8", "replace")
			except urllib.error.HTTPError as err:
				if err.code in (429, 503) and attempt < 3:
					retry_after = err.headers.get("Retry-After", "")
					wait = float(retry_after) if retry_after.isdigit() else 2 ** attempt
					time.sleep(min(wait, 10))
					continue
				return None
			except (urllib.error.URLError, TimeoutError):
				return None
		return None


class FixtureRegistry:
	def __init__(self, responses: dict):
		self.responses = responses

	def get_json(self, url: str):
		return self.responses.get(url)

	def get_text(self, url: str):
		value = self.responses.get(url)
		return value if isinstance(value, str) else None


_EXACT = re.compile(r"^v?\d+(\.\d+)*([-+.][0-9A-Za-z.+-]+)?$")


def exact_version(dep: Dependency) -> str | None:
	"""The declared version if it names one release; None for ranges, wildcards and unresolved properties."""
	v = dep.spec.strip().strip("[]")
	return v if _EXACT.match(v) and "," not in v else None


def deps_dev_url(dep: Dependency, version: str | None = None) -> str:
	system = DEPS_DEV[dep.ecosystem]
	base = f"https://api.deps.dev/v3/systems/{system}/packages/{urllib.parse.quote(dep.name, safe='')}"
	return f"{base}/versions/{urllib.parse.quote(version, safe='')}" if version else base


def resolve_license(dep: Dependency, registry, budget: int) -> tuple[str | None, str | None, bool, str | None, int]:
	"""(license, source url, found on registry, note, lookups used); budget is how many lookups may be spent."""
	if dep.ecosystem in DEPS_DEV:
		version = exact_version(dep)
		if version:
			url = deps_dev_url(dep, version)
			data = registry.get_json(url)
			lic = ", ".join(data.get("licenses") or []) or None if isinstance(data, dict) else None
			return lic, url, data is not None, None, 1
		if budget < 2:
			return None, None, True, "lookup cap reached", 0
		pkg_url = deps_dev_url(dep)
		pkg = registry.get_json(pkg_url)
		published = [v["versionKey"]["version"] for v in (pkg or {}).get("versions", [])]
		chosen, note = None, None
		nuget_float = dep.ecosystem == "nuget" and "*" in dep.spec
		if (dep.ecosystem in ("maven", "nuget") and is_range(dep.spec)) or nuget_float:
			try:
				chosen = nuget_pick(dep.spec, published) if dep.ecosystem == "nuget" else pick(dep.ecosystem, dep.spec, published)
			except ValueError:
				chosen = None
			else:
				if chosen is None:
					return None, pkg_url, pkg is not None, f"no published version is inside the declared range {dep.spec!r}", 1
				rule = ("highest, as Maven picks" if dep.ecosystem == "maven"
					else "highest floating match, as NuGet picks" if nuget_float else "lowest, as NuGet picks")
				note = f"declared range {dep.spec!r}; license of {chosen}, the {rule}"
		if chosen is None:
			chosen = next((v["versionKey"]["version"] for v in (pkg or {}).get("versions", []) if v.get("isDefault")), None)
			note = f"declared version {dep.spec!r} is not one release; license of the default version {chosen}"
		if not chosen:
			return None, pkg_url, pkg is not None, None, 1
		url = deps_dev_url(dep, chosen)
		data = registry.get_json(url)
		lic = ", ".join(data.get("licenses") or []) or None if isinstance(data, dict) else None
		return lic, url, data is not None, note, 2
	if dep.ecosystem == "rubygems":
		url = f"https://rubygems.org/api/v1/gems/{urllib.parse.quote(dep.name, safe='')}.json"
		data = registry.get_json(url)
		lic = ", ".join(data.get("licenses") or []) or None if isinstance(data, dict) else None
		note = (f"declared version {dep.spec!r} is not one release; license of the latest release {data.get('version')}"
			if isinstance(data, dict) and not exact_version(dep) else None)
		return lic, url, data is not None, note, 1
	url = registry_url(dep)
	if url is None:
		return None, None, True, "no license API for this ecosystem", 0
	data = registry.get_json(url)
	return license_from(dep, data), url, data is not None, None, 1


def registry_url(dep: Dependency) -> str | None:
	q = urllib.parse.quote(dep.name, safe="@")
	if dep.ecosystem == "npm":
		return f"https://registry.npmjs.org/{q}/latest"
	if dep.ecosystem == "pypi":
		return f"https://pypi.org/pypi/{q}/json"
	if dep.ecosystem == "crates":
		return f"https://crates.io/api/v1/crates/{q}"
	return None


def license_from(dep: Dependency, data) -> str | None:
	if not isinstance(data, dict):
		return None
	if dep.ecosystem == "npm":
		lic = data.get("license")
		return lic.get("type") if isinstance(lic, dict) else lic
	if dep.ecosystem == "pypi":
		info = data.get("info") or {}
		if info.get("license_expression"):
			return info["license_expression"]
		lic = (info.get("license") or "").strip()
		if lic and "\n" not in lic and len(lic) <= 40:
			return lic
		for c in info.get("classifiers", []):
			if c.startswith("License ::"):
				label = c.rsplit("::", 1)[-1].strip()
				if label in CLASSIFIERS:
					return CLASSIFIERS[label]
		return None
	if dep.ecosystem == "crates":
		versions = data.get("versions") or []
		return versions[0].get("license") if versions else None
	return None


# ---------- teardown ----------

def teardown_deps(client, repo: str, tree: Tree | None, top_level: set[str], registry,
		max_lookups: int = 40, include_dev: bool = False, max_pom_fetches: int = 25, build=None) -> DepsTeardown:
	base = f"/repos/{repo}"
	t = DepsTeardown(repo=repo)
	files = set(tree.files) if tree is not None else set(top_level)

	def load_repo(path: str) -> str | None:
		if path not in files:
			return None
		try:
			r = client.get(f"{base}/contents/{urllib.parse.quote(path)}")
		except NotFound:
			return None
		return base64.b64decode(r.data.get("content", "")).decode("utf-8", "replace")

	dirs = {posixpath.dirname(f) for f in files}
	expanded = set()
	for d in dirs:
		while d and d not in expanded:
			expanded.add(d)
			d = posixpath.dirname(d)

	def file_exists(path: str) -> bool | None:
		if tree is None:
			return None  # only the top level is known: absence proves nothing
		return path in files or path in expanded

	resolver = Resolver(load_repo, registry.get_text, max_fetches=max_pom_fetches, ctx=build, file_exists=file_exists)
	for path in find_manifests(tree, top_level):
		try:
			resp = client.get(f"{base}/contents/{urllib.parse.quote(path)}")
			text = base64.b64decode(resp.data.get("content", "")).decode("utf-8", "replace")
			deps = parse_manifest(path, text, resolver)
		except NotFound:
			continue
		except (ValueError, tomllib.TOMLDecodeError, ET.ParseError) as err:
			t.ledger.add(Claim("fact", f"Could not parse {path}: {err}.", [Evidence(resp.url)]))
			continue
		t.manifests.append(path)
		for dep in deps:
			dep.manifest_url = resp.url
		t.dependencies.extend(deps)

	seen: dict[tuple, tuple] = {}
	lookups = 0
	skipped: set[tuple] = set()
	for dep in t.dependencies:
		if dep.scope == "local" or (dep.scope not in ("runtime", "optional") and not include_dev):
			continue
		key = (dep.ecosystem, dep.name.lower(), dep.spec if dep.ecosystem in DEPS_DEV else "")
		if key not in seen:
			if lookups >= max_lookups:
				skipped.add(key)
				continue
			lic, url, found, note, used = resolve_license(dep, registry, max_lookups - lookups)
			lookups += used
			if url is None:
				skipped.add(key)
				continue
			seen[key] = (lic, url, found, note)
		dep.license, dep.license_source, dep.on_registry, dep.license_note = seen[key]
		dep.license_class = classify(dep.license)
		dep.looked_up = True

	t.not_checked = len(skipped)
	problems = resolver.summary()
	if problems:
		t.ledger.add(Claim("fact", "Maven versions left unresolved: " + "; ".join(problems[:5])
			+ (f"; and {len(problems) - 5} more" if len(problems) > 5 else "") + ".",
			[Evidence(u) for u in dict.fromkeys(d.manifest_url for d in t.dependencies if d.ecosystem == "maven" and d.manifest_url)][:3]
			or [Evidence(f"https://api.github.com/repos/{repo}")]))
	if resolver.notes:
		t.ledger.add(Claim("fact", "Maven profiles: " + "; ".join(resolver.notes) + ".",
			[Evidence(u) for u in dict.fromkeys(d.manifest_url for d in t.dependencies if d.ecosystem == "maven" and d.manifest_url)][:3]
			or [Evidence(f"https://api.github.com/repos/{repo}")]))
	if resolver.fetched_urls:
		t.ledger.add(Claim("fact", f"Maven versions were resolved through {len(resolver.fetched_urls)} parent or BOM POM(s) read from Maven Central.",
			[Evidence(u) for u in resolver.fetched_urls[:5]]))
	_claims(t)
	return t


def _claims(t: DepsTeardown) -> None:
	if not t.manifests:
		t.ledger.add(Claim("fact", "No dependency manifests found (package.json, pyproject.toml, requirements*.txt, Cargo.toml, go.mod, pom.xml, *.csproj, Gemfile).",
			[Evidence(f"https://api.github.com/repos/{t.repo}")]))
		return
	by_scope: dict[str, int] = {}
	for d in t.dependencies:
		by_scope[d.scope] = by_scope.get(d.scope, 0) + 1
	summary = ", ".join(f"{n} {s}" for s, n in sorted(by_scope.items()))
	manifest_urls = list(dict.fromkeys(d.manifest_url for d in t.dependencies if d.manifest_url))
	t.ledger.add(Claim("fact", f"{len(t.dependencies)} declared dependencies ({summary}) in {len(t.manifests)} manifest(s): {', '.join(t.manifests)}.",
		[Evidence(u) for u in manifest_urls[:5]] or [Evidence(f"https://api.github.com/repos/{t.repo}")]))

	checked = [d for d in t.dependencies if d.looked_up]
	copyleft = [d for d in checked if d.license_class == "copyleft"]
	weak = [d for d in checked if d.license_class == "weak_copyleft"]
	unique = lambda deps: list({d.name.lower(): d for d in deps}.values())
	missing = unique([d for d in checked if not d.on_registry])
	unknown = unique([d for d in checked if d.on_registry and d.license_class == "unknown"])
	copyleft, weak = unique(copyleft), unique(weak)

	def ev(deps):
		return [Evidence(u) for u in dict.fromkeys(d.license_source for d in deps if d.license_source)][:5]

	if copyleft:
		t.ledger.add(Claim("weakness", "Copyleft dependencies: " + ", ".join(f"{d.name} ({d.license})" for d in copyleft[:8])
			+ ". Distributing a closed product that includes them can trigger source-release obligations.", ev(copyleft), signal="deps_copyleft"))
	if weak:
		t.ledger.add(Claim("fact", "Weak-copyleft dependencies (file- or library-level obligations): "
			+ ", ".join(f"{d.name} ({d.license})" for d in weak[:8]) + ".", ev(weak)))
	if unknown:
		t.ledger.add(Claim("weakness", "Dependencies whose registry entry states no license, or one rea does not recognise: "
			+ ", ".join(f"{d.name} ({d.license})" if d.license else d.name for d in unknown[:10]) + (f" and {len(unknown) - 10} more" if len(unknown) > 10 else "") + ".",
			ev(unknown), signal="deps_license_unknown"))
	if missing:
		t.ledger.add(Claim("fact", "Not found on the public registry (private, renamed, or the lookup failed): "
			+ ", ".join(d.name for d in missing[:10]) + (f" and {len(missing) - 10} more" if len(missing) > 10 else "") + ".",
			ev(missing), signal="deps_not_on_registry"))
	if checked and not copyleft and not unknown and not missing:
		t.ledger.add(Claim("strength", f"All {len(checked)} checked dependencies have recognised permissive or weak-copyleft licenses.",
			ev(checked), signal="deps_licenses_clear"))
	if t.not_checked:
		t.ledger.add(Claim("fact", f"{t.not_checked} dependencies were not looked up because the lookup cap was reached (raise it with --max-lookups).",
			[Evidence(u) for u in manifest_urls[:1]]))


def deps_for_repo(client, repo: str, registry, max_lookups: int = 40, include_dev: bool = False, max_pom_fetches: int = 25, build=None) -> DepsTeardown:
	"""Standalone entry point: reads the default branch and file listing, then the manifests."""
	from .tree import load_tree

	if repo.count("/") != 1:
		raise ValueError(f"expected owner/name, got {repo!r}")
	base = f"/repos/{repo}"
	meta = client.get(base).data
	tree = load_tree(client, base, meta.get("default_branch") or "HEAD")
	top = set()
	if tree is None:
		top = {e["name"] for e in client.get(f"{base}/contents").data if isinstance(e, dict)}
	return teardown_deps(client, meta.get("full_name", repo), tree, top, registry, max_lookups, include_dev, max_pom_fetches, build)
