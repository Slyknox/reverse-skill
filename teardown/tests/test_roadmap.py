import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from rea.cli import main  # noqa: E402
from rea.company import load_dossier  # noqa: E402
from rea.deps import FixtureRegistry, classify, parse_manifest, teardown_deps  # noqa: E402
from rea.diff import diff  # noqa: E402
from rea.draft import MAX_TOTAL_CHARS, DraftError, draft_dossier  # noqa: E402
from rea.github import FixtureClient  # noqa: E402
from rea.repo_teardown import teardown_repo  # noqa: E402
from rea.render import to_json  # noqa: E402
from rea.tree import Tree  # noqa: E402
from rea.verify import FixtureFetcher, check_source, contains_quote, html_to_text  # noqa: E402

NOW = datetime(2026, 11, 2, tzinfo=timezone.utc)


def load(name):
	with open(os.path.join(HERE, "fixtures", name), encoding="utf-8") as fh:
		return json.load(fh)


class TreeTests(unittest.TestCase):
	def test_nested_tests_and_lockfiles_found_and_vendored_folders_skipped(self):
		t = teardown_repo(FixtureClient(load("strong_repo.json")), "acme/strong", now=NOW)
		tests = next(c for c in t.ledger.sourced if c.signal == "tests_present")
		self.assertIn("packages/api/src", tests.text)
		self.assertNotIn("node_modules", tests.text)
		lock = next(c for c in t.ledger.sourced if c.signal == "lockfile")
		self.assertIn("packages/api/pnpm-lock.yaml", lock.text)
		self.assertTrue(lock.evidence[0].url.endswith("git/trees/main?recursive=1"))

	def test_test_file_patterns(self):
		tree = Tree("https://x", ["src/a_test.go", "lib/test_b.py", "web/c.spec.tsx", "app/Main.java", "e2e/run.sh"])
		self.assertEqual(tree.test_locations(), ["src", "lib", "web", "e2e"])

	def test_truncated_listing_is_disclosed(self):
		fx = load("strong_repo.json")
		fx["/repos/acme/strong/git/trees/main?recursive=1"] = {"data": {"truncated": True, "tree": [{"path": "a.py", "type": "blob"}]}}
		t = teardown_repo(FixtureClient(fx), "acme/strong", now=NOW)
		missing = next(c for c in t.ledger.sourced if c.signal == "tests_missing")
		self.assertIn("truncated", missing.text)


class DepsTests(unittest.TestCase):
	def test_classify(self):
		cases = {"MIT": "permissive", "Apache 2.0": "permissive", "GPL-3.0-or-later": "copyleft", "LGPL-3.0": "weak_copyleft",
			"MIT OR GPL-3.0": "permissive", "GPL-2.0 AND MIT": "copyleft", "Proprietary": "unknown", None: "unknown", "MIT-CMU": "permissive"}
		for expr, want in cases.items():
			self.assertEqual(classify(expr), want, expr)

	def test_parsers(self):
		py = parse_manifest("pyproject.toml", '[project]\ndependencies = ["httpx>=0.27", "pydantic[email]==2.7 ; python_version>\'3.10\'"]\n'
			'[project.optional-dependencies]\nviz = ["matplotlib"]\n[dependency-groups]\ndev = ["pytest"]\n')
		self.assertEqual([(d.name, d.scope) for d in py], [("httpx", "runtime"), ("pydantic", "runtime"), ("matplotlib", "optional"), ("pytest", "dev")])
		self.assertEqual(py[1].spec, "==2.7")
		cargo = parse_manifest("Cargo.toml", '[dependencies]\nserde = { version = "1", features = ["derive"] }\ntokio = "1.38"\n[dev-dependencies]\ninsta = "1"\n')
		self.assertEqual([(d.name, d.spec, d.scope) for d in cargo], [("serde", "1", "runtime"), ("tokio", "1.38", "runtime"), ("insta", "1", "dev")])
		go = parse_manifest("go.mod", "module x\nrequire github.com/a/b v1.2.0\nrequire (\n\tgithub.com/c/d v0.1.0 // indirect\n\tgolang.org/x/e v0.3.0\n)\n")
		self.assertEqual([(d.name, d.scope) for d in go], [("github.com/a/b", "runtime"), ("github.com/c/d", "indirect"), ("golang.org/x/e", "runtime")])
		req = parse_manifest("requirements-dev.txt", "# c\n-e .\nblack==24.1\ngit+https://x/y\n")
		self.assertEqual([(d.name, d.scope) for d in req], [("black", "dev")])

	def test_teardown_reads_nested_manifest_skips_vendored_and_cites_registry(self):
		client = FixtureClient(load("strong_repo.json"))
		t = teardown_repo(client, "acme/strong", now=NOW)
		d = teardown_deps(client, "acme/strong", t.tree, t.top_level, FixtureRegistry(load("strong_registry.json")))
		self.assertEqual(d.manifests, ["requirements.txt", "packages/api/package.json"])
		by = {x.name: x for x in d.dependencies}
		self.assertEqual(by["mystery-lib"].license, "MIT")  # from the PyPI classifier
		self.assertFalse(by["jest"].looked_up)  # dev dependency, not looked up by default
		signals = {c.signal for c in d.ledger.sourced}
		self.assertIn("deps_copyleft", signals)
		self.assertIn("deps_license_unknown", signals)  # "SEE LICENSE IN ..." is not an identified license
		self.assertIn("deps_not_on_registry", signals)
		unknown = next(c for c in d.ledger.sourced if c.signal == "deps_license_unknown")
		self.assertIn("left-pad (SEE LICENSE IN LICENSE.txt)", unknown.text)
		self.assertNotIn("private-pkg", unknown.text)
		self.assertEqual(unknown.text.count("mystery-lib"), 0)
		copyleft = next(c for c in d.ledger.sourced if c.signal == "deps_copyleft")
		self.assertEqual(copyleft.evidence[0].url, "https://registry.npmjs.org/gpl-thing/latest")

	def test_lookup_cap_counts_the_rest(self):
		client = FixtureClient(load("strong_repo.json"))
		t = teardown_repo(client, "acme/strong", now=NOW)
		d = teardown_deps(client, "acme/strong", t.tree, t.top_level, FixtureRegistry(load("strong_registry.json")), max_lookups=1)
		self.assertEqual(d.not_checked, 4)  # unique packages: mystery-lib is declared twice but counted once


class VerifyTests(unittest.TestCase):
	PAGE = "<html><script>var x='Revenue grew';</script><p>Revenue grew to <b>$1.2B</b>, up 30%.</p><p>“Customers love it.”</p></html>"

	def test_inline_tags_join_and_scripts_are_dropped(self):
		text = html_to_text(self.PAGE)
		self.assertIn("Revenue grew to $1.2B, up 30%.", text)
		self.assertNotIn("var x", text)
		self.assertTrue(contains_quote(text, 'revenue grew to $1.2B,  up 30%.'))
		self.assertTrue(contains_quote(text, '"Customers love it."'))
		self.assertFalse(contains_quote(text, "Revenue grew to $2B"))
		self.assertFalse(contains_quote(text, "   "))

	def test_statuses(self):
		f = FixtureFetcher({"https://a.example/p": self.PAGE, "https://a.example/pdf": {"error": "unsupported_type", "reason": "application/pdf"}})
		self.assertEqual(check_source(f, "https://a.example/p", "up 30%").status, "verified")
		self.assertEqual(check_source(f, "https://a.example/p", "down 30%").status, "quote_not_found")
		self.assertEqual(check_source(f, "https://a.example/p", None).status, "no_quote")
		self.assertEqual(check_source(f, "https://a.example/pdf", "x").status, "unsupported_type")
		self.assertEqual(check_source(f, "https://a.example/missing", "x").status, "fetch_failed")

	def test_live_fetcher_refuses_non_public_and_plain_http(self):
		from rea.verify import Fetcher
		for url in ("https://127.0.0.1/x", "https://10.0.0.5/x", "http://example.org/"):
			self.assertEqual(check_source(Fetcher(), url, "x").status, "refused_url", url)

	def test_company_verify_moves_unconfirmed_claims_to_unverified(self):
		data = {"entity": "Co", "claims": [
			{"kind": "strength", "text": "growing", "sources": [{"url": "https://a.example/p", "quote": "up 30%"}]},
			{"kind": "strength", "text": "invented", "sources": [{"url": "https://a.example/p", "quote": "up 300%"}]},
		]}
		t = load_dossier(data, FixtureFetcher({"https://a.example/p": self.PAGE}))
		self.assertEqual([c.text for c in t.ledger.sourced], ["growing"])
		self.assertEqual([c.text for c in t.ledger.unverified], ["invented"])
		self.assertEqual([c.status for c in t.checks], ["verified", "quote_not_found"])
		self.assertEqual(len(load_dossier(data).ledger.sourced), 2)  # without --verify, nothing is fetched


class DiffTests(unittest.TestCase):
	def test_repo_gaps_and_leads(self):
		weak = json.loads(to_json(teardown_repo(FixtureClient(load("weak_repo.json")), "acme/weak", now=NOW)))
		strong = json.loads(to_json(teardown_repo(FixtureClient(load("strong_repo.json")), "acme/strong", now=NOW)))
		rows = {r.label: r.verdict for r in diff(weak, strong)["rows"]}
		self.assertEqual(rows["Tests"], "gap")
		self.assertEqual(rows["CI workflows"], "gap")
		self.assertEqual({r.label: r.verdict for r in diff(strong, weak)["rows"]}["Tests"], "lead")

	def test_kind_mismatch_and_bad_input_are_refused(self):
		weak = json.loads(to_json(teardown_repo(FixtureClient(load("weak_repo.json")), "acme/weak", now=NOW)))
		company = json.loads(to_json(load_dossier({"entity": "Co", "claims": []})))
		with self.assertRaises(ValueError):
			diff(weak, company)
		with self.assertRaises(ValueError):
			diff({"x": 1}, weak)


class DraftTests(unittest.TestCase):
	PAGES = {"https://co.example/about": "Acme sells usage-based plans. Annual recurring revenue reached $150 million in 2026. Onboarding takes weeks."}

	def fake(self, answer):
		calls = []

		def complete(system, user, schema):
			calls.append(user)
			return answer, "claude-opus-5-5"
		return complete, calls

	def test_only_quotes_found_on_their_page_keep_a_source(self):
		answer = {"claims": [
			{"kind": "revenue_model", "text": "Usage-based pricing", "source_url": "https://co.example/about", "quote": "Acme sells usage-based plans."},
			{"kind": "weakness", "text": "Slow onboarding", "source_url": "https://co.example/about", "quote": "Onboarding takes months."},
			{"kind": "strength", "text": "From another page", "source_url": "https://other.example/", "quote": "Acme sells usage-based plans."},
		], "figures": [
			{"metric": "arr", "low_usd_millions": 150, "high_usd_millions": 150, "as_of": "2026", "source_url": "https://co.example/about",
				"quote": "Annual recurring revenue reached $150 million in 2026."},
			{"metric": "arr", "low_usd_millions": 200, "high_usd_millions": 100, "as_of": "", "source_url": "https://co.example/about",
				"quote": "Acme sells usage-based plans."},
		]}
		complete, calls = self.fake(answer)
		res = draft_dossier("Acme", list(self.PAGES), FixtureFetcher(self.PAGES), complete)
		self.assertEqual(res.kept, 2)
		self.assertEqual(sorted(r["reason"] for r in res.rejected),
			["quote does not state a number", "quote not found on the page", "source_url is not one of the pages provided"])
		self.assertIn('<page url="https://co.example/about">', calls[0])
		self.assertEqual(res.dossier["figures"][1]["low"], 100)  # inverted range is put in order, not trusted
		t = load_dossier(res.dossier)
		self.assertEqual([c.text for c in t.ledger.sourced], ["Usage-based pricing"])
		self.assertEqual(len(t.ledger.unverified), 2)
		self.assertTrue(t.figures[0].verified)
		self.assertFalse(t.figures[1].verified)

	def test_refuses_without_pages_and_never_truncates(self):
		complete, calls = self.fake({"claims": [], "figures": []})
		with self.assertRaises(DraftError):
			draft_dossier("Acme", ["https://nowhere.example/"], FixtureFetcher({}), complete)
		with self.assertRaises(DraftError):
			draft_dossier("Acme", ["https://big.example/"], FixtureFetcher({"https://big.example/": "x" * (MAX_TOTAL_CHARS + 1)}), complete)
		with self.assertRaises(DraftError):
			draft_dossier("Acme", [], FixtureFetcher({}), complete)
		self.assertEqual(calls, [])


class CliTests(unittest.TestCase):
	def run_cli(self, *args):
		out, err = io.StringIO(), io.StringIO()
		with redirect_stdout(out), redirect_stderr(err):
			code = main(list(args))
		return code, out.getvalue()

	def test_deps_and_verify_commands(self):
		fx = os.path.join(HERE, "fixtures")
		code, out = self.run_cli("deps", "acme/strong", "--fixture", f"{fx}/strong_repo.json", "--registry-fixture", f"{fx}/strong_registry.json")
		self.assertEqual(code, 0)
		self.assertIn("| gpl-thing | `2.0.0` |  | npm | runtime | packages/api/package.json |", out)
		code, out = self.run_cli("verify", os.path.join(HERE, "..", "examples", "dossier-template.json"), "--pages-fixture", f"{fx}/strong_registry.json")
		self.assertEqual(code, 3)
		self.assertIn("0 of", out)


if __name__ == "__main__":
	unittest.main()
