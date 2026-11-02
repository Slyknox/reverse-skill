"""Maven version resolution: parent POMs, imported BOMs, and properties.

A Maven dependency often declares no version: the version comes from a parent
POM's dependencyManagement, or from a BOM imported there, and may be a
${property} defined anywhere in the chain. This follows Maven's order:

1. Build the inheritance chain (child, parent, grandparent...). A parent is read
   from the repository when relativePath points at it, else from Maven Central.
2. Merge properties from the root down, so the child's values win.
3. Merge explicit dependencyManagement entries (child wins), then interpolate
   them with the merged properties: a child overriding ${jackson.version}
   changes what the parent manages.
4. Resolve imported BOMs (versions interpolated with the merged properties),
   each in its own context, for whatever is still unmanaged.

BOMs are fetched only while a dependency is still unmanaged. Maven reads them in
declaration order; to stay inside the fetch cap, BOMs sharing two or more
groupId segments with an unmanaged dependency are read first, then at most
OTHER_BOM_BUDGET others per POM in Maven's order. If two BOMs manage the same
artifact at different versions (rare), this can pick a different one than
Maven; each version's note names the POM it came from, so it can be checked.

Fetches and depth are capped; anything past the cap stays unresolved and says
why, rather than being guessed.
"""

from __future__ import annotations

import posixpath
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

CENTRAL = "https://repo1.maven.org/maven2"
MAX_DEPTH = 8
OTHER_BOM_BUDGET = 5  # BOMs read per POM that share no groupId with what is still unmanaged
_PROP = re.compile(r"\$\{([^}]+)\}")


def _local(tag: str) -> str:
	return tag.rsplit("}", 1)[-1]


def central_url(group: str, artifact: str, version: str) -> str:
	return f"{CENTRAL}/{group.replace('.', '/')}/{artifact}/{version}/{artifact}-{version}.pom"


@dataclass
class Pom:
	group: str | None
	artifact: str | None
	version: str | None
	parent: tuple[str, str, str, str | None] | None  # group, artifact, version, relativePath ("" = none)
	properties: dict[str, str] = field(default_factory=dict)
	managed: dict[str, str] = field(default_factory=dict)  # "g:a" -> raw version
	imports: list[tuple[str, str, str]] = field(default_factory=list)  # raw g, a, version
	dependencies: list[dict] = field(default_factory=list)
	applied_profiles: list[str] = field(default_factory=list)  # activeByDefault profiles merged in
	skipped_profiles: list[str] = field(default_factory=list)  # profiles that need the build machine to decide


def _txt(el, name):
	child = next((c for c in el if _local(c.tag) == name), None)
	if child is None:
		return None
	return (child.text or "").strip()


def _read_section(el, pom: "Pom", override: bool) -> None:
	"""Read properties, dependencyManagement and dependencies from <project> or a <profile>.

	With override (a profile), its properties and managed versions replace the project's,
	as Maven's profile injection does; dependencies are added.
	"""
	fields = {_local(c.tag): c for c in el}
	if "properties" in fields:
		for p in fields["properties"]:
			if override or _local(p.tag) not in pom.properties:
				pom.properties[_local(p.tag)] = (p.text or "").strip()
	dm = fields.get("dependencyManagement")
	if dm is not None:
		for deps in dm:
			for dep in deps:
				g, a, v = _txt(dep, "groupId"), _txt(dep, "artifactId"), _txt(dep, "version")
				if not (g and a):
					continue
				if _txt(dep, "scope") == "import" and _txt(dep, "type") == "pom":
					pom.imports.append((g, a, v or ""))
				elif v and (override or f"{g}:{a}" not in pom.managed):
					pom.managed[f"{g}:{a}"] = v
	if "dependencies" in fields:
		for dep in fields["dependencies"]:
			entry = {_local(c.tag): (c.text or "").strip() for c in dep}
			if entry.get("groupId") and entry.get("artifactId"):
				pom.dependencies.append(entry)


def parse_pom(text: str) -> Pom:
	"""Parse a POM, applying profiles marked activeByDefault.

	Other profiles activate on the build machine (JDK, OS, a -D property, a file, -P),
	which a teardown cannot know, so they are listed in skipped_profiles, not applied.
	An activeByDefault profile is switched off when another profile activates; that is
	also unknowable here, so it is applied and named in applied_profiles.
	"""
	root = ET.fromstring(text)
	fields = {_local(c.tag): c for c in root}
	parent = None
	if "parent" in fields:
		p = fields["parent"]
		rel = _txt(p, "relativePath")
		parent = (_txt(p, "groupId") or "", _txt(p, "artifactId") or "", _txt(p, "version") or "", "../pom.xml" if rel is None else rel)
	pom = Pom(_txt(root, "groupId") or (parent[0] if parent else None), _txt(root, "artifactId"),
		_txt(root, "version") or (parent[2] if parent else None), parent)
	_read_section(root, pom, override=False)
	for profile in fields.get("profiles", []):
		name = _txt(profile, "id") or "(no id)"
		activation = next((c for c in profile if _local(c.tag) == "activation"), None)
		if activation is not None and (_txt(activation, "activeByDefault") or "").lower() == "true":
			_read_section(profile, pom, override=True)
			pom.applied_profiles.append(name)
		else:
			how = ", ".join(_local(c.tag) for c in activation) if activation is not None else "-P only"
			pom.skipped_profiles.append(f"{name} ({how})")
	return pom


def substitute(value: str, props: dict[str, str]) -> str:
	for _ in range(5):  # properties may refer to properties
		new = _PROP.sub(lambda m: props.get(m.group(1), m.group(0)), value)
		if new == value:
			break
		value = new
	return value


@dataclass
class Resolved:
	properties: dict[str, str]
	managed: dict[str, tuple[str, str]]  # "g:a" -> (version, where it was declared)
	problems: list[str]


def _affinity(import_group: str, wanted: set[str]) -> int:
	"""How many leading groupId segments the BOM shares with the closest unmanaged dependency."""
	best = 0
	ig = import_group.split(".")
	for ga in wanted:
		dg = ga.split(":", 1)[0].split(".")
		n = 0
		while n < min(len(ig), len(dg)) and ig[n] == dg[n]:
			n += 1
		best = max(best, n)
	return best


class Resolver:
	"""load_repo(path) -> text or None reads a repository file; fetch(url) -> text or None reads Maven Central."""

	def __init__(self, load_repo, fetch, max_fetches: int = 25):
		self.load_repo = load_repo
		self.fetch = fetch
		self.max_fetches = max_fetches
		self.fetches = 0
		self.fetched_urls: list[str] = []
		self.problems: list[str] = []
		self.notes: list[str] = []  # facts about what was applied, e.g. profiles
		self.capped = 0  # POMs not read because the fetch cap was reached
		self.skipped_boms = 0  # BOMs not tried: no shared groupId and past OTHER_BOM_BUDGET
		self._central_cache: dict[str, Pom | None] = {}

	def summary(self) -> list[str]:
		out = list(self.problems)
		if self.capped:
			out.append(f"{self.capped} POM(s) not read because the Maven Central fetch cap ({self.max_fetches}) was reached")
		if self.skipped_boms:
			out.append(f"{self.skipped_boms} imported BOM(s) not tried because their groupId matches no unmanaged dependency")
		return out

	def _problem(self, text: str) -> None:
		if text not in self.problems:
			self.problems.append(text)

	def _central(self, g: str, a: str, v: str) -> tuple[Pom | None, str]:
		url = central_url(g, a, v)
		if url in self._central_cache:
			return self._central_cache[url], url
		if self.fetches >= self.max_fetches:
			self.capped += 1
			return None, url
		self.fetches += 1
		text = self.fetch(url)
		pom = None
		if text is None:
			self._problem(f"{g}:{a}:{v} could not be read from Maven Central (missing, or still rate-limited after retries)")
		else:
			self.fetched_urls.append(url)
			try:
				pom = parse_pom(text)
			except ET.ParseError as err:
				self._problem(f"{g}:{a}:{v} is not valid XML ({err})")
		self._central_cache[url] = pom
		return pom, url

	def chain(self, pom: Pom, where: str, path: str | None) -> list[tuple[Pom, str]]:
		"""[(pom, where)] from the given POM up to the root ancestor that could be read."""
		out = [(pom, where)]
		while len(out) <= MAX_DEPTH:
			child, _ = out[-1]
			if not child.parent or not child.parent[1]:
				return out
			g, a, v, rel = child.parent
			parent, src = None, None
			if path is not None and rel:
				candidate = posixpath.normpath(posixpath.join(posixpath.dirname(path), rel))
				if not candidate.endswith(".xml"):
					candidate = posixpath.join(candidate, "pom.xml")
				text = self.load_repo(candidate)
				if text is not None:
					try:
						found = parse_pom(text)
					except ET.ParseError:
						found = None
					if found is not None and found.artifact == a:
						parent, src, path = found, candidate, candidate
			if parent is None:
				path = None  # past the repository: further relativePaths mean nothing
				if not v:
					self._problem(f"parent {g}:{a} has no version")
					return out
				parent, src = self._central(g, a, v)
				if parent is None:
					return out
			out.append((parent, src))
		self._problem(f"{where}: parent chain deeper than {MAX_DEPTH}")
		return out

	def resolve(self, pom: Pom, where: str, path: str | None, wanted: set[str], depth: int = 0) -> Resolved:
		chain = self.chain(pom, where, path)
		props: dict[str, str] = {}
		for p, _ in reversed(chain):  # root first, so the child's values win
			props.update(p.properties)
		for key, value in (("project.version", pom.version), ("project.groupId", pom.group),
				("project.artifactId", pom.artifact), ("version", pom.version), ("pom.version", pom.version)):
			if value:
				props[key] = value
		if pom.parent:
			props["project.parent.version"] = pom.parent[2]
			props["project.parent.groupId"] = pom.parent[0]

		managed: dict[str, tuple[str, str]] = {}
		imports: list[tuple[str, str, str]] = []
		for p, src in chain:  # child first, so the child's entries win
			for ga, raw in p.managed.items():
				managed.setdefault(ga, (substitute(raw, props), src))
			imports.extend(p.imports)

		remaining = set(wanted) - set(managed)
		if remaining and depth < MAX_DEPTH:
			resolved_imports = []
			for g, a, v in imports:
				v = substitute(v, props)
				if not v or _PROP.search(v):
					self._problem(f"BOM {g}:{a} has an unresolved version {v!r}")
					continue
				resolved_imports.append((g, a, v))
			# Group-specific BOMs (sharing 2+ groupId segments with an unmanaged dependency) first,
			# then a few others in Maven's declaration order, so one unmanageable artifact cannot
			# trigger a read of every BOM in a large platform.
			close = [gav for gav in resolved_imports if _affinity(gav[0], remaining) >= 2]
			close.sort(key=lambda gav: -_affinity(gav[0], remaining))  # stable: ties keep Maven's order
			others = [gav for gav in resolved_imports if gav not in close]
			for group, budget in ((close, None), (others, OTHER_BOM_BUDGET)):
				spent = 0
				for g, a, v in group:
					if not remaining or (budget is not None and spent >= budget):
						break
					bom, url = self._central(g, a, v)
					spent += 1
					if bom is None:
						continue
					sub = self.resolve(bom, url, None, remaining, depth + 1)
					for ga in list(remaining):
						if ga in sub.managed:
							managed[ga] = sub.managed[ga]
							remaining.discard(ga)
				if budget is not None and spent >= budget and remaining and len(group) > spent:
					self.skipped_boms += len(group) - spent
		return Resolved(props, managed, self.problems)


def resolve_dependencies(pom: Pom, res: Resolved) -> list[tuple[str, str, str, str | None]]:
	"""(g:a, version or "*", scope, where the version came from) for each declared dependency."""
	out = []
	for d in pom.dependencies:
		ga = f"{d['groupId']}:{d['artifactId']}"
		raw = d.get("version")
		if raw:
			version = substitute(raw, res.properties)
			origin = "unresolved property" if _PROP.search(version) else None
		elif ga in res.managed:
			version, where = res.managed[ga]
			origin = f"managed in {where}"
		else:
			version, origin = "*", "no version declared or managed"
		scope = "dev" if d.get("scope") == "test" else "runtime"
		out.append((ga, version, scope, origin))
	return out


def wanted_artifacts(pom: Pom) -> set[str]:
	return {f"{d['groupId']}:{d['artifactId']}" for d in pom.dependencies if not d.get("version")}
