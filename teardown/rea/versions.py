"""Version ordering and interval ranges for Maven and NuGet.

Both use the same range notation: "[1.0,2.0)" (closed/open bounds), "[1.0,)"
(at least), "(,1.0]" (at most), "[1.2]" (exactly), and several ranges joined by
commas ("[1,2),[3,4)") meaning any of them. They differ in which version a
range resolves to: Maven takes the highest release in the range, NuGet the
lowest. Ordering follows each tool closely enough for real version strings:

- Maven: a port of Maven 3.9.9's ComparableVersion, checked against that class
  itself (tests/fixtures/maven_order.json). Qualifiers order as
  alpha < beta < milestone < rc < snapshot < (release) < sp, then unknown
  qualifiers in text order; "1.0" equals "1"; "1a1" equals "1-alpha-1".
- NuGet: delegated to rea.nuget, a port of NuGet.Versioning (ordering, one-interval
  ranges, floating versions and the resolver's choice), checked against NuGet's own
  unit-test data.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass

# ---------- Maven: a port of org.apache.maven.artifact.versioning.ComparableVersion (Maven 3.9.9) ----------
# A version parses into nested lists of items: integers, qualifier strings, and sub-lists that
# start at "-" or at a change between digits and letters. Each list drops its trailing "null"
# items (0, a release-equivalent qualifier, an empty list). Comparison then follows the item rules
# below. Checked against Maven 3.9.9's own class: see tests/fixtures/maven_order.json.

_MVN_QUALIFIERS = ["alpha", "beta", "milestone", "rc", "snapshot", "", "sp"]
_MVN_ALIASES = {"ga": "", "final": "", "release": "", "cr": "rc"}
_RELEASE_INDEX = str(_MVN_QUALIFIERS.index(""))


def _qualifier_key(q: str) -> str:
	return str(_MVN_QUALIFIERS.index(q)) if q in _MVN_QUALIFIERS else f"{len(_MVN_QUALIFIERS)}-{q}"


def _cmp(a, b) -> int:
	return (a > b) - (a < b)


class _Str:
	__slots__ = ("value",)

	def __init__(self, value: str, followed_by_digit: bool):
		if followed_by_digit and len(value) == 1:
			value = {"a": "alpha", "b": "beta", "m": "milestone"}.get(value, value)  # a1 = alpha-1 ...
		self.value = _MVN_ALIASES.get(value, value)

	def is_null(self) -> bool:
		return _qualifier_key(self.value) == _RELEASE_INDEX


def _is_null(item) -> bool:
	if isinstance(item, int):
		return item == 0
	if isinstance(item, list):
		return len(item) == 0
	return item.is_null()


def _compare(item, other) -> int:
	"""Java's Item.compareTo; other may be None (a missing item)."""
	if isinstance(item, int):
		if other is None:
			return 0 if item == 0 else 1
		if isinstance(other, int):
			return _cmp(item, other)
		return 1  # 1.1 > 1-sp and 1.1 > 1-1
	if isinstance(item, _Str):
		if other is None:
			return _cmp(_qualifier_key(item.value), _RELEASE_INDEX)  # 1-rc < 1, 1-ga = 1
		if isinstance(other, _Str):
			return _cmp(_qualifier_key(item.value), _qualifier_key(other.value))
		return -1  # 1.any < 1.1 and 1.any < 1-1
	# a list
	if other is None:
		for sub in item:  # MNG-6964: the whole list against nothing, not just its first item
			result = _compare(sub, None)
			if result:
				return result
		return 0
	if isinstance(other, int):
		return -1  # 1-1 < 1.0.x
	if isinstance(other, _Str):
		return 1  # 1-1 > 1-sp
	for n in range(max(len(item), len(other))):
		left = item[n] if n < len(item) else None
		right = other[n] if n < len(other) else None
		result = (0 if right is None else -_compare(right, None)) if left is None else _compare(left, right)
		if result:
			return result
	return 0


def _parse_item(is_digit: bool, buf: str):
	return int(buf.lstrip("0") or "0") if is_digit else _Str(buf, False)


def _normalize(items: list) -> None:
	for n in range(len(items) - 1, -1, -1):
		if _is_null(items[n]):
			del items[n]  # trailing 0, "" (release) and empty lists do not count
		elif not isinstance(items[n], list):
			break


def maven_parse(version: str) -> list:
	version = version.lower()
	root: list = []
	current = root
	stack = [root]
	is_digit = False
	start = 0

	def open_sublist():
		nonlocal current
		new: list = []
		current.append(new)
		current = new
		stack.append(new)

	for n, c in enumerate(version):
		if c in ".-":
			current.append(0 if n == start else _parse_item(is_digit, version[start:n]))
			start = n + 1
			if c == "-":
				open_sublist()
		elif c.isdecimal():
			if not is_digit and n > start:
				if current:  # 1.0.0.X1 < 1.0.0-X2: treat .X as -X for any string qualifier X
					open_sublist()
				current.append(_Str(version[start:n], True))
				start = n
				open_sublist()
			is_digit = True
		else:
			if is_digit and n > start:
				current.append(_parse_item(True, version[start:n]))
				start = n
				open_sublist()
			is_digit = False
	if len(version) > start:
		if not is_digit and current:
			open_sublist()
		current.append(_parse_item(is_digit, version[start:]))
	while stack:
		_normalize(stack.pop())
	return root


def maven_compare(a: str, b: str) -> int:
	return _compare(maven_parse(a), maven_parse(b))


maven_key = functools.cmp_to_key(maven_compare)  # sort key in Maven 3.9.9's order


def _nuget_compare(a: str, b: str) -> int:
	from .nuget import compare_strings  # the port of NuGet.Versioning's default comparer

	return compare_strings(a, b)


nuget_key = functools.cmp_to_key(_nuget_compare)  # sort key in NuGet's order (rea.nuget)


def is_prerelease(ecosystem: str, version: str) -> bool:
	if ecosystem == "nuget":
		return "-" in version.split("+", 1)[0]
	return "snapshot" in version.lower()


@dataclass(frozen=True)
class Interval:
	low: str | None
	low_inclusive: bool
	high: str | None
	high_inclusive: bool

	def contains(self, version: str, key) -> bool:
		k = key(version)
		if self.low is not None:
			lk = key(self.low)
			if k < lk or (k == lk and not self.low_inclusive):
				return False
		if self.high is not None:
			hk = key(self.high)
			if k > hk or (k == hk and not self.high_inclusive):
				return False
		return True


_RANGE = re.compile(r"([\[(])\s*([^,\])]*)\s*(?:,\s*([^\])]*))?\s*([\])])")


def is_range(spec: str) -> bool:
	return spec.strip()[:1] in ("[", "(")


def parse_range(spec: str) -> list[Interval]:
	"""Intervals of a range spec; raises ValueError for something that is not one."""
	spec = spec.strip()
	pos, out = 0, []
	while pos < len(spec):
		m = _RANGE.match(spec, pos)
		if not m:
			raise ValueError(f"not a version range: {spec!r}")
		opening, low, high, closing = m.group(1), m.group(2).strip(), m.group(3), m.group(4)
		if high is None:  # "[1.2]": exactly this version
			if opening != "[" or closing != "]" or not low:
				raise ValueError(f"not a version range: {spec!r}")
			out.append(Interval(low, True, low, True))
		else:
			out.append(Interval(low or None, opening == "[", high.strip() or None, closing == "]"))
		pos = m.end()
		while pos < len(spec) and spec[pos] in ", ":
			pos += 1
	if not out:
		raise ValueError(f"not a version range: {spec!r}")
	return out


def pick(ecosystem: str, spec: str, available: list[str]) -> str | None:
	"""The version Maven (highest) or NuGet (lowest) would resolve the range to, from the published versions."""
	if ecosystem == "nuget":
		from .nuget import pick as nuget_pick  # the port of NuGet.Versioning, including floats

		return nuget_pick(spec, available)
	key = nuget_key if ecosystem == "nuget" else maven_key
	intervals = parse_range(spec)
	allow_pre = ecosystem != "nuget" or any(
		is_prerelease(ecosystem, b) for iv in intervals for b in (iv.low, iv.high) if b)
	matches = [v for v in available
		if (allow_pre or not is_prerelease(ecosystem, v)) and any(iv.contains(v, key) for iv in intervals)]
	if not matches:
		return None
	return (min if ecosystem == "nuget" else max)(matches, key=key)
