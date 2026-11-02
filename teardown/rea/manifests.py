"""Manifest parsers for Composer (PHP), Swift Package Manager, pub (Dart) and Hex (Elixir).

Each returns (name, spec, scope, extra) tuples that deps.py turns into
Dependency rows. Scopes: runtime, dev, local (a path in the repository) and
vcs (fetched from a git repository rather than a registry, so no registry can
say its license).

pubspec.yaml and mix.exs have no standard-library parser, so both are read
with small purpose-built readers that handle the shapes those files use in
practice and ignore anything else. Nothing is guessed: a dependency the reader
cannot place is left out, and the caller reports how many were left out.
"""

from __future__ import annotations

import json
import re

Row = tuple[str, str, str, dict]  # name, spec, scope, extra


# ---------- Composer ----------

def parse_composer(text: str) -> list[Row]:
	data = json.loads(text)
	out: list[Row] = []
	for key, scope in (("require", "runtime"), ("require-dev", "dev")):
		for name, spec in (data.get(key) or {}).items():
			if name == "php" or name.startswith(("ext-", "lib-")) or name == "composer-plugin-api" or name == "composer-runtime-api":
				continue  # platform requirements, not packages
			if "/" not in name:
				continue
			out.append((name, str(spec), scope, {}))
	return out


# ---------- Swift Package Manager ----------

_SWIFT_PKG = re.compile(r"\.package\s*\((?P<args>.*?)\)\s*(?:,|\])", re.S)
_SWIFT_URL = re.compile(r"""url\s*:\s*"([^"]+)\"""")
_SWIFT_PATH = re.compile(r"""path\s*:\s*"([^"]+)\"""")
_SWIFT_REQ = [
	(re.compile(r"""exact\s*:\s*"([^"]+)\""""), lambda m: f"={m.group(1)}"),
	(re.compile(r"""from\s*:\s*"([^"]+)\""""), lambda m: f"from {m.group(1)}"),
	(re.compile(r"""\.upToNextMajor\s*\(\s*from\s*:\s*"([^"]+)\"\s*\)"""), lambda m: f"from {m.group(1)}"),
	(re.compile(r"""\.upToNextMinor\s*\(\s*from\s*:\s*"([^"]+)\"\s*\)"""), lambda m: f"from {m.group(1)} (minor)"),
	(re.compile(r"""\.exact\s*\(\s*"([^"]+)\"\s*\)"""), lambda m: f"={m.group(1)}"),
	(re.compile(r""""([^"]+)"\s*\.\.<\s*"([^"]+)\""""), lambda m: f"[{m.group(1)},{m.group(2)})"),
	(re.compile(r""""([^"]+)"\s*\.\.\.\s*"([^"]+)\""""), lambda m: f"[{m.group(1)},{m.group(2)}]"),
	(re.compile(r"""branch\s*:\s*"([^"]+)\""""), lambda m: f"branch {m.group(1)}"),
	(re.compile(r"""revision\s*:\s*"([^"]+)\""""), lambda m: f"revision {m.group(1)}"),
]


def swift_identity(url: str) -> str | None:
	"""owner/repo for a GitHub URL; the last path part otherwise."""
	m = re.match(r"^(?:https?://|git@)github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", url.strip())
	if m:
		return f"{m.group(1)}/{m.group(2)}"
	tail = url.rstrip("/").rsplit("/", 1)[-1]
	return tail[:-4] if tail.endswith(".git") else tail or None


def parse_package_swift(text: str) -> list[Row]:
	out: list[Row] = []
	for m in _SWIFT_PKG.finditer(text):
		args = m.group("args")
		path = _SWIFT_PATH.search(args)
		if path:
			out.append((path.group(1).rstrip("/").rsplit("/", 1)[-1] or path.group(1), f"path = {path.group(1)}", "local", {}))
			continue
		url = _SWIFT_URL.search(args)
		if not url:
			continue
		spec = next((fmt(r) for pat, fmt in _SWIFT_REQ if (r := pat.search(args))), "*")
		identity = swift_identity(url.group(1))
		if identity:
			out.append((identity, spec, "runtime", {"url": url.group(1)}))
	return out


# ---------- pub (Dart / Flutter) ----------

def _yaml_section(lines: list[str], header: str) -> list[tuple[str, str, list[str]]]:
	"""(key, inline value, nested lines) for each entry under a top-level `header:` map."""
	out: list[tuple[str, str, list[str]]] = []
	i = 0
	while i < len(lines):
		if lines[i].rstrip() == f"{header}:":
			i += 1
			while i < len(lines):
				line = lines[i]
				if line.strip() and not line.startswith((" ", "\t")):
					break  # next top-level key
				m = re.match(r"^  (\S[^:]*):\s*(.*)$", line.rstrip())
				if m and not line.startswith("   "):
					nested = []
					j = i + 1
					while j < len(lines) and (not lines[j].strip() or lines[j].startswith("   ")):
						nested.append(lines[j])
						j += 1
					out.append((m.group(1).strip(), m.group(2).strip().split(" #", 1)[0].strip().strip("'\""), nested))
					i = j
					continue
				i += 1
			break
		i += 1
	return out


def parse_pubspec(text: str) -> list[Row]:
	lines = text.splitlines()
	out: list[Row] = []
	for header, scope in (("dependencies", "runtime"), ("dev_dependencies", "dev")):
		for name, value, nested in _yaml_section(lines, header):
			if name == "flutter" and any("sdk:" in n for n in nested):
				continue
			body = "\n".join(nested)
			if re.search(r"^\s+sdk:", body, re.M):
				continue  # part of the Flutter SDK, not on pub.dev
			if re.search(r"^\s+path:", body, re.M):
				out.append((name, "path", "local", {}))
			elif re.search(r"^\s+git:", body, re.M):
				out.append((name, "git", "vcs", {}))
			elif re.search(r"^\s+hosted:", body, re.M):
				ver = re.search(r"^\s+version:\s*(.+)$", body, re.M)
				out.append((name, (ver.group(1).strip().strip("'\"") if ver else "*"), scope, {"hosted": True}))
			else:
				out.append((name, value or "*", scope, {}))
	return out


# ---------- Hex (Elixir mix.exs) ----------

_MIX_DEP = re.compile(r"\{\s*:(?P<name>[a-zA-Z_][\w]*)\s*(?P<rest>(?:,[^{}]*?)?)\}", re.S)


def parse_mix(text: str) -> list[Row]:
	m = re.search(r"defp?\s+deps\s*(?:\(\))?\s*(?:,\s*)?do(.*?)\n\s*end", text, re.S)
	if not m:
		return []
	out: list[Row] = []
	for dep in _MIX_DEP.finditer(m.group(1)):
		name, rest = dep.group("name"), dep.group("rest")
		spec_m = re.match(r'\s*,\s*"([^"]*)"', rest)
		spec = spec_m.group(1) if spec_m else "*"
		if re.search(r"\bpath:", rest):
			out.append((name, "path", "local", {}))
		elif re.search(r"\b(github|git):", rest):
			out.append((name, "git", "vcs", {}))
		else:
			only = re.search(r"\bonly:\s*(\[[^\]]*\]|:\w+)", rest)
			dev = bool(only) and all(env in ("dev", "test", "docs") for env in re.findall(r":(\w+)", only.group(1)))
			out.append((name, spec, "dev" if dev else "runtime", {}))
	return out


# ---------- Composer version constraints ----------

_COMPOSER_STABILITY = {"dev": 0, "alpha": 1, "a": 1, "beta": 2, "b": 2, "rc": 3, "c": 3, "": 4, "stable": 4, "patch": 5, "pl": 5, "p": 5}


def composer_key(version: str) -> tuple:
	"""Composer's order: up to four numeric parts (missing ones are 0), then stability, then its number."""
	v = version.strip().lstrip("vV")
	m = re.match(r"^(\d+(?:\.\d+){0,3})(?:[-._]?(dev|alpha|a|beta|b|rc|c|stable|patch|pl|p)[-._]?(\d*))?(?:\+.*)?$", v, re.I)
	if not m:
		return ((-1,), 0, 0)
	nums = [int(x) for x in m.group(1).split(".")]
	nums += [0] * (4 - len(nums))
	stability = _COMPOSER_STABILITY[(m.group(2) or "").lower()]
	return (tuple(nums), stability, int(m.group(3) or 0))


def composer_is_stable(version: str) -> bool:
	return composer_key(version)[1] == 4 and composer_key(version)[0][0] >= 0


def _composer_single(constraint: str, key: tuple) -> bool:
	c = constraint.strip()
	if c in ("*", "@stable", "@dev", ""):
		return True
	c = re.sub(r"@(stable|RC|beta|alpha|dev)$", "", c)
	m = re.match(r"^(\^|~|>=|<=|!=|<>|>|<|==|=)?\s*v?(\d+(?:\.(?:\d+|\*|x))*)$", c, re.I)
	if not m:
		return False
	op, raw = m.group(1) or "=", m.group(2)
	parts = raw.split(".")
	if parts[-1] in ("*", "x"):  # 1.2.* means >=1.2.0 <1.3.0
		base = [int(x) for x in parts[:-1]]
		low = base + [0] * (4 - len(base))
		high = base[:-1] + [base[-1] + 1] + [0] * (4 - len(base))
		return tuple(low) <= key[0] < tuple(high)
	nums = [int(x) for x in parts]
	full = tuple(nums + [0] * (4 - len(nums)))
	if op == "^":
		# ^1.2.3 -> >=1.2.3 <2.0.0; ^0.3 -> >=0.3.0 <0.4.0; ^0.0.3 -> >=0.0.3 <0.0.4
		if nums[0] > 0 or len(nums) == 1:
			high = (nums[0] + 1, 0, 0, 0)
		elif len(nums) == 2 or nums[1] > 0:
			high = (0, nums[1] + 1, 0, 0)
		else:
			high = (0, 0, nums[2] + 1, 0)
		return full <= key[0] < high
	if op == "~":  # ~1.2 -> >=1.2 <2.0; ~1.2.3 -> >=1.2.3 <1.3.0
		idx = max(len(nums) - 2, 0)
		high = list(nums[:idx]) + [nums[idx] + 1] + [0] * (4 - idx - 1)
		return full <= key[0] < tuple(high)
	cmp = (key[0] > full) - (key[0] < full)
	if cmp == 0 and key[1] != 4 and op in (">=", "<=", "=", "=="):
		cmp = -1  # 2.0.0-beta is below 2.0.0
	return {"=": cmp == 0, "==": cmp == 0, "!=": cmp != 0, "<>": cmp != 0, ">": cmp > 0, ">=": cmp >= 0, "<": cmp < 0, "<=": cmp <= 0}[op]


def composer_satisfies(spec: str, version: str) -> bool:
	"""Composer constraint syntax: `||` alternatives, each a space- or comma-separated AND; ^, ~, wildcards, comparison operators."""
	key = composer_key(version)
	if key[0][0] < 0:
		return False
	for alt in re.split(r"\s*\|\|?\s*", spec.strip()):
		if alt and all(_composer_single(c, key) for c in re.split(r"[\s,]+(?![-\d]*$)", alt.replace(" - ", "|-|")) if c and c != "|-|"):
			return True
	return False


def composer_pick(spec: str, versions: list[str]) -> str | None:
	"""Composer installs the highest stable version that satisfies the constraint (minimum-stability stable)."""
	stable = [v for v in versions if composer_is_stable(v) and composer_satisfies(spec, v)]
	return max(stable, key=composer_key) if stable else None
