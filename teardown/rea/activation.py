"""Maven profile activation against a build context you describe.

Maven decides which profiles apply from the machine and command line that run
the build: the JDK, the OS, -D properties, -P profile ids, and files on disk.
A teardown cannot see that machine, so you describe it (`--jdk 21`,
`--os-family unix`, `-D release=true`, `-P ci`). Each condition then evaluates
to true, false, or unknown when the context does not say. Unknown is never
treated as true or false: such a profile is not applied, and the report says
which flag would decide it.

Rules followed (Maven 3.2.2 and later):
- All conditions inside one <activation> must hold.
- -P id turns a profile on whatever its conditions; -P !id turns it off.
- jdk: "11" or "1.8" matches by version prefix segment ("1.8" matches 1.8.0_392;
  "1" does not match 11); "!x" negates; a range like "[11,)" or "[1.8,17)" uses
  Maven version order.
- os: name, family, arch and version compared case-insensitively; "!x" negates.
- property: <name> alone means "set to a non-empty value"; "!name" means
  "not set"; with <value>, "!v" means "not equal" (and a "!" on the name is
  then ignored, as in Maven). Only the -D properties you give count as set,
  which is what a plain command-line build sees.
- file: <exists>/<missing>, after ${basedir} and ${project.basedir}, checked
  against the repository's file listing. Any other ${...} is unknown.
- property env.X: decided only by an --env X=value you give (an environment
  holds many variables, so one you do not list is unknown, not absent).
- packaging (Maven 4): the POM's own <packaging>, jar when absent; a leading
  "!" negates it, as for the other conditions.
- activeByDefault: on only when no other profile in the same POM is on. If any
  other profile's state is unknown, the default profile is applied and the
  report says it might not be.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass, field

from .versions import is_range, maven_key, parse_range


@dataclass
class BuildContext:
	profiles_on: set[str] = field(default_factory=set)
	profiles_off: set[str] = field(default_factory=set)
	properties: dict[str, str] = field(default_factory=dict)
	jdk: str | None = None
	os_name: str | None = None
	os_family: str | None = None
	os_arch: str | None = None
	os_version: str | None = None
	env: dict[str, str] = field(default_factory=dict)  # only the variables given; any other is unknown

	@classmethod
	def from_args(cls, profiles: list[str] | None = None, properties: list[str] | None = None,
			env: list[str] | None = None, **os_and_jdk) -> "BuildContext":
		ctx = cls(**{k: v for k, v in os_and_jdk.items() if v})
		for item in env or []:
			name, eq, value = item.partition("=")
			if not name or not eq:
				raise ValueError(f"--env needs NAME=value (NAME= for set but empty), got {item!r}")
			ctx.env[name] = value
		for chunk in profiles or []:
			for pid in (p.strip() for p in chunk.split(",")):
				if pid.startswith(("!", "-")):
					ctx.profiles_off.add(pid[1:])
				elif pid:
					ctx.profiles_on.add(pid)
		for prop in properties or []:
			name, _, value = prop.partition("=")
			if not name:
				raise ValueError(f"-D needs name=value, got {prop!r}")
			ctx.properties[name] = value if "=" in prop else "true"  # Maven: -Dname alone sets "true"
		return ctx

	def describe(self) -> str:
		parts = [f"-P {','.join(sorted(self.profiles_on))}" if self.profiles_on else "",
			f"-P !{',!'.join(sorted(self.profiles_off))}" if self.profiles_off else "",
			" ".join(f"-D{k}={v}" for k, v in sorted(self.properties.items())),
			f"jdk {self.jdk}" if self.jdk else "", f"os family {self.os_family}" if self.os_family else "",
			f"os name {self.os_name}" if self.os_name else "", f"os arch {self.os_arch}" if self.os_arch else "",
			" ".join(f"env {k}={v}" for k, v in sorted(self.env.items()))]
		return ", ".join(p for p in parts if p) or "no build context given"


# Tri-state result: (True | False | None, reason)
Result = tuple


def _neg(value: str) -> tuple[bool, str]:
	value = value.strip()
	return (True, value[1:].strip()) if value.startswith("!") else (False, value)


def _jdk(spec: str, ctx: BuildContext) -> Result:
	if not ctx.jdk:
		return None, "needs --jdk"
	negate, spec = _neg(spec)
	if is_range(spec):
		try:
			hit = any(iv.contains(ctx.jdk, maven_key) for iv in parse_range(spec))
		except ValueError:
			return None, f"unreadable jdk range {spec!r}"
	else:
		want = re.split(r"[._-]", spec)
		have = re.split(r"[._-]", ctx.jdk)
		hit = have[: len(want)] == want
	hit = hit != negate
	return hit, f"jdk {'!' if negate else ''}{spec} vs {ctx.jdk}"


def _os(el_fields: dict[str, str], ctx: BuildContext) -> Result:
	given = {"name": ctx.os_name, "family": ctx.os_family, "arch": ctx.os_arch, "version": ctx.os_version}
	for key in ("name", "family", "arch", "version"):
		if key not in el_fields:
			continue
		if not given[key]:
			return None, f"needs --os-{key}"
		negate, want = _neg(el_fields[key])
		if (want.lower() == given[key].lower()) == negate:
			return False, f"os {key} {el_fields[key]} vs {given[key]}"
	return True, "os matches"


def _property(name: str | None, value: str | None, ctx: BuildContext) -> Result:
	if not name:
		return None, "property activation without a name"
	negate_name, name = _neg(name)
	if name.startswith("env."):
		# Environments hold many variables, so absence from the context proves nothing: only listed ones count.
		if name[4:] not in ctx.env:
			return None, f"needs --env {name[4:]}=..."
		have = ctx.env[name[4:]]
	else:
		have = ctx.properties.get(name)
	if not value:  # as Maven: "set" means a non-empty value, and "!name" reverses it
		is_set = bool(have)
		return is_set != negate_name, f"property {'!' if negate_name else ''}{name} ({'set' if is_set else 'not set'})"
	negate_value, value = _neg(value)  # with a value, Maven ignores a "!" on the name
	return (have == value) != negate_value, f"property {name}={have if have is not None else '(not set)'} vs {'!' if negate_value else ''}{value}"


def _file(exists: str | None, missing: str | None, pom_dir: str | None, file_exists) -> Result:
	path, want_present = (exists, True) if exists else (missing, False)
	if path is None:
		return None, "file activation without a path"
	if pom_dir is None or file_exists is None:
		return None, f"file {path}: the POM is not in this repository"
	resolved = re.sub(r"\$\{(project\.)?basedir\}", pom_dir or ".", path.strip())
	if "${" in resolved:
		return None, f"file {path}: unresolved property"
	resolved = posixpath.normpath(resolved if not posixpath.isabs(resolved) else resolved.lstrip("/"))
	present = file_exists(resolved)
	if present is None:
		return None, f"file {path}: not known"
	return present == want_present, f"file {resolved} {'exists' if present else 'missing'}"


def _packaging(spec: str, packaging: str | None) -> Result:
	negate, want = _neg(spec)
	have = packaging or "jar"  # Maven's default packaging
	return (have == want) != negate, f"packaging {have} vs {'!' if negate else ''}{want}"


def evaluate(profile_id: str, activation: dict | None, ctx: BuildContext, pom_dir: str | None, file_exists,
		packaging: str | None = None) -> Result:
	"""activation: {"jdk": str, "os": {...}, "property": (name, value), "file": (exists, missing)} or None."""
	if profile_id in ctx.profiles_off:
		return False, "turned off with -P !" + profile_id
	if profile_id in ctx.profiles_on:
		return True, "turned on with -P " + profile_id
	if not activation:
		return False, "activates only with -P"
	results = []
	if "jdk" in activation:
		results.append(_jdk(activation["jdk"], ctx))
	if "os" in activation:
		results.append(_os(activation["os"], ctx))
	if "property" in activation:
		results.append(_property(*activation["property"], ctx))
	if "file" in activation:
		results.append(_file(*activation["file"], pom_dir, file_exists))
	if "packaging" in activation:
		results.append(_packaging(activation["packaging"], packaging))
	if not results:
		return False, "no activation condition"
	if any(r[0] is False for r in results):
		return False, "; ".join(r[1] for r in results if r[0] is False)
	if any(r[0] is None for r in results):
		return None, "; ".join(r[1] for r in results if r[0] is None)
	return True, "; ".join(r[1] for r in results)
