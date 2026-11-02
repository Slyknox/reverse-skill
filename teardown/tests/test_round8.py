import base64
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from rea.deps import Dependency, FixtureRegistry, exact_version, parse_manifest, resolve_license, teardown_deps  # noqa: E402
from rea.github import FixtureClient  # noqa: E402
from rea.lockfiles import parse_lockfile  # noqa: E402
from rea.manifests import composer_key, composer_pick, composer_satisfies, parse_composer, parse_mix, parse_package_swift, parse_pubspec  # noqa: E402
from rea.tree import Tree  # noqa: E402


def b64(text: str) -> str:
	return base64.b64encode(text.encode()).decode()


class ParserTests(unittest.TestCase):
	def test_composer_skips_platform_requirements(self):
		rows = parse_composer('{"require":{"php":">=8.1","ext-json":"*","monolog/monolog":"^3.0"},"require-dev":{"phpunit/phpunit":"^10"}}')
		self.assertEqual([(n, s, sc) for n, s, sc, _ in rows], [("monolog/monolog", "^3.0", "runtime"), ("phpunit/phpunit", "^10", "dev")])

	def test_package_swift_requirements_and_identities(self):
		rows = parse_package_swift('''dependencies: [
			.package(url: "https://github.com/apple/swift-argument-parser.git", from: "1.2.0"),
			.package(url: "https://github.com/A/B", "5.0.0"..<"6.0.0"),
			.package(url: "git@github.com:x/y.git", exact: "2.1.0"),
			.package(url: "https://gitlab.com/g/h.git", branch: "main"),
			.package(path: "../Local"),
		]''')
		self.assertEqual([(n, s, sc) for n, s, sc, _ in rows], [("apple/swift-argument-parser", "from 1.2.0", "runtime"),
			("A/B", "[5.0.0,6.0.0)", "runtime"), ("x/y", "=2.1.0", "runtime"), ("h", "branch main", "runtime"), ("Local", "path = ../Local", "local")])
		self.assertEqual(exact_version(Dependency("x/y", "=2.1.0", "swift", "runtime", "Package.swift")), "2.1.0")

	def test_pubspec_scopes(self):
		rows = parse_pubspec("name: app\ndependencies:\n  flutter:\n    sdk: flutter\n  http: ^1.1.0 # client\n  mine:\n    path: ../mine\n"
			"  other:\n    git: https://x/other\n  priv:\n    hosted: https://pub.example.com\n    version: ^2.0.0\ndev_dependencies:\n  test: ^1.24.0\nflutter:\n  uses-material-design: true\n")
		self.assertEqual([(n, s, sc) for n, s, sc, _ in rows],
			[("http", "^1.1.0", "runtime"), ("mine", "path", "local"), ("other", "git", "vcs"), ("priv", "^2.0.0", "runtime"), ("test", "^1.24.0", "dev")])

	def test_mix_deps(self):
		rows = parse_mix('defp deps do\n  [\n    {:phoenix, "~> 1.7.0"},\n    {:credo, "~> 1.7", only: [:dev, :test], runtime: false},\n'
			'    {:local_lib, path: "../local_lib"},\n    {:fork, github: "o/fork", branch: "main"},\n    {:jason, "~> 1.2", optional: true}\n  ]\nend')
		self.assertEqual([(n, s, sc) for n, s, sc, _ in rows],
			[("phoenix", "~> 1.7.0", "runtime"), ("credo", "~> 1.7", "dev"), ("local_lib", "path", "local"), ("fork", "git", "vcs"), ("jason", "~> 1.2", "runtime")])


class ComposerConstraintTests(unittest.TestCase):
	VS = ["v6.4.0", "v6.4.12", "v7.0.0", "v7.1.5", "v7.2.0-BETA1", "v7.2.0", "v8.0.0", "dev-main", "2.x-dev"]

	def test_picks_the_highest_stable_allowed(self):
		for spec, want in (("^7.0", "v7.2.0"), ("^6.4 || ^7.0", "v7.2.0"), ("~7.1", "v7.2.0"), ("7.*", "v7.2.0"), (">=6.4 <8", "v7.2.0"),
				("=7.1.5", "v7.1.5"), ("^8.0", "v8.0.0"), ("^9.0", None)):
			self.assertEqual(composer_pick(spec, self.VS), want, spec)
		self.assertEqual(composer_pick("^0.3", ["0.3.0", "0.3.9", "0.4.0"]), "0.3.9")
		self.assertEqual(composer_pick("^0.0.3", ["0.0.3", "0.0.4"]), "0.0.3")

	def test_stability_order_and_prereleases(self):
		self.assertEqual(sorted(["1.0.0-RC1", "1.0.0-beta2", "1.0.0", "1.0.0-patch1", "0.9.9"], key=composer_key),
			["0.9.9", "1.0.0-beta2", "1.0.0-RC1", "1.0.0", "1.0.0-patch1"])
		self.assertFalse(composer_satisfies(">=7.2", "7.2.0-BETA1"))
		self.assertTrue(composer_satisfies(">=7.2", "7.2.0"))


class LockfileTests(unittest.TestCase):
	def test_each_format(self):
		cases = {
			"package-lock.json": (json.dumps({"packages": {"": {}, "node_modules/lodash": {"version": "4.17.21"}, "node_modules/a/node_modules/lodash": {"version": "3.0.0"}}}), {"lodash": "4.17.21"}),
			"pnpm-lock.yaml": ("lockfileVersion: '9.0'\npackages:\n  '@scope/pkg@1.2.3':\n    resolution: {}\n  lodash@4.17.21:\n    resolution: {}\n", {"@scope/pkg": "1.2.3", "lodash": "4.17.21"}),
			"yarn.lock": ('# yarn lockfile v1\n\nlodash@^4.17.0, lodash@^4.0.0:\n  version "4.17.21"\n\n"@scope/pkg@npm:^1.0.0":\n  version: 1.2.3\n', {"lodash": "4.17.21", "@scope/pkg": "1.2.3"}),
			"uv.lock": ('[[package]]\nname = "requests"\nversion = "2.31.0"\n\n[[package]]\nname = "idna"\nversion = "3.6"\n', {"requests": "2.31.0", "idna": "3.6"}),
			"Pipfile.lock": (json.dumps({"default": {"requests": {"version": "==2.31.0"}}, "develop": {"black": {"version": "==24.1.0"}}}), {"requests": "2.31.0", "black": "24.1.0"}),
			"Cargo.lock": ('[[package]]\nname = "serde"\nversion = "1.0.200"\n', {"serde": "1.0.200"}),
			"Gemfile.lock": ("GEM\n  remote: https://rubygems.org/\n  specs:\n    rails (7.1.3)\n      actionpack (= 7.1.3)\n    pg (1.5.4)\n\nPLATFORMS\n  ruby\n", {"rails": "7.1.3", "pg": "1.5.4"}),
			"composer.lock": (json.dumps({"packages": [{"name": "monolog/monolog", "version": "3.5.0"}], "packages-dev": [{"name": "phpunit/phpunit", "version": "v10.5.0"}]}), {"monolog/monolog": "3.5.0", "phpunit/phpunit": "10.5.0"}),
			"pubspec.lock": ('packages:\n  http:\n    dependency: "direct main"\n    version: "1.2.1"\n  path:\n    version: "1.9.0"\nsdks:\n  dart: ">=3.0.0"\n', {"http": "1.2.1", "path": "1.9.0"}),
			"mix.lock": ('%{\n  "phoenix": {:hex, :phoenix, "1.7.14", "abc", [:mix], [], "hexpm", "def"},\n  "jason": {:hex, :jason, "1.4.1", "x", [:mix], [], "hexpm", "y"},\n}\n', {"phoenix": "1.7.14", "jason": "1.4.1"}),
			"Package.resolved": (json.dumps({"pins": [{"identity": "swift-argument-parser", "location": "https://github.com/apple/swift-argument-parser.git", "state": {"version": "1.3.0"}}], "version": 2}), {"apple/swift-argument-parser": "1.3.0"}),
			"packages.lock.json": (json.dumps({"version": 1, "dependencies": {"net8.0": {"Newtonsoft.Json": {"type": "Direct", "requested": "[13.0.3, )", "resolved": "13.0.3"}}}}), {"newtonsoft.json": "13.0.3"}),
		}
		for name, (text, want) in cases.items():
			self.assertEqual(parse_lockfile(name, text), want, name)
		self.assertEqual(parse_lockfile("Cargo.lock", "not = toml ="), {})
		self.assertEqual(parse_lockfile("go.sum", "x v1.0.0 h1:abc"), {})


class PinningAndLookupTests(unittest.TestCase):
	P2 = {"packages": {"monolog/monolog": [
		{"name": "monolog/monolog", "version": "3.12.1", "license": ["MIT"]},
		{"version": "3.5.0"},  # minified: unchanged fields are carried forward
		{"version": "3.0.0", "license": ["BSD-3-Clause"]},
	]}}

	def run_deps(self, files: dict, registry: dict, **kw):
		client = FixtureClient({f"/repos/acme/php/contents/{p}": {"data": {"content": b64(t)}} for p, t in files.items()})
		return teardown_deps(client, "acme/php", Tree("https://x", list(files)), set(), FixtureRegistry(registry), **kw)

	def test_lockfile_pins_the_version_and_the_lookup_uses_it(self):
		t = self.run_deps({"composer.json": '{"require":{"monolog/monolog":"^3.0"}}',
			"composer.lock": json.dumps({"packages": [{"name": "monolog/monolog", "version": "3.0.0"}]})},
			{"https://repo.packagist.org/p2/monolog/monolog.json": self.P2})
		dep = t.dependencies[0]
		self.assertEqual((dep.locked, dep.lock_source, dep.version_from), ("3.0.0", "composer.lock", "locked in composer.lock"))
		self.assertEqual(dep.license, "BSD-3-Clause")  # the locked release's license, not the latest
		self.assertEqual(t.lockfiles, ["composer.lock"])
		self.assertTrue(any("pinned to the version in a lockfile" in c.text for c in t.ledger.sourced))

	def test_without_a_lockfile_composer_rules_pick_the_release(self):
		t = self.run_deps({"composer.json": '{"require":{"monolog/monolog":"^3.0"}}'}, {"https://repo.packagist.org/p2/monolog/monolog.json": self.P2})
		dep = t.dependencies[0]
		self.assertEqual(dep.license, "MIT")
		self.assertIn("license of 3.12.1, the highest stable release it allows, as Composer picks", dep.license_note)

	def test_lockfile_in_the_manifests_folder_beats_the_root_one(self):
		t = self.run_deps({"api/composer.json": '{"require":{"monolog/monolog":"^3.0"}}',
			"api/composer.lock": json.dumps({"packages": [{"name": "monolog/monolog", "version": "3.5.0"}]}),
			"composer.lock": json.dumps({"packages": [{"name": "monolog/monolog", "version": "3.0.0"}]})},
			{"https://repo.packagist.org/p2/monolog/monolog.json": self.P2})
		self.assertEqual(t.dependencies[0].locked, "3.5.0")

	def test_vcs_and_local_dependencies_are_never_looked_up(self):
		t = self.run_deps({"mix.exs": 'defp deps do\n  [{:fork, github: "o/fork"}, {:lib, path: "../lib"}, {:jason, "~> 1.2"}]\nend'},
			{"https://hex.pm/api/packages/jason": {"meta": {"licenses": ["Apache-2.0"]}}})
		by = {d.name: d for d in t.dependencies}
		self.assertFalse(by["fork"].looked_up)
		self.assertFalse(by["lib"].looked_up)
		self.assertEqual(by["jason"].license, "Apache-2.0")

	def test_version_specific_registry_urls_and_fallback(self):
		reg = FixtureRegistry({"https://registry.npmjs.org/lodash/4.17.21": {"license": "MIT"}, "https://registry.npmjs.org/left-pad/latest": {"license": "WTFPL"}})
		d = Dependency("lodash", "^4.17", "npm", "runtime", "package.json")
		d.locked = "4.17.21"
		self.assertEqual(resolve_license(d, reg, 5)[:2], ("MIT", "https://registry.npmjs.org/lodash/4.17.21"))
		d = Dependency("left-pad", "^1.3", "npm", "runtime", "package.json")
		d.locked = "9.9.9"  # not on the registry: fall back to latest, and say so
		lic, src, _, note, used = resolve_license(d, reg, 5)
		self.assertEqual((lic, src, used), ("WTFPL", "https://registry.npmjs.org/left-pad/latest", 2))
		self.assertIn("version 9.9.9 not found", note)

	def test_swift_pub_hex_sources(self):
		reg = FixtureRegistry({"https://api.github.com/repos/apple/swift-argument-parser": {"license": {"spdx_id": "Apache-2.0"}},
			"https://pub.dev/api/packages/http/score": {"tags": ["license:bsd-3-clause", "license:osi-approved"]},
			"https://hex.pm/api/packages/phoenix": {"meta": {"licenses": ["MIT"]}}})
		swift = Dependency("apple/swift-argument-parser", "from 1.2.0", "swift", "runtime", "Package.swift")
		swift.url = "https://github.com/apple/swift-argument-parser.git"
		self.assertEqual(resolve_license(swift, reg, 5)[0], "Apache-2.0")
		other = Dependency("h", "branch main", "swift", "runtime", "Package.swift")
		other.url = "https://gitlab.com/g/h.git"
		self.assertEqual(resolve_license(other, reg, 5)[1], None)  # not GitHub: no lookup, skipped
		self.assertEqual(resolve_license(Dependency("http", "^1.1.0", "pub", "runtime", "pubspec.yaml"), reg, 5)[0], "BSD-3-Clause")
		self.assertEqual(resolve_license(Dependency("phoenix", "~> 1.7", "hex", "runtime", "mix.exs"), reg, 5)[0], "MIT")


if __name__ == "__main__":
	unittest.main()
