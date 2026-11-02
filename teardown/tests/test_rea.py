import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from rea.cli import main  # noqa: E402
from rea.company import load_dossier  # noqa: E402
from rea.evidence import Claim, Evidence, EvidenceError, Ledger  # noqa: E402
from rea.figures import Figure, rank  # noqa: E402
from rea.github import FixtureClient  # noqa: E402
from rea.repo_teardown import teardown_repo  # noqa: E402

NOW = datetime(2026, 11, 2, tzinfo=timezone.utc)
SRC = [Evidence("https://example.org/10-k")]


def fixture(name):
	with open(os.path.join(HERE, "fixtures", name), encoding="utf-8") as fh:
		return FixtureClient(json.load(fh))


class EvidenceTests(unittest.TestCase):
	def test_unsourced_and_non_https_claims_are_unverified(self):
		ledger = Ledger()
		ledger.add(Claim("strength", "sourced", SRC))
		ledger.add(Claim("strength", "no source"))
		ledger.add(Claim("strength", "http only", [Evidence("http://example.org")]))
		self.assertEqual([c.text for c in ledger.sourced], ["sourced"])
		self.assertEqual(len(ledger.unverified), 2)

	def test_bad_claim_kind_and_empty_text_are_refused(self):
		with self.assertRaises(EvidenceError):
			Claim("rumour", "x")
		with self.assertRaises(EvidenceError):
			Claim("fact", "  ")


class FigureTests(unittest.TestCase):
	def test_ranks_only_within_one_metric_kind(self):
		groups = rank([
			Figure("Palantir", "revenue_guidance", 8150, 8150, sources=SRC),
			Figure("Databricks", "arr", 6900, 6900, sources=SRC),
			Figure("Sierra", "arr", 150, 150),
			Figure("Genesys", "arr", 2600, 2600),
		])
		self.assertEqual(list(groups), ["arr", "revenue_guidance"])
		self.assertEqual([f.entity for f in groups["arr"]], ["Databricks", "Genesys", "Sierra"])

	def test_wide_range_is_disputed_and_ranked_by_low_end(self):
		a = Figure("A", "annualized_run_rate", 47000, 74000)
		b = Figure("B", "annualized_run_rate", 50000, 50000)
		self.assertEqual(a.flags, ["UNVERIFIED", "DISPUTED"])
		self.assertEqual([f.entity for f in rank([a, b])["annualized_run_rate"]], ["B", "A"])
		self.assertFalse(Figure("C", "arr", 900, 1100, sources=SRC).disputed)

	def test_invalid_figures_and_mixed_units_are_refused(self):
		with self.assertRaises(EvidenceError):
			Figure("X", "arr", 10, 5)
		with self.assertRaises(EvidenceError):
			Figure("X", "made_up_metric", 10, 10)
		with self.assertRaises(EvidenceError):
			rank([Figure("X", "arr", 1, 1), Figure("Y", "arr", 1, 1, unit="EUR_millions")])


class RepoTests(unittest.TestCase):
	def test_weak_repo_signals_and_fix_items_come_first(self):
		t = teardown_repo(fixture("weak_repo.json"), "acme/weak", now=NOW)
		signals = {c.signal for c in t.ledger.sourced if c.kind == "weakness"}
		self.assertTrue({"license_missing", "archived", "stale", "tests_missing", "ci_missing",
			"security_policy_missing", "lockfile_missing", "docs_thin", "releases_missing", "bus_factor_low"} <= signals)
		plan = t.plan()
		self.assertTrue(all(i["action"] == "fix" for i in plan))
		self.assertTrue(all(c.sourced for c in t.ledger.sourced))

	def test_strong_repo_reads_dot_github_and_workflows(self):
		t = teardown_repo(fixture("strong_repo.json"), "acme/strong", now=NOW)
		signals = {c.signal for c in t.ledger.sourced if c.kind == "strength"}
		self.assertTrue({"license_permissive", "active", "tests_present", "ci_present", "security_policy",
			"contributing", "lockfile", "docs", "releases", "contributors_broad"} <= signals)
		ci = next(c for c in t.ledger.sourced if c.signal == "ci_present")
		self.assertIn("1 GitHub Actions", ci.text)
		self.assertTrue(ci.evidence[0].url.endswith("/contents/.github/workflows"))

	def test_bad_repo_name_is_refused(self):
		with self.assertRaises(ValueError):
			teardown_repo(fixture("weak_repo.json"), "weak", now=NOW)


class CompanyTests(unittest.TestCase):
	def test_unsourced_claims_never_reach_the_plan(self):
		t = load_dossier({"entity": "Co", "claims": [
			{"kind": "weakness", "text": "slow onboarding", "sources": [{"url": "https://example.org/a"}]},
			{"kind": "weakness", "text": "rumour", "sources": []},
			{"kind": "strength", "text": "narrow agents", "sources": [{"url": "https://example.org/b"}], "countermeasure": "one agent per step"},
		]})
		becauses = [i["because"] for i in t.plan()]
		self.assertEqual(becauses, ["slow onboarding", "narrow agents"])
		self.assertTrue(t.plan()[0]["step"].startswith("TODO"))
		self.assertEqual(t.plan()[1]["step"], "one agent per step")
		self.assertEqual([c.text for c in t.ledger.unverified], ["rumour"])


class CliTests(unittest.TestCase):
	def run_cli(self, *args):
		buf = io.StringIO()
		with redirect_stdout(buf):
			code = main(list(args))
		return code, buf.getvalue()

	def test_rank_example_marks_everything_unverified(self):
		code, out = self.run_cli("rank", os.path.join(HERE, "..", "examples", "top-arr-2026-unverified.json"))
		self.assertEqual(code, 0)
		self.assertIn("13 of 13 figures have no source", out)
		self.assertIn("UNVERIFIED, DISPUTED", out)
		self.assertLess(out.index("Databricks"), out.index("Salesforce"))
		self.assertNotIn("| ok |", out)

	def test_company_and_repo_json(self):
		code, out = self.run_cli("company", os.path.join(HERE, "..", "examples", "dossier-template.json"), "--format", "json")
		self.assertEqual(code, 0)
		self.assertEqual(len(json.loads(out)["ledger"]["unverified"]), 1)
		code, out = self.run_cli("repo", "acme/strong", "--fixture", os.path.join(HERE, "fixtures", "strong_repo.json"), "--format", "json")
		self.assertEqual(code, 0)
		self.assertEqual(json.loads(out)["repo"], "acme/strong")

	def test_missing_repo_exits_2_and_out_file_is_written(self):
		self.assertEqual(self.run_cli("repo", "acme/none", "--fixture", os.path.join(HERE, "fixtures", "weak_repo.json"))[0], 2)
		with tempfile.TemporaryDirectory() as d:
			path = os.path.join(d, "r.md")
			code, _ = self.run_cli("repo", "acme/weak", "--fixture", os.path.join(HERE, "fixtures", "weak_repo.json"), "--out", path)
			self.assertEqual(code, 0)
			with open(path, encoding="utf-8") as fh:
				self.assertIn("# Teardown: acme/weak", fh.read())


if __name__ == "__main__":
	unittest.main()
