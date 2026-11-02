"""Lockfile readers: the exact version each dependency resolved to.

A manifest says what is allowed ("^3.0"); the lockfile says what was installed
("3.12.1"). When a lockfile sits beside a manifest (or at the repository
root), the locked version is what a license or vulnerability lookup should be
about, and the report says it came from the lockfile.

Each reader returns {name.lower(): version} for its ecosystem. Readers accept
the common shapes of each format and skip entries they cannot read; they never
guess a version. go.sum is not read: go.mod already pins exact versions, and
go.sum lists every version ever downloaded, not the one in use.
"""

from __future__ import annotations

import json
import re
import tomllib

LOCKFILE_ECOSYSTEM = {
	"package-lock.json": "npm", "npm-shrinkwrap.json": "npm", "pnpm-lock.yaml": "npm", "yarn.lock": "npm", "bun.lock": "npm",
	"uv.lock": "pypi", "poetry.lock": "pypi", "Pipfile.lock": "pypi", "pdm.lock": "pypi",
	"Cargo.lock": "crates", "Gemfile.lock": "rubygems", "composer.lock": "packagist", "pubspec.lock": "pub",
	"mix.lock": "hex", "Package.resolved": "swift", "packages.lock.json": "nuget",
}


def parse_lockfile(name: str, text: str) -> dict[str, str]:
	try:
		reader = _READERS[name]
	except KeyError:
		return {}
	try:
		return {k.lower(): v for k, v in reader(text).items() if k and v}
	except (ValueError, KeyError, TypeError, AttributeError, tomllib.TOMLDecodeError):
		return {}  # an unreadable lockfile pins nothing; the caller reports it as not read


def _package_lock(text: str) -> dict[str, str]:
	data = json.loads(text)
	out: dict[str, str] = {}
	for path, entry in (data.get("packages") or {}).items():  # lockfile v2/v3
		if path.startswith("node_modules/") and isinstance(entry, dict) and entry.get("version"):
			name = path.rsplit("node_modules/", 1)[-1]
			out.setdefault(name, entry["version"])  # the top-level copy comes first
	for name, entry in (data.get("dependencies") or {}).items():  # v1
		if isinstance(entry, dict) and entry.get("version"):
			out.setdefault(name, entry["version"])
	return out


def _pnpm_lock(text: str) -> dict[str, str]:
	out: dict[str, str] = {}
	in_packages = False
	for line in text.splitlines():
		if re.match(r"^(packages|snapshots):\s*$", line):
			in_packages = True
			continue
		if line and not line.startswith(" ") and in_packages:
			in_packages = False
		if in_packages:
			m = re.match(r"^  '?/?((?:@[^/\s']+/)?[^@/\s']+)@([^\s'(:]+)", line)
			if m:
				out.setdefault(m.group(1), m.group(2))
	return out


def _yarn_lock(text: str) -> dict[str, str]:
	out: dict[str, str] = {}
	current: list[str] = []
	for line in text.splitlines():
		if line and not line.startswith((" ", "#")):
			current = []
			for key in line.rstrip(":").split(","):
				key = key.strip().strip('"')
				m = re.match(r"^((?:@[^/@]+/)?[^@]+)@", key)
				if m:
					current.append(m.group(1))
		else:
			m = re.match(r'^\s+version:?\s+"?([^"\s]+)"?\s*$', line)
			if m:
				for name in current:
					out.setdefault(name, m.group(1))
	return out


def _bun_lock(text: str) -> dict[str, str]:
	# bun.lock is JSONC with trailing commas; strip comments and trailing commas before reading.
	cleaned = re.sub(r"//[^\n]*", "", text)
	cleaned = re.sub(r",(\s*[}\]])", r"\1", cleaned)
	data = json.loads(cleaned)
	out: dict[str, str] = {}
	for name, entry in (data.get("packages") or {}).items():
		if isinstance(entry, list) and entry and isinstance(entry[0], str):
			m = re.match(r"^(.+)@([^@]+)$", entry[0])
			if m:
				out[m.group(1)] = m.group(2)
	return out


def _toml_packages(text: str) -> dict[str, str]:
	data = tomllib.loads(text)
	return {p["name"]: p["version"] for p in data.get("package", []) if p.get("name") and p.get("version")}


def _pipfile_lock(text: str) -> dict[str, str]:
	data = json.loads(text)
	out: dict[str, str] = {}
	for section in ("default", "develop"):
		for name, entry in (data.get(section) or {}).items():
			v = entry.get("version", "") if isinstance(entry, dict) else ""
			if v.startswith("=="):
				out.setdefault(name, v[2:])
	return out


def _gemfile_lock(text: str) -> dict[str, str]:
	out: dict[str, str] = {}
	in_specs = False
	for line in text.splitlines():
		if line.strip() == "specs:":
			in_specs = True
			continue
		if in_specs and line and not line.startswith(" "):
			in_specs = False
		if in_specs:
			m = re.match(r"^    (\S+) \(([^)\s]+)\)", line)  # four spaces: a resolved gem; six: its dependencies
			if m:
				out.setdefault(m.group(1), m.group(2))
	return out


def _composer_lock(text: str) -> dict[str, str]:
	data = json.loads(text)
	out: dict[str, str] = {}
	for section in ("packages", "packages-dev"):
		for p in data.get(section) or []:
			if p.get("name") and p.get("version"):
				out.setdefault(p["name"], p["version"].lstrip("v"))
	return out


def _pubspec_lock(text: str) -> dict[str, str]:
	out: dict[str, str] = {}
	name = None
	in_packages = False
	for line in text.splitlines():
		if line.rstrip() == "packages:":
			in_packages = True
			continue
		if in_packages and line and not line.startswith(" "):
			in_packages = False
		if not in_packages:
			continue
		m = re.match(r"^  (\S+):\s*$", line)
		if m:
			name = m.group(1)
			continue
		m = re.match(r'^    version:\s*"?([^"\s]+)"?\s*$', line)
		if m and name:
			out.setdefault(name, m.group(1))
	return out


def _mix_lock(text: str) -> dict[str, str]:
	return {m.group(1): m.group(2) for m in re.finditer(r'"([a-z0-9_]+)":\s*\{:hex,\s*:[a-z0-9_]+,\s*"([^"]+)"', text)}


def _package_resolved(text: str) -> dict[str, str]:
	from .manifests import swift_identity

	data = json.loads(text)
	pins = data.get("pins") or (data.get("object") or {}).get("pins") or []
	out: dict[str, str] = {}
	for pin in pins:
		url = pin.get("location") or pin.get("repositoryURL") or ""
		version = (pin.get("state") or {}).get("version")
		identity = swift_identity(url) if url else pin.get("identity") or pin.get("package")
		if identity and version:
			out[identity] = version
	return out


def _packages_lock_json(text: str) -> dict[str, str]:
	data = json.loads(text)
	out: dict[str, str] = {}
	for framework in (data.get("dependencies") or {}).values():
		for name, entry in (framework or {}).items():
			if isinstance(entry, dict) and entry.get("resolved"):
				out.setdefault(name, entry["resolved"])
	return out


_READERS = {
	"package-lock.json": _package_lock, "npm-shrinkwrap.json": _package_lock, "pnpm-lock.yaml": _pnpm_lock,
	"yarn.lock": _yarn_lock, "bun.lock": _bun_lock,
	"uv.lock": _toml_packages, "poetry.lock": _toml_packages, "pdm.lock": _toml_packages, "Pipfile.lock": _pipfile_lock,
	"Cargo.lock": _toml_packages, "Gemfile.lock": _gemfile_lock, "composer.lock": _composer_lock,
	"pubspec.lock": _pubspec_lock, "mix.lock": _mix_lock, "Package.resolved": _package_resolved,
	"packages.lock.json": _packages_lock_json,
}
