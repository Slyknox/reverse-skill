import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from rea.company import load_dossier  # noqa: E402
from rea.deps import parse_manifest  # noqa: E402
from rea.diff import diff, diff_md  # noqa: E402
from rea.judge import MAX_CLAIMS, JudgeError, judge_matches  # noqa: E402
from rea.maven import parse_pom  # noqa: E402
from rea.render import to_json  # noqa: E402
from rea.versions import maven_key, nuget_key, parse_range, pick  # noqa: E402


class VersionTests(unittest.TestCase):
	def test_maven_order(self):
		order = ["1.0-alpha-1", "1.0-beta", "1.0-rc1", "1.0-SNAPSHOT", "1.0", "1.0-sp1", "1.0.1", "1.1", "1.10", "2.0"]
		self.assertEqual(sorted(reversed(order), key=maven_key), order)
		self.assertEqual(maven_key("1.0"), maven_key("1"))
		self.assertEqual(maven_key("1.0.0"), maven_key("1.0-final"))
		self.assertEqual(maven_key("1.0-beta"), maven_key("1-beta"))

	def test_nuget_order(self):
		self.assertEqual(sorted(["1.0.1", "1.0.0", "1.0.0-beta", "1.0.0-alpha", "0.9"], key=nuget_key),
			["0.9", "1.0.0-alpha", "1.0.0-beta", "1.0.0", "1.0.1"])

	def test_range_syntax(self):
		self.assertEqual(len(parse_range("[1,2),[3,4]")), 2)
		exact = parse_range("[1.2]")[0]
		self.assertTrue(exact.contains("1.2", maven_key) and not exact.contains("1.3", maven_key))
		for bad in ("1.0", "[1.0", "[,]x"):
			with self.assertRaises(ValueError):
				parse_range(bad)

	def test_maven_takes_highest_nuget_takes_lowest(self):
		vs = ["1.0", "1.5", "1.9.9", "2.0", "2.1-SNAPSHOT", "3.0"]
		self.assertEqual(pick("maven", "[1.0,2.0)", vs), "1.9.9")
		self.assertEqual(pick("maven", "(,1.5]", vs), "1.5")
		self.assertEqual(pick("maven", "[2.0,)", vs), "3.0")
		nv = ["3.0.0", "3.1.0", "3.1.1", "3.2.0-dev", "4.0.0"]
		self.assertEqual(pick("nuget", "[3.1,4.0)", nv), "3.1.0")
		self.assertEqual(pick("nuget", "[3.2.0-dev,4.0)", nv), "3.2.0-dev")  # pre-release allowed only when a bound is one
		self.assertIsNone(pick("nuget", "[5,6)", nv))

	def test_maven_range_includes_prereleases_of_the_upper_bound(self):
		# Maven orders 2.0.0-beta1 before 2.0, so "[1.7,2.0)" contains it: a known Maven behaviour, reproduced on purpose.
		self.assertEqual(pick("maven", "[1.7,2.0)", ["1.7.36", "2.0.0-beta1", "2.0.0"]), "2.0.0-beta1")


PROFILES = """<project><artifactId>app</artifactId><properties><x.version>1</x.version></properties>
<dependencies><dependency><groupId>g</groupId><artifactId>base</artifactId><version>${x.version}</version></dependency></dependencies>
<profiles>
 <profile><id>default</id><activation><activeByDefault>true</activeByDefault></activation>
  <properties><x.version>2</x.version></properties>
  <dependencyManagement><dependencies><dependency><groupId>g</groupId><artifactId>managed</artifactId><version>7</version></dependency></dependencies></dependencyManagement>
  <dependencies><dependency><groupId>g</groupId><artifactId>extra</artifactId><version>3</version></dependency>
   <dependency><groupId>g</groupId><artifactId>managed</artifactId></dependency></dependencies></profile>
 <profile><id>jdk21</id><activation><jdk>21</jdk></activation>
  <dependencies><dependency><groupId>g</groupId><artifactId>jdk-only</artifactId><version>1</version></dependency></dependencies></profile>
 <profile><id>release</id></profile>
</profiles></project>"""


class ProfileTests(unittest.TestCase):
	def test_active_by_default_profile_is_applied_and_others_are_listed(self):
		pom = parse_pom(PROFILES)  # no build context: the JDK profile is unknown, so the default carries a caveat
		self.assertEqual(pom.applied_profiles, ["default (activeByDefault, but another profile's state is unknown and could turn it off)"])
		self.assertEqual(pom.skipped_profiles, ["jdk21 (unknown: needs --jdk)", "release (activates only with -P)"])
		deps = {d.name: d.spec for d in parse_manifest("pom.xml", PROFILES)}
		self.assertEqual(deps, {"g:base": "2", "g:extra": "3", "g:managed": "7"})  # profile property wins; jdk21 not applied


def company(entity, claims):
	src = [{"url": "https://a.example/"}]
	return json.loads(to_json(load_dossier({"entity": entity, "claims": [{"kind": k, "text": t, "sources": src} for k, t in claims]})))


class JudgeTests(unittest.TestCase):
	OURS = company("Us", [("weakness", "Implementation takes months"), ("weakness", "No single sign-on"), ("fact", "Founded 2019")])
	THEIRS = company("Them", [("strength", "Setup done in minutes"), ("strength", "Okta and Azure AD login built in")])

	def fake(self, pairs):
		seen = []

		def complete(system, user, schema):
			seen.append(user)
			return {"pairs": pairs}, "claude-opus-5-5"
		return complete, seen

	def test_model_adds_pairs_the_vocabulary_misses_and_only_sees_unmatched_claims(self):
		complete, seen = self.fake([{"ours": "O1", "theirs": "T1", "reason": "enterprise sign-in"}])
		result = diff(self.OURS, self.THEIRS, complete)
		self.assertNotIn("Implementation takes months", seen[0])  # already matched by the vocabulary
		by = {(m["ours"]["text"], m["found_by"]) for m in result["suggested_matches"]}
		self.assertEqual(by, {("Implementation takes months", "vocabulary"), ("No single sign-on", "model (claude-opus-5-5)")})
		sso = next(m for m in result["suggested_matches"] if m["found_by"].startswith("model"))
		self.assertEqual((sso["meaning"], sso["score"], sso["model_reason"]), ("possible gap", None, "enterprise sign-in"))
		self.assertIn("model (claude-opus-5-5): enterprise sign-in", diff_md(result))

	def test_invented_ids_reuse_and_disallowed_kind_pairs_are_dropped(self):
		ours = [{"kind": "fact", "text": "a"}, {"kind": "weakness", "text": "b"}]
		theirs = [{"kind": "strength", "text": "c"}, {"kind": "strength", "text": "d"}]
		complete, _ = self.fake([
			{"ours": "O9", "theirs": "T1", "reason": "invented"},
			{"ours": "O1", "theirs": "T1", "reason": "fact vs strength is not a pairing"},
			{"ours": "O2", "theirs": "T2", "reason": "kept"},
			{"ours": "O2", "theirs": "T1", "reason": "O2 already used"},
			{"ours": "x", "theirs": "T1", "reason": "not an id"},
		])
		out = judge_matches(ours, theirs, complete, [])
		self.assertEqual([(m["ours"]["text"], m["theirs"]["text"], m["model_reason"]) for m in out], [("b", "d", "kept")])

	def test_nothing_sent_when_too_many_claims_or_nothing_left(self):
		complete, seen = self.fake([])
		with self.assertRaises(JudgeError):
			judge_matches([{"kind": "fact", "text": str(i)} for i in range(MAX_CLAIMS + 1)], [{"kind": "fact", "text": "x"}], complete, [])
		self.assertEqual(judge_matches([], [{"kind": "fact", "text": "x"}], complete, []), [])
		self.assertEqual(seen, [])

	def test_judge_refused_for_repo_reports(self):
		repo = {"repo": "a/b", "ledger": {"sourced": [], "unverified": []}}
		with self.assertRaises(ValueError):
			diff(repo, repo, self.fake([])[0])


if __name__ == "__main__":
	unittest.main()
