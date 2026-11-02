import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from rea.activation import BuildContext, evaluate  # noqa: E402
from rea.deps import Dependency, FixtureRegistry, resolve_license  # noqa: E402
from rea.maven import parse_pom  # noqa: E402
from rea.nuget import compare_strings, parse_float, parse_range, parse_version, pick  # noqa: E402

with open(os.path.join(HERE, "fixtures", "nuget_cases.json"), encoding="utf-8") as fh:
	CASES = json.load(fh)["cases"]


def sign(n: int) -> int:
	return (n > 0) - (n < 0)


class NuGetTestSuiteTests(unittest.TestCase):
	"""Each test replays the InlineData rows of the NuGet.Versioning test of the same purpose."""

	def rows(self, name):
		self.assertTrue(CASES[name], f"no rows for {name}")
		return CASES[name]

	def test_equal(self):
		for name in ("VersionComparisonDefaultEqual", "VersionComparisonDefaultEqualWithNuGetVersion"):
			for a, b in self.rows(name):
				self.assertEqual(compare_strings(a, b), 0, f"{name}: {a} == {b}")

	def test_not_equal(self):
		for a, b in self.rows("VersionComparisonDefaultNotEqualWithNuGetVersion"):
			self.assertNotEqual(compare_strings(a, b), 0, f"{a} != {b}")
		# The default comparer ignores metadata and label case: NuGet's own Equal rows include these.
		self.assertEqual(compare_strings("1.0.0-BETA+AA", "1.0.0-beta"), 0)

	def test_less(self):
		for a, b in self.rows("VersionComparisonDefaultLess"):
			self.assertEqual(sign(compare_strings(a, b)), -1, f"{a} < {b}")
			self.assertEqual(sign(compare_strings(b, a)), 1, f"{b} > {a}")

	def test_is_better_with_prerelease(self):
		for rng, version, expected in self.rows("VersionRange_IsBetter_Prerelease"):
			self.assertEqual(parse_range(rng).is_better(None, parse_version(version)), expected, f"{rng} / {version}")

	def test_satisfies(self):
		for spec, version in self.rows("ParseVersionRangeSatisfies"):
			self.assertTrue(parse_range(spec).satisfies(parse_version(version)), f"{spec} contains {version}")
		for spec, version in self.rows("ParseVersionRangeDoesNotSatisfy"):
			self.assertFalse(parse_range(spec).satisfies(parse_version(version)), f"{spec} excludes {version}")

	def test_bad_ranges_are_refused(self):
		for (spec,) in self.rows("ParseVersionRangeWithBadVersionThrows"):
			self.assertIsNone(parse_range(spec), repr(spec))

	def test_missing_components_default_to_zero(self):
		for short, long in self.rows("VersionRange_MissingVersionComponents_DefaultToZero"):
			self.assertEqual(parse_range(short), parse_range(long), f"{short} == {long}")

	def test_float_parsing(self):
		for name, valid in (("FloatingRange_TryParse_Valid", True), ("FloatingRange_Parse_Valid", True),
				("FloatingRange_TryParse_Invalid", False), ("FloatingRange_Parse_Invalid", False)):
			for (spec,) in self.rows(name):
				self.assertEqual(parse_float(spec) is not None, valid, f"{name}: {spec!r}")
		for spec, normalized in self.rows("FloatRange_ParsesCorrectMinVersion"):
			self.assertEqual(parse_float(spec).min_version.normalized(), normalized, spec)


class NuGetBestMatchTests(unittest.TestCase):
	"""Transcribed from FloatingRangeTests' *Best tests and NuGet's documented resolution rules."""

	VERSIONS = ["0.1.0", "0.2.0", "1.0.0", "1.0.1-beta", "1.0.1", "1.1.0-alpha", "1.1.0", "1.2.0-rc.1", "2.0.0", "2.0.1-beta.2"]

	def test_plain_range_takes_the_lowest_release(self):
		self.assertEqual(pick("1.0.0", self.VERSIONS), "1.0.0")  # bare version: a minimum
		self.assertEqual(pick("[1.0.1,2.0.0)", self.VERSIONS), "1.0.1")
		self.assertEqual(pick("(1.0.0,]", self.VERSIONS), "1.0.1")  # the pre-release 1.0.1-beta is skipped

	def test_floating_takes_the_highest_match(self):
		self.assertEqual(pick("1.*", self.VERSIONS), "1.1.0")  # pre-releases do not float in
		self.assertEqual(pick("1.0.*", self.VERSIONS), "1.0.1")
		self.assertEqual(pick("*", self.VERSIONS), "2.0.0")
		self.assertEqual(pick("1.*-*", self.VERSIONS), "1.2.0-rc.1")  # pre-release float
		self.assertEqual(pick("*-*", self.VERSIONS), "2.0.1-beta.2")  # absolute latest

	def test_float_below_everything_falls_back_to_the_nearest(self):
		self.assertEqual(pick("3.*", self.VERSIONS), None)  # nothing satisfies the range minimum 3.0.0
		self.assertEqual(pick("0.*", ["1.0.0", "2.0.0"]), "1.0.0")  # FloatMinorPrefixNotFoundBest: lowest above

	def test_prerelease_bound_lets_prereleases_in(self):
		self.assertEqual(pick("[1.0.1-beta,2.0.0)", self.VERSIONS), "1.0.1-beta")
		self.assertIsNone(pick("[5.0.0,6.0.0)", self.VERSIONS))

	def test_parse_rules(self):
		self.assertIsNone(parse_version("v1.0.0"))
		self.assertIsNone(parse_version("1.0.0-01"))  # numeric label with a leading zero
		self.assertEqual(parse_version(" 1 . 2 ").normalized(), "1.2.0")
		self.assertEqual(parse_version("1.0.0.4-beta+meta").normalized(), "1.0.0.4-beta")
		self.assertIsNone(parse_range("[1.0,2.0),[3.0,4.0)"))  # NuGet allows one interval only
		self.assertIsNone(parse_range("(1.0]"))


class NuGetLookupTests(unittest.TestCase):
	def test_floating_versions_resolve_through_deps_dev(self):
		dd = "https://api.deps.dev/v3/systems/nuget/packages/Serilog"
		reg = FixtureRegistry({dd: {"versions": [{"versionKey": {"version": v}} for v in ("3.0.0", "3.1.0", "3.1.1", "4.0.0-dev")]},
			f"{dd}/versions/3.1.1": {"licenses": ["Apache-2.0"]}})
		lic, src, _, note, _ = resolve_license(Dependency("Serilog", "3.*", "nuget", "runtime", "App.csproj"), reg, 5)
		self.assertEqual((lic, src.rsplit("/", 1)[-1]), ("Apache-2.0", "3.1.1"))
		self.assertIn("as NuGet picks", note)


class ProfileConditionTests(unittest.TestCase):
	def test_env_conditions_use_only_listed_variables(self):
		ctx = BuildContext.from_args([], [], ["CI=true", "EMPTY="])
		self.assertEqual(evaluate("p", {"property": ("env.CI", None)}, ctx, None, None)[0], True)
		self.assertEqual(evaluate("p", {"property": ("env.CI", "false")}, ctx, None, None)[0], False)
		self.assertEqual(evaluate("p", {"property": ("!env.CI", None)}, ctx, None, None)[0], False)
		self.assertEqual(evaluate("p", {"property": ("env.EMPTY", None)}, ctx, None, None)[0], False)
		result = evaluate("p", {"property": ("env.HOME", None)}, ctx, None, None)
		self.assertEqual(result, (None, "needs --env HOME=..."))
		with self.assertRaises(ValueError):
			BuildContext.from_args([], [], ["NOVALUE"])

	def test_packaging_condition_uses_the_poms_packaging(self):
		pom = """<project><artifactId>a</artifactId><packaging>war</packaging><profiles>
			<profile><id>web</id><activation><packaging>war</packaging></activation></profile>
			<profile><id>lib</id><activation><packaging>jar</packaging></activation></profile>
			<profile><id>not-jar</id><activation><packaging>!jar</packaging></activation></profile></profiles></project>"""
		p = parse_pom(pom)
		self.assertEqual([x.split(" ")[0] for x in p.applied_profiles], ["web", "not-jar"])
		default_jar = parse_pom(pom.replace("<packaging>war</packaging><profiles>", "<profiles>"))
		self.assertEqual([x.split(" ")[0] for x in default_jar.applied_profiles], ["lib"])  # no <packaging> means jar


if __name__ == "__main__":
	unittest.main()
