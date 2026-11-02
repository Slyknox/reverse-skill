import base64
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from rea.cli import main  # noqa: E402
from rea.company import load_dossier  # noqa: E402
from rea.deps import Dependency, FixtureRegistry, effective_version, teardown_deps  # noqa: E402
from rea.diff import diff  # noqa: E402
from rea.github import FixtureClient  # noqa: E402
from rea.osv import MAX_QUERIES, QUERY, QUERYBATCH, check, claims, cvss3_base_score, cvss_rating  # noqa: E402
from rea.render import to_json  # noqa: E402
from rea.report import kind_of, render  # noqa: E402
from rea.repo_teardown import teardown_repo  # noqa: E402
from rea.tree import Tree  # noqa: E402

FX = os.path.join(HERE, "fixtures")


def dep(name, spec, eco, locked=None, scope="runtime"):
	d = Dependency(name, spec, eco, scope, "m")
	d.locked = locked
	return d


class OsvTests(unittest.TestCase):
	def registry(self, results, details=None, record=None):
		def post(body):
			if record is not None:
				record.append(body)
			return {"results": results(body["queries"]) if callable(results) else results}
		data = {"POST " + QUERYBATCH: post}
		if details:
			data["POST " + QUERY] = lambda body: {"vulns": [{"id": vid, **d} for vid, d in details.items()]}
		return FixtureRegistry(data)

	def test_only_exact_or_locked_versions_are_queried_and_findings_cite_osv(self):
		sent = []
		reg = self.registry(lambda qs: [{"vulns": [{"id": "GHSA-1"}, {"id": "GHSA-2"}]} if q["package"]["name"] == "lodash" else {} for q in qs],
			{"GHSA-1": {"summary": "Prototype pollution", "database_specific": {"severity": "HIGH"}, "aliases": ["CVE-2020-8203"]},
				"GHSA-2": {"details": "## ReDoS\n\nLong explanation.", "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:H"}]}}, sent)
		deps = [dep("lodash", "^4.17", "npm", locked="4.17.15"), dep("express", "^4", "npm"), dep("requests", "==2.31.0", "pypi"),
			dep("mine", "path = ../mine", "crates", scope="local"), dep("x/y", "=1.0.0", "swift")]
		deps[-1].url = "https://github.com/x/y.git"
		result = check(deps, reg, effective_version)
		self.assertEqual([(q["package"], q["version"]) for q in sent[0]["queries"]], [
			({"name": "lodash", "ecosystem": "npm"}, "4.17.15"), ({"name": "requests", "ecosystem": "PyPI"}, "2.31.0"),
			({"name": "github.com/x/y", "ecosystem": "SwiftURL"}, "1.0.0")])
		self.assertEqual((result.checked, result.unchecked, result.failed), (3, 1, False))
		self.assertEqual([(f.name, [a.id for a in f.advisories]) for f in result.findings], [("lodash", ["GHSA-1", "GHSA-2"])])
		self.assertEqual(result.findings[0].advisories[0].severity, "High")
		self.assertEqual(result.findings[0].advisories[1].severity, "High (CVSS 3 base score 7.5)")
		self.assertEqual(result.findings[0].advisories[1].summary, "ReDoS")  # from details when summary is absent
		out = claims(result, "https://api.github.com/repos/a/b")
		weakness = next(c for c in out if c.kind == "weakness")
		self.assertIn("GHSA-1 (High): Prototype pollution", weakness.text)
		self.assertEqual([e.url for e in weakness.evidence], ["https://osv.dev/vulnerability/GHSA-1", "https://osv.dev/vulnerability/GHSA-2"])
		self.assertEqual(weakness.signal, "deps_vulnerable")
		self.assertIn("3 dependencies with an exact or locked version were checked", next(c for c in out if c.kind == "fact").text)

	def test_cvss3_scores_match_the_specification_examples(self):
		for vector, want in (("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H", 9.8), ("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:L", 5.3),
				("CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:U/C:H/I:H/A:N", 7.4), ("CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N", 6.1),
				("CVSS:3.0/AV:L/AC:L/PR:L/UI:N/S:U/C:N/I:N/A:N", 0.0), ("CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:C/C:H/I:H/A:H", 9.9)):
			self.assertEqual(cvss3_base_score(vector), want, vector)
		self.assertIsNone(cvss3_base_score("CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N"))
		self.assertEqual([cvss_rating(x) for x in (0, 3.9, 4.0, 6.9, 7.0, 8.9, 9.0)], ["None", "Low", "Medium", "Medium", "High", "High", "Critical"])

	def test_osv_failure_is_reported_as_not_checked(self):
		result = check([dep("lodash", "4.17.15", "npm")], FixtureRegistry({}), effective_version)
		self.assertTrue(result.failed)
		self.assertEqual(result.checked, 0)
		text = claims(result, "https://api.github.com/repos/a/b")[0].text
		self.assertIn("could not be reached", text)
		self.assertNotIn("none has a known vulnerability", text)

	def test_query_cap(self):
		deps = [dep(f"p{i}", "1.0.0", "npm") for i in range(MAX_QUERIES + 5)]
		result = check(deps, self.registry(lambda qs: [{} for _ in qs]), effective_version)
		self.assertEqual((result.checked, result.skipped_cap), (MAX_QUERIES, 5))

	def test_teardown_adds_osv_claims_and_a_plan_item(self):
		files = {"package.json": json.dumps({"dependencies": {"lodash": "4.17.15"}})}
		client = FixtureClient({f"/repos/a/b/contents/{p}": {"data": {"content": base64.b64encode(t.encode()).decode()}} for p, t in files.items()})
		reg = self.registry([{"vulns": [{"id": "GHSA-1"}]}], {"GHSA-1": {"summary": "bad"}})
		reg.responses["https://registry.npmjs.org/lodash/4.17.15"] = {"license": "MIT"}
		t = teardown_deps(client, "a/b", Tree("https://x", list(files)), set(), reg)
		self.assertEqual([f.name for f in t.osv.findings], ["lodash"])
		self.assertTrue(any(c.signal == "deps_vulnerable" for c in t.ledger.sourced))
		off = teardown_deps(client, "a/b", Tree("https://x", list(files)), set(), reg, osv=False)
		self.assertIsNone(off.osv)
		from rea.repo_teardown import PLAN
		self.assertEqual(PLAN["deps_vulnerable"][0], "fix")


class ReportTests(unittest.TestCase):
	def load(self, name):
		with open(os.path.join(FX, name), encoding="utf-8") as fh:
			return json.load(fh)

	def test_each_kind_renders_self_contained_html(self):
		from datetime import datetime, timezone
		now = datetime(2026, 11, 2, tzinfo=timezone.utc)
		repo = json.loads(to_json(teardown_repo(FixtureClient(self.load("strong_repo.json")), "acme/strong", now=now)))
		weak = json.loads(to_json(teardown_repo(FixtureClient(self.load("weak_repo.json")), "acme/weak", now=now)))
		company = json.loads(to_json(load_dossier({"entity": "Acme <b>&</b>", "claims": [
			{"kind": "strength", "text": "<script>alert(1)</script>", "sources": [{"url": "https://a.example/\"onclick=x"}]},
			{"kind": "weakness", "text": "slow", "sources": []}], "figures": [{"kind": "arr", "low": 150, "high": 150, "sources": []}]})))
		d = diff(weak, repo)
		d["rows"] = [{**r.__dict__, "verdict": r.verdict} for r in d["rows"]]
		for data, kind, must in ((repo, "repo", "Original build plan"), (company, "company", "Unverified"), (d, "diff", "Check by check")):
			self.assertEqual(kind_of(data), kind)
			page = render(data)
			self.assertTrue(page.startswith("<!doctype html>"))
			self.assertIn(must, page)
			self.assertNotIn("<script>alert", page)  # escaped
			self.assertNotIn('href="https://a.example/"onclick', page)
			self.assertNotIn("http://", page.replace("http://www.w3.org", ""))  # nothing fetched from outside
			self.assertNotIn("<link ", page)
			self.assertEqual(page.count("<script>"), 1)  # only the inline sorter
		self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", render(company))
		self.assertIn("Acme &lt;b&gt;&amp;&lt;/b&gt;", render(company))

	def test_deps_kind_and_cli_paths(self):
		import io
		import tempfile
		from contextlib import redirect_stdout
		with tempfile.TemporaryDirectory() as tmp:
			out_json = os.path.join(tmp, "d.json")
			code = main(["deps", "acme/strong", "--fixture", f"{FX}/strong_repo.json", "--registry-fixture", f"{FX}/strong_registry.json", "--no-osv", "--format", "json", "--out", out_json])
			self.assertEqual(code, 0)
			data = json.load(open(out_json, encoding="utf-8"))
			self.assertEqual(kind_of(data), "deps")
			self.assertNotIn("Known vulnerabilities", render(data))
			buf = io.StringIO()
			with redirect_stdout(buf):
				self.assertEqual(main(["report", out_json]), 0)
			self.assertIn("<h2>Dependency list</h2>", buf.getvalue())
			buf = io.StringIO()
			with redirect_stdout(buf):
				self.assertEqual(main(["repo", "acme/strong", "--fixture", f"{FX}/strong_repo.json", "--format", "html"]), 0)
			self.assertIn("<title>Teardown: acme/strong</title>", buf.getvalue())
		with self.assertRaises(ValueError):
			kind_of({"x": 1})


if __name__ == "__main__":
	unittest.main()
