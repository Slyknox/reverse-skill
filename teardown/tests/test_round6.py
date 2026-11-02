import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from rea.activation import BuildContext, evaluate  # noqa: E402
from rea.deps import parse_manifest  # noqa: E402
from rea.maven import Resolver, parse_pom  # noqa: E402
from rea.versions import maven_compare, maven_key, nuget_key  # noqa: E402

with open(os.path.join(HERE, "fixtures", "maven_order.json"), encoding="utf-8") as fh:
	MAVEN = json.load(fh)


def sign(n: int) -> int:
	return (n > 0) - (n < 0)


class MavenOrderTests(unittest.TestCase):
	"""Answers recorded from Maven 3.9.9's own ComparableVersion (tests/tools/maven_order/regenerate.py)."""

	def test_maven_test_suite_orders(self):
		for name, order in MAVEN["orders"].items():
			for i, a in enumerate(order):
				for b in order[i + 1:]:
					self.assertEqual(sign(maven_compare(a, b)), -1, f"{name}: {a} < {b}")

	def test_every_recorded_pair_matches_maven(self):
		wrong = [(a, b, want, sign(maven_compare(a, b))) for a, b, want in MAVEN["pairs"] if sign(maven_compare(a, b)) != want]
		self.assertEqual(wrong, [], f"{len(wrong)} of {len(MAVEN['pairs'])} pairs differ from Maven")

	def test_maven_equalities(self):
		for a, b in [("1", "1.0.0"), ("1", "1-0"), ("1.0", "1.0-0"), ("1a", "1.0.0-a"), ("1x", "1.0-x"), ("1ga", "1"),
				("1cr", "1rc"), ("1a1", "1-alpha-1"), ("1m3", "1MILESTONE3"), ("1RELeaSE", "1"), ("1.0001", "1.1")]:
			self.assertEqual(maven_compare(a, b), 0, f"{a} == {b}")
		self.assertEqual(maven_key("1.0"), maven_key("1"))

	def test_semver_precedence_for_nuget(self):
		order = ["1.0.0-alpha", "1.0.0-alpha.1", "1.0.0-alpha.beta", "1.0.0-beta", "1.0.0-beta.2", "1.0.0-beta.11", "1.0.0-rc.1", "1.0.0"]
		self.assertEqual(sorted(reversed(order), key=nuget_key), order)


POM = """<project><artifactId>app</artifactId><properties><lib.version>1</lib.version></properties>
<dependencies><dependency><groupId>g</groupId><artifactId>lib</artifactId><version>${lib.version}</version></dependency></dependencies>
<profiles>
 <profile><id>default</id><activation><activeByDefault>true</activeByDefault></activation><properties><lib.version>2</lib.version></properties></profile>
 <profile><id>modern</id><activation><jdk>[17,)</jdk></activation><properties><lib.version>17</lib.version></properties></profile>
 <profile><id>release</id><activation><property><name>release</name></property></activation>
  <dependencies><dependency><groupId>g</groupId><artifactId>signer</artifactId><version>1</version></dependency></dependencies></profile>
 <profile><id>no-ci</id><activation><property><name>!ci</name></property></activation>
  <dependencies><dependency><groupId>g</groupId><artifactId>local-only</artifactId><version>1</version></dependency></dependencies></profile>
 <profile><id>docker</id><activation><file><exists>${basedir}/Dockerfile</exists></file></activation>
  <dependencies><dependency><groupId>g</groupId><artifactId>docker-tools</artifactId><version>1</version></dependency></dependencies></profile>
 <profile><id>win</id><activation><os><family>windows</family></os></activation>
  <dependencies><dependency><groupId>g</groupId><artifactId>win-only</artifactId><version>1</version></dependency></dependencies></profile>
</profiles></project>"""


def deps_with(ctx: BuildContext, files=("app/Dockerfile",)) -> dict:
	r = Resolver(lambda _p: None, lambda _u: None, max_fetches=0, ctx=ctx, file_exists=lambda p: p in files)
	return {d.name: d.spec for d in parse_manifest("app/pom.xml", POM, r)}


class ActivationTests(unittest.TestCase):
	def test_conditions_with_a_full_context(self):
		ctx = BuildContext.from_args(["release"], ["ci=true"], jdk="21", os_family="unix")
		deps = deps_with(ctx)
		self.assertEqual(deps["g:lib"], "17")  # jdk [17,) on, so the default profile is off
		self.assertIn("g:signer", deps)  # -P release
		self.assertNotIn("g:local-only", deps)  # !ci is false: -Dci is set
		self.assertIn("g:docker-tools", deps)  # app/Dockerfile exists in the repository
		self.assertNotIn("g:win-only", deps)

	def test_default_profile_applies_only_when_nothing_else_is_on(self):
		ctx = BuildContext.from_args([], ["ci=true"], jdk="11", os_family="unix")
		deps = deps_with(ctx, files=())
		self.assertEqual(deps["g:lib"], "2")  # everything decided and off, so the default applies
		pom = parse_pom(POM, ctx, "app", lambda p: False)
		self.assertTrue(pom.applied_profiles[0].startswith("default (activeByDefault)"))
		self.assertIn("modern (jdk [17,) vs 11)", pom.skipped_profiles)

	def test_unknown_conditions_are_reported_not_guessed(self):
		pom = parse_pom(POM, BuildContext(), None, None)  # a POM from Maven Central: no files, no context
		skipped = " | ".join(pom.skipped_profiles)
		self.assertIn("modern (unknown: needs --jdk)", skipped)
		self.assertIn("docker (unknown: file ${basedir}/Dockerfile: the POM is not in this repository)", skipped)
		self.assertIn("win (unknown: needs --os-family)", skipped)
		# !ci holds because no -Dci was given, so no-ci is on, and that alone turns the default off.
		self.assertEqual(pom.applied_profiles, ["no-ci (property !ci (not set))"])
		self.assertIn("default (activeByDefault, off because another profile is on)", skipped)

	def test_minus_p_overrides_conditions(self):
		ctx = BuildContext.from_args(["!no-ci,win"], [], jdk="21", os_family="unix")
		deps = deps_with(ctx)
		self.assertIn("g:win-only", deps)  # -P win wins over the os condition
		self.assertNotIn("g:local-only", deps)  # -P !no-ci

	def test_jdk_prefix_is_segment_aware_and_property_rules_follow_maven(self):
		ctx = BuildContext(jdk="1.8.0_392")
		self.assertEqual(evaluate("p", {"jdk": "1.8"}, ctx, None, None)[0], True)
		self.assertEqual(evaluate("p", {"jdk": "1"}, BuildContext(jdk="11"), None, None)[0], False)
		self.assertEqual(evaluate("p", {"jdk": "!1.8"}, ctx, None, None)[0], False)
		props = BuildContext.from_args([], ["env=prod", "empty="])
		self.assertEqual(evaluate("p", {"property": ("env", "prod")}, props, None, None)[0], True)
		self.assertEqual(evaluate("p", {"property": ("env", "!prod")}, props, None, None)[0], False)
		self.assertEqual(evaluate("p", {"property": ("missing", "!x")}, props, None, None)[0], True)  # not set != x
		self.assertEqual(evaluate("p", {"property": ("empty", None)}, props, None, None)[0], False)  # empty is not set
		self.assertEqual(evaluate("p", {"property": ("env.HOME", None)}, props, None, None)[0], None)

	def test_all_conditions_must_hold(self):
		ctx = BuildContext(jdk="21")
		both = {"jdk": "21", "property": ("release", None)}
		self.assertEqual(evaluate("p", both, ctx, None, None)[0], False)
		self.assertEqual(evaluate("p", both, BuildContext.from_args([], ["release"], jdk="21"), None, None)[0], True)


if __name__ == "__main__":
	unittest.main()
