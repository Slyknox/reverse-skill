"""NuGet versions and ranges: a port of NuGet.Versioning (NuGet/NuGet.Client, dev branch).

Ported from NuGetVersionFactory, SemanticVersionFactory, VersionComparer
(VersionRelease mode, the default), VersionRangeFactory, VersionRangeBase,
VersionRange.IsBetter/FindBestMatch and FloatRange. Checked against the
InlineData cases of NuGet's own unit tests (tests/fixtures/nuget_cases.json,
extracted by tests/tools/nuget_cases/extract.py).

What a range resolves to follows NuGet's resolver: the lowest version that
satisfies the range; for a floating range (1.*, 1.0.0-beta.*), the highest
version the float matches. Pre-releases are left out unless a bound is a
pre-release or the float asks for them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_INT_MAX = 2**31 - 1


@dataclass(frozen=True)
class NuGetVersion:
	major: int
	minor: int
	patch: int
	revision: int
	labels: tuple[str, ...]  # empty for a release
	metadata: str = ""

	@property
	def is_prerelease(self) -> bool:
		return bool(self.labels)

	@property
	def release(self) -> str:
		return ".".join(self.labels)

	def normalized(self) -> str:
		"""ToNormalizedString: Major.Minor.Patch[.Revision when not 0][-labels], without metadata."""
		core = f"{self.major}.{self.minor}.{self.patch}" + (f".{self.revision}" if self.revision else "")
		return core + (f"-{self.release}" if self.labels else "")


def _is_ld(c: str) -> bool:
	return ("0" <= c <= "9") or ("A" <= c <= "Z") or ("a" <= c <= "z") or c == "-"


def _valid_part(s: str, allow_leading_zeros: bool) -> bool:
	if not s:
		return False
	if not allow_leading_zeros and len(s) > 1 and s[0] == "0" and s[1:].isdigit() and s.isascii():
		return False
	return all(_is_ld(c) for c in s)


def _sections(value: str) -> tuple[str | None, list[str] | None, str | None]:
	"""SemanticVersionFactory.ParseSections."""
	version = labels = metadata = None
	version_end = plus = -1
	for i, ch in enumerate(value):
		end = i == len(value) - 1
		if version_end < 0:
			if end or ch in "-+":
				version = value[: i + (1 if end else 0)]
				version_end = i
				if ch == "+":
					plus = i
		elif plus < 0:
			if end or ch == "+":
				labels = value[version_end + 1: i + (1 if end else 0)].split(".")
				plus = i
		elif end:
			metadata = value[plus + 1:]
	return version, labels, metadata


def _numbers(s: str) -> tuple[int, int, int, int] | None:
	"""NuGetVersionFactory.TryGetNormalizedVersion: 1 to 4 dot-separated int32 parts, whitespace allowed around."""
	if not s or s.isspace():
		return None
	parts, pos = [], 0
	for _ in range(4):
		if pos == len(s):
			parts.append(0)
			continue
		while pos < len(s) and s[pos].isspace():
			pos += 1
		if pos < len(s) and not ("0" <= s[pos] <= "9"):
			return None
		n, digits, done = 0, False, False
		while pos < len(s):
			c = s[pos]
			if "0" <= c <= "9":
				digits = True
				n = n * 10 + ord(c) - 48
				if n > _INT_MAX:
					return None
				pos += 1
			elif c == ".":
				pos += 1
				if pos == len(s):
					return None  # ends with "."
				done = True
				break
			elif c.isspace():
				break
			else:
				return None
		if not digits:
			return None
		if pos == len(s):
			done = True
		if not done:  # trailing whitespace, then "." or the end
			while pos < len(s):
				c = s[pos]
				if not c.isspace():
					if c != ".":
						return None
					pos += 1
					if pos == len(s):
						return None
					break
				pos += 1
		parts.append(n)
	if pos != len(s):
		return None
	return tuple(parts)


def parse_version(value: str | None) -> NuGetVersion | None:
	"""NuGetVersion.TryParse; None when NuGet would refuse the string."""
	if value is None:
		return None
	version, labels, metadata = _sections(value.strip())
	if not version:
		return None
	nums = _numbers(version)
	if nums is None:
		return None
	if labels is not None and not all(_valid_part(p, False) for p in labels):
		return None
	if metadata is not None and not all(_valid_part(p, True) for p in metadata.split(".")):
		return None
	return NuGetVersion(*nums, tuple(labels or ()), metadata or "")


_INT_LABEL = re.compile(r"^[+-]?[0-9]+$")


def _label_int(s: str) -> int | None:
	if _INT_LABEL.match(s):
		n = int(s)
		if -(2**31) <= n <= _INT_MAX:
			return n
	return None


def _compare_label(a: str, b: str) -> int:
	ia, ib = _label_int(a), _label_int(b)
	if ia is not None and ib is not None:
		return (ia > ib) - (ia < ib)
	if ia is not None:
		return -1
	if ib is not None:
		return 1
	ua, ub = a.upper(), b.upper()  # StringComparer.OrdinalIgnoreCase
	return (ua > ub) - (ua < ub)


def compare(x: NuGetVersion, y: NuGetVersion) -> int:
	"""VersionComparer.Default: numbers (with the 4th part), then release labels; metadata ignored."""
	for a, b in ((x.major, y.major), (x.minor, y.minor), (x.patch, y.patch), (x.revision, y.revision)):
		if a != b:
			return -1 if a < b else 1
	if x.labels and not y.labels:
		return -1
	if y.labels and not x.labels:
		return 1
	for n in range(max(len(x.labels), len(y.labels))):
		if n >= len(x.labels):
			return -1
		if n >= len(y.labels):
			return 1
		result = _compare_label(x.labels[n], y.labels[n])
		if result:
			return result
	return 0


def compare_strings(a: str, b: str) -> int:
	va, vb = parse_version(a), parse_version(b)
	if va is None or vb is None:
		raise ValueError(f"not a NuGet version: {a if va is None else b!r}")
	return compare(va, vb)


# ---------- floating ranges ----------

FLOAT_PRERELEASE = {"Prerelease", "PrereleaseMajor", "PrereleaseMinor", "PrereleasePatch", "PrereleaseRevision", "AbsoluteLatest"}


@dataclass(frozen=True)
class FloatRange:
	behavior: str  # None | Prerelease | Revision | Patch | Minor | Major | Prerelease{Major,Minor,Patch,Revision} | AbsoluteLatest
	min_version: NuGetVersion
	release_prefix: str | None = None

	@property
	def include_prerelease(self) -> bool:
		return self.behavior in FLOAT_PRERELEASE

	def satisfies(self, v: NuGetVersion) -> bool:
		b, m, pre = self.behavior, self.min_version, (self.release_prefix or "").upper()
		if b == "AbsoluteLatest":
			return True
		if b == "Major" and not v.is_prerelease:
			return True
		prefix_ok = (v.is_prerelease and v.release.upper().startswith(pre)) or not v.is_prerelease
		if self.include_prerelease:
			if b == "PrereleaseRevision":
				return (m.major, m.minor, m.patch) == (v.major, v.minor, v.patch) and prefix_ok
			if b == "PrereleasePatch":
				return (m.major, m.minor) == (v.major, v.minor) and prefix_ok
			if b == "PrereleaseMinor":
				return m.major == v.major and prefix_ok
			if b == "PrereleaseMajor":
				return prefix_ok
			if b == "Prerelease":  # VersionComparer.Version: the numbers only
				return (m.major, m.minor, m.patch, m.revision) == (v.major, v.minor, v.patch, v.revision) and prefix_ok
		elif b == "Revision":
			return (m.major, m.minor, m.patch) == (v.major, v.minor, v.patch) and not v.is_prerelease
		elif b == "Patch":
			return (m.major, m.minor) == (v.major, v.minor) and not v.is_prerelease
		elif b == "Minor":
			return m.major == v.major and not v.is_prerelease
		return False


def _parts(line: str) -> int:
	return line.count(".") + 1


def parse_float(s: str | None) -> FloatRange | None:
	"""FloatRange.TryParse."""
	if s is None or not s.strip():
		return None
	first, last = s.find("*"), s.rfind("*")
	if s == "*":
		return FloatRange("Major", NuGetVersion(0, 0, 0, 0, ()))
	if s == "*-*":
		return FloatRange("AbsoluteLatest", parse_version("0.0.0-0"), "")
	if first != last and last != -1 and "+" not in s:
		dash = s.find("-")
		if dash != -1 and last == len(s) - 1 and first == dash - 1:
			stable = s[: dash - 1] + "0"
			behavior = {1: "PrereleaseMajor", 2: "PrereleaseMinor", 3: "PrereleasePatch", 4: "PrereleaseRevision"}.get(_parts(stable), "None")
			release = s[dash + 1:]
			prefix = release[:-1]
			part = prefix + ("0" if not prefix or prefix.endswith(".") else "")
			v = parse_version(stable + "-" + part)
			return FloatRange(behavior, v, prefix) if v else None
		return None
	if last == len(s) - 1 and "+" not in s:
		actual = s[:-1]
		prefix = None
		if "-" not in s:
			actual += "0"
			behavior = {2: "Minor", 3: "Patch", 4: "Revision"}.get(_parts(actual), "None")
		else:
			behavior = "Prerelease"
			if s.find("-") == s.rfind("-"):
				prefix = actual[s.rfind("-") + 1:]
				if not prefix or actual.endswith("."):
					actual += "0"
				elif actual.endswith("-"):
					actual += "-"
		v = parse_version(actual)
		return FloatRange(behavior, v, prefix) if v else None
	v = parse_version(s)
	return FloatRange("None", v) if v else None


# ---------- ranges ----------

@dataclass(frozen=True)
class VersionRange:
	min_version: NuGetVersion | None
	include_min: bool
	max_version: NuGetVersion | None
	include_max: bool
	float_range: FloatRange | None = None

	@property
	def is_floating(self) -> bool:
		return self.float_range is not None and self.float_range.behavior != "None"

	@property
	def has_prerelease_bounds(self) -> bool:
		return bool((self.min_version and self.min_version.is_prerelease) or (self.max_version and self.max_version.is_prerelease))

	def satisfies(self, v: NuGetVersion) -> bool:
		if self.min_version is not None:
			c = compare(self.min_version, v)
			if c > 0 or (c == 0 and not self.include_min):
				return False
		if self.max_version is not None:
			c = compare(self.max_version, v)
			if c < 0 or (c == 0 and not self.include_max):
				return False
		return True

	def is_better(self, current: NuGetVersion | None, considering: NuGetVersion | None) -> bool:
		"""VersionRange.IsBetter."""
		if considering is None or current == considering:
			return False
		if (not self.has_prerelease_bounds and considering.is_prerelease
				and not (self.float_range and self.float_range.behavior in FLOAT_PRERELEASE)):
			return False
		if not self.satisfies(considering):
			return False
		if current is None:
			return True
		if self.is_floating:
			fr = self.float_range
			cur_in, con_in = fr.satisfies(current), fr.satisfies(considering)
			if cur_in and not con_in:
				return False
			if con_in and not cur_in:
				return True
			if cur_in and con_in:
				return compare(current, considering) < 0
			cur_low = compare(current, fr.min_version) < 0
			con_low = compare(considering, fr.min_version) < 0
			if cur_low and not con_low:
				return True
			if not cur_low and con_low:
				return False
			if not cur_low and not con_low:
				return compare(current, considering) > 0
			return compare(current, considering) < 0
		return compare(current, considering) > 0

	def find_best_match(self, versions) -> NuGetVersion | None:
		best = None
		for v in versions:
			if self.is_better(best, v):
				best = v
		return best


def parse_range(value: str | None, allow_floating: bool = True) -> VersionRange | None:
	"""VersionRangeFactory.TryParse; None when NuGet would refuse the string."""
	if value is None:
		return None
	t = value.strip()
	if not t:
		return None
	if allow_floating and t == "*":
		return VersionRange(NuGetVersion(0, 0, 0, 0, ()), True, None, True, parse_float(t))
	float_range = None
	if t[0] in "([":
		include_min = t[0] == "["
		if t[-1] not in ")]":
			return None
		include_max = t[-1] == "]"
		parts = t[1:-1].split(",")
		if len(parts) > 2 or all(not p for p in parts):
			return None
		if len(parts) == 1 and not (include_min and include_max):
			return None
		min_s, max_s = parts[0], parts[1] if len(parts) == 2 else parts[0]
	else:
		include_min, include_max, min_s, max_s = True, False, t, None
	min_v = max_v = None
	if min_s is not None and min_s.strip():
		if allow_floating and "*" in min_s:
			float_range = parse_float(min_s)
			if float_range is None or float_range.min_version is None:
				return None
			min_v = float_range.min_version
		else:
			min_v = parse_version(min_s)
			if min_v is None:
				return None
	if max_s is not None and max_s.strip():
		max_v = parse_version(max_s)
		if max_v is None:
			return None
	if min_v is not None and max_v is not None:
		c = compare(min_v, max_v)
		if c > 0 or (c == 0 and include_min != include_max):
			return None
	return VersionRange(min_v, include_min, max_v, include_max, float_range)


def pick(spec: str, available: list[str]) -> str | None:
	"""The published version NuGet's resolver would choose for this range, or None."""
	rng = parse_range(spec)
	if rng is None:
		raise ValueError(f"not a NuGet version range: {spec!r}")
	parsed = [(v, parse_version(v)) for v in available]
	best = rng.find_best_match(p for _, p in parsed if p is not None)
	return next((raw for raw, p in parsed if p is best), None) if best else None
