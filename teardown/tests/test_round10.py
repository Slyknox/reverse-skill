import base64
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from rea.deps import FixtureRegistry, teardown_deps  # noqa: E402
from rea.github import FixtureClient  # noqa: E402
from rea.lockfiles import parse_lockfile, parse_lockfile_full  # noqa: E402
from rea.osv import QUERYBATCH  # noqa: E402
from rea.tree import Tree  # noqa: E402


def b64(text: str) -> str:
	return base64.b64encode(text.encode()).decode()


LOCK = json.dumps({"packages": {
	"": {"dependencies": {"lodash": "^4.17"}},
	"node_modules/lodash": {"version": "4.17.21"},
	"node_modules/minimist": {"version": "1.2.5"},
	"node_modules/jest": {"version": "29.7.0", "dev": True},
	"node_modules/jest/node_modules/minimist": {"version": "0.0.8", "dev": True},
}})
FILES = {"package.json": json.dumps({"dependencies": {"lodash": "^4.17"}, "devDependencies": {"jest": "^29"}}), "package-lock.json": LOCK}


class LockfileDevFlagTests(unittest.TestCase):
	def test_dev_flags_where_the_format_states_them(self):
		full = parse_lockfile_full("package-lock.json", LOCK)
		self.assertEqual(full["minimist"], ("1.2.5", False))  # the top-level copy wins over the nested dev one
		self.assertEqual(full["jest"], ("29.7.0", True))
		self.assertEqual(parse_lockfile("package-lock.json", LOCK)["jest"], "29.7.0")
		composer = parse_lockfile_full("composer.lock", json.dumps({"packages": [{"name": "a/b", "version": "1.0.0"}], "packages-dev": [{"name": "c/d", "version": "2.0.0"}]}))
		self.assertEqual(composer, {"a/b": ("1.0.0", False), "c/d": ("2.0.0", True)})
		pub = parse_lockfile_full("pubspec.lock", 'packages:\n  http:\n    dependency: "direct main"\n    version: "1.2.1"\n  test:\n    dependency: "direct dev"\n    version: "1.24.0"\n  path:\n    dependency: transitive\n    version: "1.9.0"\n')
		self.assertEqual(pub, {"http": ("1.2.1", False), "test": ("1.24.0", True), "path": ("1.9.0", False)})


class TransitiveTests(unittest.TestCase):
	def run_deps(self, registry: dict, **kw):
		client = FixtureClient({f"/repos/a/b/contents/{p}": {"data": {"content": b64(t)}} for p, t in FILES.items()})
		return teardown_deps(client, "a/b", Tree("https://x", list(FILES)), set(), FixtureRegistry(registry), **kw)

	def test_lockfile_packages_become_transitive_dependencies(self):
		t = self.run_deps({}, osv=False, max_lookups=0)
		by = {(d.name, d.scope): d for d in t.dependencies}
		self.assertEqual(set(by), {("lodash", "runtime"), ("jest", "dev"), ("minimist", "transitive")})
		self.assertEqual(t.transitive, 1)
		self.assertEqual((by[("minimist", "transitive")].locked, by[("minimist", "transitive")].manifest, by[("minimist", "transitive")].version_from),
			("1.2.5", "package-lock.json", "resolved in package-lock.json"))
		self.assertEqual(by[("lodash", "runtime")].locked, "4.17.21")  # declared ones are pinned, not duplicated
		self.assertTrue(any("add 1 transitive packages" in c.text for c in t.ledger.sourced))

	def test_declared_dependencies_are_looked_up_before_transitive_ones(self):
		reg = {"https://registry.npmjs.org/lodash/4.17.21": {"license": "MIT"}, "https://registry.npmjs.org/minimist/1.2.5": {"license": "MIT"}}
		t = self.run_deps(reg, osv=False, max_lookups=1)
		by = {d.name: d for d in t.dependencies}
		self.assertTrue(by["lodash"].looked_up)
		self.assertFalse(by["minimist"].looked_up)
		self.assertEqual(t.not_checked, 1)
		t = self.run_deps(reg, osv=False, max_lookups=2)
		self.assertEqual({d.name for d in t.dependencies if d.looked_up}, {"lodash", "minimist"})

	def test_osv_covers_the_transitive_tree_and_names_the_lockfile(self):
		sent = []

		def post(body):
			sent.append([q["package"]["name"] for q in body["queries"]])
			return {"results": [{"vulns": [{"id": "GHSA-x"}]} if q["package"]["name"] == "minimist" else {} for q in body["queries"]]}
		t = self.run_deps({"POST " + QUERYBATCH: post}, max_lookups=0)
		self.assertEqual(sent, [["lodash", "minimist"]])  # jest is dev-only: not checked without --include-dev
		finding = t.osv.findings[0]
		self.assertEqual((finding.name, finding.scope, finding.via), ("minimist", "transitive", "package-lock.json"))
		weakness = next(c for c in t.ledger.sourced if c.signal == "deps_vulnerable")
		self.assertIn("minimist 1.2.5 (npm (transitive, resolved in package-lock.json))", weakness.text)
		t = self.run_deps({"POST " + QUERYBATCH: post}, max_lookups=0, include_dev=True)
		self.assertEqual(sorted(sent[-1]), ["jest", "lodash", "minimist"])

	def test_no_transitive_keeps_only_declared_dependencies(self):
		t = self.run_deps({}, osv=False, max_lookups=0, transitive=False)
		self.assertEqual({d.name for d in t.dependencies}, {"lodash", "jest"})
		self.assertEqual(t.transitive, 0)
		self.assertEqual(t.dependencies[0].locked, "4.17.21")


if __name__ == "__main__":
	unittest.main()


class LargeFileTests(unittest.TestCase):
	def test_files_over_the_contents_limit_are_read_through_the_blob_api(self):
		lock = LOCK
		client = FixtureClient({
			"/repos/a/b/contents/package.json": {"data": {"content": b64(FILES["package.json"])}},
			"/repos/a/b/contents/package-lock.json": {"data": {"content": "", "encoding": "none", "size": 2_000_000, "sha": "deadbeef"}},
			"/repos/a/b/git/blobs/deadbeef": {"data": {"content": b64(lock), "encoding": "base64"}},
		})
		from rea.tree import Tree as T
		t = teardown_deps(client, "a/b", T("https://x", list(FILES), shas={"package-lock.json": "deadbeef"}), set(), FixtureRegistry({}), osv=False, max_lookups=0)
		self.assertEqual(t.lockfiles, ["package-lock.json"])
		self.assertEqual({d.name for d in t.dependencies if d.scope == "transitive"}, {"minimist"})

	def test_osv_caps_allow_a_real_tree(self):
		from rea.osv import BATCH, MAX_QUERIES
		self.assertGreaterEqual(MAX_QUERIES, 2000)
		self.assertLessEqual(BATCH, 1000)  # OSV's per-call limit
