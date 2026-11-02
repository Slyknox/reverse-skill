import base64
import builtins
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from pdf_helper import make_pdf  # noqa: E402
from rea.company import load_dossier  # noqa: E402
from rea.deps import Dependency, FixtureRegistry, exact_version, parse_manifest, resolve_license  # noqa: E402
from rea.diff import diff, diff_md, similarity, suggest_matches  # noqa: E402
from rea.render import to_json  # noqa: E402
from rea.verify import FetchError, FixtureFetcher, check_source, contains_quote, pdf_to_text  # noqa: E402

try:
	import pypdf  # noqa: F401
	HAVE_PYPDF = True
except ImportError:
	HAVE_PYPDF = False

DD = "https://api.deps.dev/v3/systems"


class EcosystemParserTests(unittest.TestCase):
	def test_pom_resolves_properties_skips_dependency_management_and_marks_test_scope(self):
		pom = """<project xmlns="http://maven.apache.org/POM/4.0.0"><properties><g.version>33.0.0-jre</g.version></properties>
		<dependencyManagement><dependencies><dependency><groupId>x</groupId><artifactId>pinned</artifactId><version>1</version></dependency></dependencies></dependencyManagement>
		<dependencies>
		<dependency><groupId>com.google.guava</groupId><artifactId>guava</artifactId><version>${g.version}</version></dependency>
		<dependency><groupId>junit</groupId><artifactId>junit</artifactId><version>4.13.2</version><scope>test</scope></dependency>
		<dependency><groupId>org.slf4j</groupId><artifactId>slf4j-api</artifactId><version>${unknown}</version></dependency>
		</dependencies></project>"""
		deps = parse_manifest("pom.xml", pom)
		self.assertEqual([(d.name, d.spec, d.scope) for d in deps], [
			("com.google.guava:guava", "33.0.0-jre", "runtime"), ("junit:junit", "4.13.2", "dev"), ("org.slf4j:slf4j-api", "${unknown}", "runtime")])
		self.assertEqual([exact_version(d) for d in deps], ["33.0.0-jre", "4.13.2", None])

	def test_csproj_and_gemfile(self):
		cs = parse_manifest("src/App.csproj", '<Project><ItemGroup><PackageReference Include="Newtonsoft.Json" Version="13.0.3" />'
			'<PackageReference Include="Serilog"><Version>[3.1,4.0)</Version></PackageReference></ItemGroup></Project>')
		self.assertEqual([(d.name, d.spec, d.ecosystem) for d in cs], [("Newtonsoft.Json", "13.0.3", "nuget"), ("Serilog", "[3.1,4.0)", "nuget")])
		gem = parse_manifest("Gemfile", "source 'https://rubygems.org'\ngem 'rails', '~> 7.1'\n"
			"group :development, :test do\n  gem 'rspec' # tests\nend\ngroup :production do\n  gem 'puma'\nend\n"
			"gem 'rubocop', group: :development\ngem 'mine', path: '../mine'\n")
		self.assertEqual([(d.name, d.scope) for d in gem],
			[("rails", "runtime"), ("rspec", "dev"), ("puma", "runtime"), ("rubocop", "dev"), ("mine", "local")])

	def test_local_path_dependencies_are_marked_local(self):
		cargo = parse_manifest("Cargo.toml", '[dependencies]\ncore = { path = "../core" }\nserde = { workspace = true }\n')
		self.assertEqual([(d.name, d.scope) for d in cargo], [("core", "local"), ("serde", "runtime")])
		npm = parse_manifest("package.json", json.dumps({"dependencies": {"ui": "workspace:*", "lib": "file:../lib", "react": "^18"}}))
		self.assertEqual([(d.name, d.scope) for d in npm], [("ui", "local"), ("lib", "local"), ("react", "runtime")])


class DepsDevLookupTests(unittest.TestCase):
	def test_exact_version_is_one_lookup(self):
		dep = Dependency("github.com/spf13/cobra", "v1.8.0", "go", "runtime", "go.mod")
		url = f"{DD}/go/packages/github.com%2Fspf13%2Fcobra/versions/v1.8.0"
		lic, src, found, note, used = resolve_license(dep, FixtureRegistry({url: {"licenses": ["Apache-2.0"]}}), 5)
		self.assertEqual((lic, src, found, note, used), ("Apache-2.0", url, True, None, 1))

	def test_range_falls_back_to_default_version_and_says_so(self):
		dep = Dependency("Serilog", "[3.1,4.0)", "nuget", "runtime", "App.csproj")
		reg = FixtureRegistry({
			f"{DD}/nuget/packages/Serilog": {"versions": [{"versionKey": {"version": "3.0.0"}}, {"versionKey": {"version": "4.4.0"}, "isDefault": True}]},
			f"{DD}/nuget/packages/Serilog/versions/4.4.0": {"licenses": ["Apache-2.0"]},
		})
		lic, src, found, note, used = resolve_license(dep, reg, 5)
		self.assertEqual((lic, used), ("Apache-2.0", 2))
		self.assertIn("default version 4.4.0", note)
		self.assertEqual(resolve_license(dep, reg, 1)[1], None)  # not enough budget for two calls: skipped, not half-done

	def test_multiple_licenses_are_all_applied(self):
		dep = Dependency("example.com/mixed", "v1.0.0", "go", "runtime", "go.mod")
		reg = FixtureRegistry({f"{DD}/go/packages/example.com%2Fmixed/versions/v1.0.0": {"licenses": ["MIT", "GPL-3.0"]}})
		from rea.deps import classify
		self.assertEqual(classify(resolve_license(dep, reg, 5)[0]), "copyleft")

	def test_rubygems_range_names_the_release_it_checked(self):
		dep = Dependency("rails", "~> 7.1", "rubygems", "runtime", "Gemfile")
		reg = FixtureRegistry({"https://rubygems.org/api/v1/gems/rails.json": {"licenses": ["MIT"], "version": "8.1.4"}})
		lic, _, _, note, _ = resolve_license(dep, reg, 5)
		self.assertEqual(lic, "MIT")
		self.assertIn("latest release 8.1.4", note)


@unittest.skipUnless(HAVE_PYPDF, "pypdf not installed")
class PdfTests(unittest.TestCase):
	PDF = make_pdf(["Annual recurring revenue reached $150 mil-", "lion in fiscal 2026.", "Our cloud-", "based platform."])

	def test_quotes_match_across_line_end_hyphens_in_both_readings(self):
		text = pdf_to_text(self.PDF)
		self.assertTrue(contains_quote(text, "reached $150 million in fiscal 2026"))
		self.assertTrue(contains_quote(text, "Our cloud-based platform."))
		self.assertFalse(contains_quote(text, "reached $900 million"))

	def test_fixture_pdf_through_check_source(self):
		f = FixtureFetcher({"https://co.example/report.pdf": {"pdf_base64": base64.b64encode(self.PDF).decode()}})
		self.assertEqual(check_source(f, "https://co.example/report.pdf", "lion in fiscal 2026").status, "verified")

	def test_not_a_pdf_is_refused(self):
		with self.assertRaises(FetchError) as cm:
			pdf_to_text(b"<html>not a pdf</html>")
		self.assertEqual(cm.exception.status, "fetch_failed")


class PdfWithoutReaderTests(unittest.TestCase):
	def test_missing_pypdf_is_reported_not_guessed(self):
		real_import = builtins.__import__

		def no_pypdf(name, *args, **kwargs):
			if name == "pypdf" or name.startswith("pypdf."):
				raise ImportError(name)
			return real_import(name, *args, **kwargs)

		builtins.__import__ = no_pypdf
		try:
			with self.assertRaises(FetchError) as cm:
				pdf_to_text(make_pdf(["hello"]))
		finally:
			builtins.__import__ = real_import
		self.assertEqual(cm.exception.status, "unsupported_type")
		self.assertIn("pip install pypdf", cm.exception.reason)


class SuggestedMatchTests(unittest.TestCase):
	def test_similarity_ignores_stopwords_and_plurals(self):
		self.assertEqual(similarity("The customers love it", "customer love"), 1.0)
		self.assertEqual(similarity("", "anything"), 0.0)

	def test_pairs_are_one_to_one_and_cross_kind_pairs_mean_gap_or_lead(self):
		ours = [{"kind": "weakness", "text": "Customers report long onboarding"},
			{"kind": "strength", "text": "Usage-based pricing per resolved task"},
			{"kind": "fact", "text": "Founded in 2019 in Austin"}]
		theirs = [{"kind": "strength", "text": "Fast self-serve onboarding for customers"},
			{"kind": "strength", "text": "Pricing per resolved task"},
			{"kind": "strength", "text": "Pricing per resolved task, billed monthly"},
			{"kind": "fact", "text": "Headquartered in Boston since 2015"}]
		matches = suggest_matches(ours, theirs)
		self.assertEqual([(m["meaning"], m["ours"]["text"], m["theirs"]["text"]) for m in matches], [
			("both have", "Usage-based pricing per resolved task", "Pricing per resolved task"),
			("possible gap", "Customers report long onboarding", "Fast self-serve onboarding for customers")])

	def test_company_diff_shows_suggestions_from_sourced_claims_only(self):
		src = [{"url": "https://a.example/"}]
		ours = json.loads(to_json(load_dossier({"entity": "Us", "claims": [
			{"kind": "weakness", "text": "Customers report long onboarding", "sources": src},
			{"kind": "weakness", "text": "Customers report slow onboarding support", "sources": []}]})))
		theirs = json.loads(to_json(load_dossier({"entity": "Them", "claims": [
			{"kind": "strength", "text": "Fast self-serve onboarding for customers", "sources": src}]})))
		result = diff(ours, theirs)
		self.assertEqual(len(result["suggested_matches"]), 1)
		md = diff_md(result)
		self.assertEqual(result["suggested_matches"][0]["meaning"], "possible gap")
		self.assertEqual(result["suggested_matches"][0]["opposite_on"], ["speed"])  # long versus fast
		self.assertIn("| possible gap |", md)
		self.assertIn("Suggestions only", md)


if __name__ == "__main__":
	unittest.main()
