"""Version ordering and interval ranges for Maven and NuGet.

Both use the same range notation: "[1.0,2.0)" (closed/open bounds), "[1.0,)"
(at least), "(,1.0]" (at most), "[1.2]" (exactly), and several ranges joined by
commas ("[1,2),[3,4)") meaning any of them. They differ in which version a
range resolves to: Maven takes the highest release in the range, NuGet the
lowest. Ordering follows each tool closely enough for real version strings:

- Maven: numeric segments compare as numbers, trailing zeros do not count
  ("1.0" equals "1"), and qualifiers order as
  alpha < beta < milestone < rc < snapshot < (release) < sp, with unknown
  qualifiers after sp in text order.
- NuGet: SemVer-style; a pre-release ("1.0.0-beta") sorts before its release
  and is left out of a range unless a bound itself is a pre-release.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_QUALIFIERS = {"alpha": 0, "a": 0, "beta": 1, "b": 1, "milestone": 2, "m": 2, "rc": 3, "cr": 3,
	"snapshot": 4, "": 5, "ga": 5, "final": 5, "release": 5, "sp": 6}


def maven_key(version: str) -> tuple:
	"""Sort key in Maven's order (a simplification of ComparableVersion that agrees on common versions)."""
	tokens = re.findall(r"\d+|[a-zA-Z]+", version.lower())
	items: list[tuple] = []
	for tok in tokens:
		if tok.isdigit():
			items.append((1, int(tok), ""))
		else:
			rank = _QUALIFIERS.get(tok)
			items.append((0, rank, "") if rank is not None else (0, 7, tok))
	# Zeros at the end of a numeric run, and release-equivalent qualifiers, do not change a version:
	# 1.0 == 1 == 1.0-final, and 1.0-beta == 1-beta. A zero followed by another number stays (1.0.1).
	cleaned: list[tuple] = []
	for i, item in enumerate(items):
		if item == (0, 5, ""):
			continue
		if item == (1, 0, ""):
			following = next((x for x in items[i + 1:] if x not in ((1, 0, ""), (0, 5, ""))), None)
			if following is None or following[0] == 0:  # only zeros, then a qualifier or the end
				continue
		cleaned.append(item)
	items = cleaned
	# A version that ends here is a release: it sorts after its own pre-releases (qualifier < 5)
	# and before its service packs; padding with a release marker gets that order.
	return tuple(items) + ((0, 5, ""),)


def nuget_key(version: str) -> tuple:
	core, _, pre = version.lstrip("vV").partition("-")
	core = core.split("+", 1)[0]
	nums = [int(n) if n.isdigit() else 0 for n in core.split(".")]
	while len(nums) < 4:
		nums.append(0)
	pre_parts = tuple((0, int(p), "") if p.isdigit() else (1, 0, p.lower()) for p in pre.split(".")) if pre else ()
	# A release (no pre-release part) sorts after every pre-release of the same numbers.
	return (tuple(nums), 0 if pre else 1, pre_parts)


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
	key = nuget_key if ecosystem == "nuget" else maven_key
	intervals = parse_range(spec)
	allow_pre = ecosystem != "nuget" or any(
		is_prerelease(ecosystem, b) for iv in intervals for b in (iv.low, iv.high) if b)
	matches = [v for v in available
		if (allow_pre or not is_prerelease(ecosystem, v)) and any(iv.contains(v, key) for iv in intervals)]
	if not matches:
		return None
	return (min if ecosystem == "nuget" else max)(matches, key=key)
