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
from .lockfiles import LOCKFILE_ECOSYSTEM, parse_lockfile
from .manifests import composer_pick, parse_composer, parse_mix, parse_package_swift, parse_pubspec
from .maven import Resolver, parse_pom, resolve_dependencies, wanted_artifacts
from .tree import Tree
from .nuget import pick as nuget_pick
from .osv import OsvResult, check as osv_check, claims as osv_claims
from .versions import is_range, pick

MANIFESTS = {"package.json": "npm", "pyproject.toml": "pypi", "Cargo.toml": "crates", "go.mod": "go",
	"pom.xml": "maven", "Gemfile": "rubygems", "composer.json": "packagist", "Package.swift": "swift",
	"pubspec.yaml": "pub", "mix.exs": "hex"}
MAX_LOCKFILES = 10
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
	scope: str  # runtime | dev | optional | indirect | local (the project's own code) | vcs (from a git repository); local and vcs are never looked up
	manifest: str
	license: str | None = None
	license_class: str = "unknown"
	license_source: str | None = None
	looked_up: bool = False
	on_registry: bool = True
	license_note: str | None = None
	version_from: str | None = None  # where a version that is not written in the manifest came from
	locked: str | None = None  # exact version from a lockfile beside the manifest, when the spec is not exact
	lock_source: str | None = None  # the lockfile path
	url: str | None = None  # Swift: the package's git URL
	manifest_url: str | None = None


@dataclass
class DepsTeardown:
	repo: str
	manifests: list[str] = field(default_factory=list)
	dependencies: list[Dependency] = field(default_factory=list)
	lockfiles: list[str] = field(default_factory=list)
	lockfiles_unread: list[str] = field(default_factory=list)
	not_checked: int = 0
	osv: OsvResult | None = None
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
	elif name in ("composer.json", "Package.swift", "pubspec.yaml", "mix.exs"):
		parser = {"composer.json": parse_composer, "Package.swift": parse_package_swift, "pubspec.yaml": parse_pubspec, "mix.exs": parse_mix}[name]
		for n, spec, scope, extra in parser(text):
			dep = Dependency(n, spec, MANIFESTS[name], scope, path)
			dep.url = extra.get("url")
			out.append(dep)
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


	def post_json(self, url: str, body: dict):
		req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
			headers={"User-Agent": "rea-teardown/0.8 (vulnerability lookup)", "Content-Type": "application/json", "Accept": "application/json"})
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

	def post_json(self, url: str, body: dict):
		"""Answers come from the key "POST <url>": one response, or a callable taking the body."""
		value = self.responses.get("POST " + url)
		return value(body) if callable(value) else value


_EXACT = re.compile(r"^v?\d+(\.\d+)*([-+.][0-9A-Za-z.+-]+)?$")


def exact_version(dep: Dependency) -> str | None:
	"""The declared version if it names one release; None for ranges, wildcards and unresolved properties."""
	v = dep.spec.strip().strip("[]").lstrip("=")
	return v if _EXACT.match(v) and "," not in v else None


def effective_version(dep: Dependency) -> str | None:
	"""The version a lookup should be about: the locked one, else an exact declared one."""
	return dep.locked or exact_version(dep)


def deps_dev_url(dep: Dependency, version: str | None = None) -> str:
	system = DEPS_DEV[dep.ecosystem]
	base = f"https://api.deps.dev/v3/systems/{system}/packages/{urllib.parse.quote(dep.name, safe='')}"
	return f"{base}/versions/{urllib.parse.quote(version, safe='')}" if version else base


def resolve_license(dep: Dependency, registry, budget: int) -> tuple[str | None, str | None, bool, str | None, int]:
	"""(license, source url, found on registry, note, lookups used); budget is how many lookups may be spent."""
	if dep.ecosystem in DEPS_DEV:
		version = effective_version(dep)
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
	version = effective_version(dep)
	q = urllib.parse.quote(dep.name, safe="@/")
	if dep.ecosystem == "rubygems":
		if version:
			url = f"https://rubygems.org/api/v2/rubygems/{q}/versions/{urllib.parse.quote(version, safe='')}.json"
			data = registry.get_json(url)
			if isinstance(data, dict):
				return ", ".join(data.get("licenses") or []) or None, url, True, None, 1
			return None, url, False, f"version {version} not found on rubygems.org", 1
		url = f"https://rubygems.org/api/v1/gems/{q}.json"
		data = registry.get_json(url)
		lic = ", ".join(data.get("licenses") or []) or None if isinstance(data, dict) else None
		note = f"declared version {dep.spec!r} is not one release; license of the latest release {data.get('version')}" if isinstance(data, dict) else None
		return lic, url, data is not None, note, 1
	if dep.ecosystem == "packagist":
		url = f"https://repo.packagist.org/p2/{q}.json"
		data = registry.get_json(url)
		releases = _packagist_releases(data, dep.name)
		if not releases:
			return None, url, data is not None, None, 1
		hit = next((r for r in releases if version and r.get("version", "").lstrip("v") == version.lstrip("v")), None)
		note = None
		if hit is None:
			chosen = composer_pick(dep.spec, [r.get("version", "") for r in releases])
			if chosen is None:
				return None, url, True, f"no stable release satisfies the declared constraint {dep.spec!r}", 1
			hit = next(r for r in releases if r.get("version") == chosen)
			note = f"declared constraint {dep.spec!r}; license of {chosen}, the highest stable release it allows, as Composer picks"
		return ", ".join(hit.get("license") or []) or None, url, True, note, 1
	if dep.ecosystem == "pub":
		url = f"https://pub.dev/api/packages/{q}/score"
		data = registry.get_json(url)
		if not isinstance(data, dict):
			return None, url, False, None, 1
		tags = [t[len("license:"):] for t in data.get("tags", []) if t.startswith("license:") and t not in ("license:fsf-libre", "license:osi-approved", "license:unknown")]
		lic = ", ".join(_PUB_LICENSES.get(t, t.upper()) for t in tags) or None
		return lic, url, True, "pub.dev reports the license of the latest release only", 1
	if dep.ecosystem == "hex":
		url = f"https://hex.pm/api/packages/{q}"
		data = registry.get_json(url)
		if not isinstance(data, dict):
			return None, url, False, None, 1
		lic = ", ".join((data.get("meta") or {}).get("licenses") or []) or None
		return lic, url, True, "hex.pm reports the package's licenses, not per release", 1
	if dep.ecosystem == "swift":
		if "/" not in dep.name or (dep.url and "github.com" not in dep.url):
			return None, None, True, "no registry: a Swift package is a git repository, and only GitHub repositories are looked up", 0
		url = f"https://api.github.com/repos/{q}"
		data = registry.get_json(url)
		if not isinstance(data, dict):
			return None, url, False, None, 1
		spdx = (data.get("license") or {}).get("spdx_id")
		lic = None if spdx in (None, "NOASSERTION") else spdx
		return lic, url, True, "license GitHub detects on the repository's default branch, not the pinned release", 1
	url = registry_url(dep, version)
	if url is None:
		return None, None, True, "no license API for this ecosystem", 0
	data = registry.get_json(url)
	if version and data is None:
		# The registry has no such version (or the fixture has none): fall back to the package's latest, and say so.
		latest = registry_url(dep)
		data = registry.get_json(latest)
		note = f"version {version} not found on the registry; license of the latest release" if data is not None else None
		return license_from(dep, data), latest, data is not None, note, 2
	return license_from(dep, data), url, data is not None, None, 1


_PUB_LICENSES = {"mit": "MIT", "apache-2.0": "Apache-2.0", "bsd-3-clause": "BSD-3-Clause", "bsd-2-clause": "BSD-2-Clause",
	"isc": "ISC", "mpl-2.0": "MPL-2.0", "gpl-3.0": "GPL-3.0", "gpl-2.0": "GPL-2.0", "lgpl-3.0": "LGPL-3.0", "agpl-3.0": "AGPL-3.0",
	"unlicense": "Unlicense", "cc0-1.0": "CC0-1.0", "zlib": "Zlib", "0bsd": "0BSD"}


def _packagist_releases(data, name: str) -> list[dict]:
	"""Expand Packagist's minified p2 list: each entry carries only the fields that changed from the previous one."""
	if not isinstance(data, dict):
		return []
	raw = (data.get("packages") or {}).get(name) or []
	out, carried = [], {}
	for entry in raw:
		carried = {**carried, **{k: v for k, v in entry.items() if k != "__unset"}}
		for k in entry.get("__unset", []):
			carried.pop(k, None)
		out.append(dict(carried))
	return out


def registry_url(dep: Dependency, version: str | None = None) -> str | None:
	q = urllib.parse.quote(dep.name, safe="@")
	v = urllib.parse.quote(version, safe="") if version else None
	if dep.ecosystem == "npm":
		return f"https://registry.npmjs.org/{q}/{v or 'latest'}"
	if dep.ecosystem == "pypi":
		return f"https://pypi.org/pypi/{q}/{v}/json" if v else f"https://pypi.org/pypi/{q}/json"
	if dep.ecosystem == "crates":
		return f"https://crates.io/api/v1/crates/{q}/{v}" if v else f"https://crates.io/api/v1/crates/{q}"
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
		if isinstance(data.get("version"), dict):  # /crates/{name}/{version}
			return data["version"].get("license")
		versions = data.get("versions") or []
		return versions[0].get("license") if versions else None
	return None


# ---------- teardown ----------

def teardown_deps(client, repo: str, tree: Tree | None, top_level: set[str], registry,
		max_lookups: int = 40, include_dev: bool = False, max_pom_fetches: int = 25, build=None, osv: bool = True) -> DepsTeardown:
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

	_pin_from_lockfiles(t, tree, top_level, load_repo)

	seen: dict[tuple, tuple] = {}
	lookups = 0
	skipped: set[tuple] = set()
	for dep in t.dependencies:
		if dep.scope in ("local", "vcs") or (dep.scope not in ("runtime", "optional") and not include_dev):
			continue
		key = (dep.ecosystem, dep.name.lower(), effective_version(dep) or dep.spec)
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
	if osv:
		candidates = [d for d in t.dependencies if d.scope in ("runtime", "optional") or include_dev]
		t.osv = osv_check(candidates, registry, effective_version)
		for claim in osv_claims(t.osv, f"https://api.github.com/repos/{repo}"):
			t.ledger.add(claim)
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


def _pin_from_lockfiles(t: DepsTeardown, tree, top_level: set[str], load_repo) -> None:
	"""Pin each dependency with a non-exact spec to the version in a lockfile beside its manifest, else at the root."""
	paths = tree.files if tree is not None else sorted(top_level)
	locks = [p for p in paths if p.rsplit("/", 1)[-1] in LOCKFILE_ECOSYSTEM]
	locks = sorted(locks, key=lambda p: (p.count("/"), p))[:MAX_LOCKFILES]
	index: dict[tuple[str, str], tuple[str, dict[str, str]]] = {}  # (dir, ecosystem) -> (path, versions)
	for path in locks:
		text = load_repo(path)
		if text is None:
			continue
		versions = parse_lockfile(path.rsplit("/", 1)[-1], text)
		if not versions:
			t.lockfiles_unread.append(path)
			continue
		t.lockfiles.append(path)
		index.setdefault((posixpath.dirname(path), LOCKFILE_ECOSYSTEM[path.rsplit("/", 1)[-1]]), (path, versions))
	for dep in t.dependencies:
		if dep.scope in ("local", "vcs") or exact_version(dep):
			continue
		for where in (posixpath.dirname(dep.manifest), ""):
			hit = index.get((where, dep.ecosystem))
			if hit and dep.name.lower() in hit[1]:
				dep.locked, dep.lock_source = hit[1][dep.name.lower()], hit[0]
				dep.version_from = dep.version_from or f"locked in {hit[0]}"
				break


def _claims(t: DepsTeardown) -> None:
	if not t.manifests:
		t.ledger.add(Claim("fact", "No dependency manifests found (package.json, pyproject.toml, requirements*.txt, Cargo.toml, go.mod, pom.xml, *.csproj, Gemfile, composer.json, Package.swift, pubspec.yaml, mix.exs).",
			[Evidence(f"https://api.github.com/repos/{t.repo}")]))
		return
	by_scope: dict[str, int] = {}
	for d in t.dependencies:
		by_scope[d.scope] = by_scope.get(d.scope, 0) + 1
	summary = ", ".join(f"{n} {s}" for s, n in sorted(by_scope.items()))
	manifest_urls = list(dict.fromkeys(d.manifest_url for d in t.dependencies if d.manifest_url))
	t.ledger.add(Claim("fact", f"{len(t.dependencies)} declared dependencies ({summary}) in {len(t.manifests)} manifest(s): {', '.join(t.manifests)}.",
		[Evidence(u) for u in manifest_urls[:5]] or [Evidence(f"https://api.github.com/repos/{t.repo}")]))

	pinned = [d for d in t.dependencies if d.locked]
	if t.lockfiles:
		t.ledger.add(Claim("fact", f"{len(pinned)} of {len(t.dependencies)} dependencies were pinned to the version in a lockfile ({', '.join(t.lockfiles)}); "
			"their license and vulnerability lookups are about that version.", [Evidence(u) for u in manifest_urls[:3]]))
	if t.lockfiles_unread:
		t.ledger.add(Claim("fact", f"Lockfile(s) found but not read (unrecognised shape): {', '.join(t.lockfiles_unread)}.", [Evidence(u) for u in manifest_urls[:1]]))
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


def deps_for_repo(client, repo: str, registry, max_lookups: int = 40, include_dev: bool = False, max_pom_fetches: int = 25, build=None, osv: bool = True) -> DepsTeardown:
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
	return teardown_deps(client, meta.get("full_name", repo), tree, top, registry, max_lookups, include_dev, max_pom_fetches, build, osv)
