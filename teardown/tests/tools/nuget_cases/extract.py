"""Rebuild tests/fixtures/nuget_cases.json from NuGet.Versioning's own unit tests.

Reads (never runs) the C# test files of NuGet/NuGet.Client and keeps the
InlineData rows of the tests listed in TESTS, with the test name, so that
tests/test_round7.py can check the Python port against NuGet's expected answers.

    python3 tests/tools/nuget_cases/extract.py [REF]   # REF defaults to dev
"""

import json
import os
import re
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "..", "fixtures", "nuget_cases.json")
BASE = "https://raw.githubusercontent.com/NuGet/NuGet.Client/{ref}/test/NuGet.Core.Tests/NuGet.Versioning.Test/"
TESTS = {
	# VersionComparisonDefaultNotEqual is left out on purpose: it calls the two-argument
	# object.Equals(string, string), so it checks that the strings differ, not the versions.
	"VersionComparerTests.cs": ["VersionComparisonDefaultEqual", "VersionComparisonDefaultEqualWithNuGetVersion",
		"VersionComparisonDefaultNotEqualWithNuGetVersion", "VersionComparisonDefaultLess"],
	"VersionRangeTests.cs": ["VersionRange_IsBetter_Prerelease", "VersionRange_MissingVersionComponents_DefaultToZero",
		"ParseVersionRangeDoesNotSatisfy", "ParseVersionRangeSatisfies", "ParseVersionRangeWithBadVersionThrows"],
	"FloatingRangeTests.cs": ["FloatRange_ParsesCorrectMinVersion", "FloatingRange_TryParse_Valid", "FloatingRange_TryParse_Invalid",
		"FloatingRange_Parse_Valid", "FloatingRange_Parse_Invalid"],
}
_ARG = re.compile(r'\s*(?:"((?:[^"\\]|\\.)*)"|(true|false|null))\s*(?:,|$)')


def args(text: str) -> list:
	out, pos = [], 0
	while pos < len(text):
		m = _ARG.match(text, pos)
		if not m:
			raise ValueError(f"cannot read InlineData arguments: {text!r}")
		if m.group(1) is not None:
			out.append(bytes(m.group(1), "utf-8").decode("unicode_escape"))
		else:
			out.append({"true": True, "false": False, "null": None}[m.group(2)])
		pos = m.end()
	return out


def extract(source: str, wanted: list[str]) -> dict[str, list]:
	cases: dict[str, list] = {name: [] for name in wanted}
	pending: list = []
	for line in source.splitlines():
		line = line.strip()
		m = re.match(r"\[InlineData\((.*)\)\]$", line)
		if m:
			pending.append(args(m.group(1)))
			continue
		m = re.match(r"public void (\w+)\(", line)
		if m:
			if m.group(1) in cases:
				cases[m.group(1)].extend(pending)
			pending = []
		elif line.startswith("[Theory") or line.startswith("[Fact"):
			pending = []
	return cases


def main() -> None:
	ref = sys.argv[1] if len(sys.argv) > 1 else "dev"
	result = {"source": f"NuGet/NuGet.Client@{ref} test/NuGet.Core.Tests/NuGet.Versioning.Test (InlineData rows)", "cases": {}}
	for name, wanted in TESTS.items():
		with urllib.request.urlopen(BASE.format(ref=ref) + name, timeout=30) as r:
			result["cases"].update(extract(r.read().decode("utf-8"), wanted))
	empty = [k for k, v in result["cases"].items() if not v]
	if empty:
		sys.exit(f"no InlineData found for {empty}; the test files changed shape")
	with open(OUT, "w", encoding="utf-8") as fh:
		json.dump(result, fh, indent=1, ensure_ascii=False)
		fh.write("\n")
	print(f"wrote {sum(len(v) for v in result['cases'].values())} cases to {OUT}")


if __name__ == "__main__":
	main()
