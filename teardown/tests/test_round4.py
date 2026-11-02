import base64
import json
import os
import shutil
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from pdf_helper import make_scanned_pdf  # noqa: E402
from rea.deps import FixtureRegistry, parse_manifest, teardown_deps  # noqa: E402
from rea.diff import opposed, similarity, suggest_matches  # noqa: E402
from rea.github import FixtureClient  # noqa: E402
from rea.maven import Resolver, central_url  # noqa: E402
from rea.tree import Tree  # noqa: E402
from rea.verify import FetchError, FixtureFetcher, check_source, pdf_to_text  # noqa: E402

try:
	import PIL  # noqa: F401
	import pypdf  # noqa: F401
	HAVE_OCR = shutil.which("tesseract") is not None
except ImportError:
	HAVE_OCR = False


def pom(body: str, parent: str = "") -> str:
	return f"<project xmlns='http://maven.apache.org/POM/4.0.0'>{parent}{body}</project>"


PARENT = central_url("org.acme", "acme-parent", "1.0")
BOM = central_url("com.widget", "widget-bom", "2.0")
CENTRAL = {
	PARENT: pom("""<groupId>org.acme</groupId><artifactId>acme-parent</artifactId><version>1.0</version>
		<properties><guava.version>32.0.0-jre</guava.version><widget.version>2.0</widget.version></properties>
		<dependencyManagement><dependencies>
		<dependency><groupId>com.google.guava</groupId><artifactId>guava</artifactId><version>${guava.version}</version></dependency>
		<dependency><groupId>com.widget</groupId><artifactId>widget-bom</artifactId><version>${widget.version}</version><type>pom</type><scope>import</scope></dependency>
		<dependency><groupId>org.unrelated</groupId><artifactId>unrelated-bom</artifactId><version>9</version><type>pom</type><scope>import</scope></dependency>
		</dependencies></dependencyManagement>"""),
	BOM: pom("""<groupId>com.widget</groupId><artifactId>widget-bom</artifactId><version>2.0</version>
		<dependencyManagement><dependencies>
		<dependency><groupId>com.widget</groupId><artifactId>widget-core</artifactId><version>2.0.3</version></dependency>
		</dependencies></dependencyManagement>"""),
}
CHILD = pom("""<artifactId>app</artifactId><properties><guava.version>33.0.0-jre</guava.version></properties><dependencies>
	<dependency><groupId>com.google.guava</groupId><artifactId>guava</artifactId></dependency>
	<dependency><groupId>com.widget</groupId><artifactId>widget-core</artifactId></dependency>
	<dependency><groupId>org.nowhere</groupId><artifactId>orphan</artifactId></dependency>
	</dependencies>""", "<parent><groupId>org.acme</groupId><artifactId>acme-parent</artifactId><version>1.0</version><relativePath/></parent>")


class MavenTests(unittest.TestCase):
	def resolve(self, repo_files=None, central=CENTRAL, path="pom.xml", text=CHILD, **kw):
		fetched = []

		def fetch(url):
			fetched.append(url)
			return central.get(url)

		r = Resolver(lambda p: (repo_files or {}).get(p), fetch, **kw)
		return {d.name: d for d in parse_manifest(path, text, r)}, r, fetched

	def test_parent_management_uses_the_childs_property_override(self):
		deps, _, _ = self.resolve()
		self.assertEqual(deps["com.google.guava:guava"].spec, "33.0.0-jre")  # child's property, parent's management
		self.assertIn(PARENT, deps["com.google.guava:guava"].version_from)

	def test_imported_bom_supplies_versions_and_unrelated_boms_are_not_fetched_first(self):
		deps, r, fetched = self.resolve()
		self.assertEqual(deps["com.widget:widget-core"].spec, "2.0.3")
		self.assertIn(BOM, deps["com.widget:widget-core"].version_from)
		self.assertEqual(fetched[:2], [PARENT, BOM])
		self.assertEqual(deps["org.nowhere:orphan"].spec, "*")
		self.assertEqual(deps["org.nowhere:orphan"].version_from, "no version declared or managed")

	def test_in_repo_parent_is_read_from_relative_path_before_central(self):
		parent = pom("""<groupId>org.acme</groupId><artifactId>acme-parent</artifactId><version>1.0</version>
			<dependencyManagement><dependencies><dependency><groupId>com.google.guava</groupId><artifactId>guava</artifactId>
			<version>31.1-jre</version></dependency></dependencies></dependencyManagement>""")
		child = CHILD.replace("<relativePath/>", "")  # default ../pom.xml
		deps, _, fetched = self.resolve({"pom.xml": parent}, path="app/pom.xml", text=child)
		self.assertEqual(deps["com.google.guava:guava"].spec, "31.1-jre")
		self.assertEqual(deps["com.google.guava:guava"].version_from, "managed in pom.xml")
		self.assertEqual(fetched, [])

	def test_fetch_cap_is_reported_not_guessed(self):
		deps, r, _ = self.resolve(max_fetches=1)
		self.assertEqual(deps["com.widget:widget-core"].spec, "*")
		self.assertTrue(any("fetch cap" in p for p in r.summary()))

	def test_unreadable_parent_is_a_problem_and_versions_stay_unresolved(self):
		deps, r, _ = self.resolve(central={})
		self.assertEqual(deps["com.google.guava:guava"].spec, "*")
		self.assertTrue(any("could not be read" in p for p in r.summary()))

	def test_deps_teardown_cites_maven_central_and_reports_problems(self):
		client = FixtureClient({"/repos/acme/app/contents/pom.xml": {"data": {"content": base64.b64encode(CHILD.encode()).decode()}}})
		registry = FixtureRegistry(dict(CENTRAL))
		t = teardown_deps(client, "acme/app", Tree("https://x", ["pom.xml"]), set(), registry, max_lookups=0)
		facts = [c for c in t.ledger.sourced if c.kind == "fact"]
		central = next(c for c in facts if c.text.startswith("Maven versions were resolved"))
		self.assertEqual([e.url for e in central.evidence][:2], [PARENT, BOM])
		unresolved = next(c for c in facts if c.text.startswith("Maven versions left unresolved"))
		self.assertIn("org.unrelated:unrelated-bom:9 could not be read", unresolved.text)


@unittest.skipUnless(HAVE_OCR, "needs pypdf, Pillow and the tesseract command")
class OcrTests(unittest.TestCase):
	def test_scanned_pdf_is_ocred_and_labelled(self):
		pdf = make_scanned_pdf(["Annual recurring revenue reached", "$150 million in fiscal 2026."])
		f = FixtureFetcher({"https://x.example/s.pdf": {"pdf_base64": base64.b64encode(pdf).decode()}})
		hit = check_source(f, "https://x.example/s.pdf", "Annual recurring revenue reached $150 million")
		self.assertEqual(hit.status, "verified")
		self.assertIn("OCR", hit.reason)
		miss = check_source(f, "https://x.example/s.pdf", "revenue reached $900 million")
		self.assertEqual(miss.status, "quote_not_found")
		self.assertIn("check by hand", miss.reason)


class OcrUnavailableTests(unittest.TestCase):
	@unittest.skipUnless(HAVE_OCR, "needs pypdf and Pillow to build a scanned PDF")
	def test_without_tesseract_a_scanned_pdf_is_reported(self):
		import rea.verify as verify
		real = shutil.which
		verify_shutil = __import__("shutil")
		verify_shutil.which = lambda name, *a, **k: None if name == "tesseract" else real(name, *a, **k)
		try:
			with self.assertRaises(FetchError) as cm:
				pdf_to_text(make_scanned_pdf(["hello world"]))
		finally:
			verify_shutil.which = real
		self.assertEqual(cm.exception.status, "unsupported_type")
		self.assertIn("install tesseract", cm.exception.reason)


class ConceptMatchTests(unittest.TestCase):
	def test_synonyms_and_opposites_share_a_concept(self):
		self.assertGreaterEqual(similarity("Slow onboarding for new customers", "Customers get set up in minutes"), 0.4)
		self.assertEqual(opposed("Slow onboarding for new customers", "Customers get set up in minutes"), ["speed"])
		self.assertEqual(opposed("Expensive per-seat pricing", "Affordable usage-based pricing"), ["cost_level"])
		self.assertEqual(opposed("Frequent outages", "99.99% uptime"), ["reliability"])
		self.assertEqual(opposed("Fast setup", "Quick onboarding"), [])

	def test_unrelated_claims_still_do_not_match(self):
		self.assertEqual(similarity("Founded in 2019 in Austin", "Headquartered in Boston since 2015"), 0.0)

	def test_gap_found_through_synonyms(self):
		ours = [{"kind": "weakness", "text": "Implementation takes months"}]
		theirs = [{"kind": "strength", "text": "Setup done in minutes"}, {"kind": "strength", "text": "SOC 2 compliant"}]
		m = suggest_matches(ours, theirs)
		self.assertEqual([(x["meaning"], x["theirs"]["text"], x["opposite_on"]) for x in m],
			[("possible gap", "Setup done in minutes", ["speed"])])


if __name__ == "__main__":
	unittest.main()
