"""Teardown of a public GitHub repository from what the GitHub API reports.

Every claim cites the API response or page it came from. Checks read the
recursive file listing (git tree API) where GitHub returns it, and fall back to
top-level files when it does not; a
"not found" result means "not found where we looked", and the claim text says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from .evidence import Claim, Evidence, Ledger
from .github import NotFound
from .tree import TEST_DIRS, Tree, load_tree

PERMISSIVE = {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "0BSD", "Unlicense", "MPL-2.0"}
COPYLEFT = {"GPL-2.0", "GPL-3.0", "AGPL-3.0", "LGPL-2.1", "LGPL-3.0"}
LOCKFILES = {"package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock", "bun.lockb", "bun.lock", "uv.lock",
	"poetry.lock", "pdm.lock", "Pipfile.lock", "Cargo.lock", "go.sum", "Gemfile.lock", "composer.lock", "pubspec.lock", "mix.lock",
	"Package.resolved", "packages.lock.json"}

# signal -> (action for the original build, measurable acceptance criterion)
PLAN = {
	"license_permissive": ("keep", "Ship under a permissive license so adopters face no legal review delay.", "LICENSE file with an OSI-approved permissive SPDX id."),
	"license_missing": ("fix", "Choose and publish a license before anything else; without one nobody may legally reuse the code.", "GitHub reports an SPDX license id."),
	"license_copyleft": ("decide", "Decide whether copyleft obligations fit your distribution model; do not copy code from this repo into a closed product.", "Written license decision recorded in the repo."),
	"active": ("keep", "Keep a visible release and commit cadence.", "Default branch pushed within the last 30 days."),
	"stale": ("fix", "Plan for maintenance from day one: an owner, a cadence, and a deprecation policy.", "Named maintainer and a commit within every 90 days."),
	"archived": ("fix", "Treat as unmaintained: fork deliberately or pick a maintained alternative.", "Dependency on this repo replaced or forked with an owner."),
	"tests_present": ("keep", "Keep a test suite that runs on every change.", "Tests in the repository and CI running them on pull requests."),
	"tests_missing": ("fix", "Write tests alongside the first feature, not after.", "A test directory exists and CI fails when a test fails."),
	"ci_present": ("keep", "Run CI on every push and pull request.", "At least one workflow under .github/workflows."),
	"ci_missing": ("fix", "Add CI that runs lint, type checks and tests on every pull request.", "A workflow that blocks merge on failure."),
	"security_policy": ("keep", "Publish how to report vulnerabilities.", "SECURITY.md present."),
	"security_policy_missing": ("fix", "Publish a vulnerability disclosure policy and contact.", "SECURITY.md with a contact and response window."),
	"contributing": ("keep", "Document how to contribute.", "CONTRIBUTING.md present."),
	"contributing_missing": ("fix", "Document setup, test and review steps for contributors.", "CONTRIBUTING.md present."),
	"lockfile": ("keep", "Commit lockfiles so builds are reproducible.", "Lockfile committed for each package manager used."),
	"lockfile_missing": ("fix", "Commit a lockfile so two builds of one commit install the same dependencies.", "Lockfile committed."),
	"releases": ("keep", "Tag releases so users can pin versions.", "Tagged release at least every quarter."),
	"releases_missing": ("fix", "Tag versioned releases with notes.", "First tagged release with release notes."),
	"bus_factor_low": ("fix", "Spread knowledge: no single person should hold most of the commits.", "Top contributor below 50% of contributions on the default branch."),
	"contributors_broad": ("keep", "Keep a broad contributor base with clear review rules.", "10 or more contributors."),
	"docs": ("keep", "Keep user docs next to the code.", "docs/ directory or a README of 2 KB or more."),
	"deps_copyleft": ("decide", "Decide per copyleft dependency: replace it, isolate it behind a process boundary, or accept the source-release obligation in writing.", "Each copyleft dependency has a recorded decision."),
	"deps_license_unknown": ("fix", "Resolve every dependency license the registry does not state; an unknown license is not a permission.", "Every runtime dependency has an identified license."),
	"deps_licenses_clear": ("keep", "Keep a license check on new dependencies.", "A license allow-list enforced in CI."),
	"deps_vulnerable": ("fix", "Upgrade, patch or replace each dependency with a known vulnerability, starting with the highest severity; add a vulnerability check to CI so new ones block merge.", "OSV.dev lists no advisory for any resolved dependency version."),
	"docs_thin": ("fix", "Write a README that covers install, a first example and limits.", "README of 2 KB or more."),
}


@dataclass
class RepoTeardown:
	repo: str
	html_url: str
	facts: dict = field(default_factory=dict)
	tree: Tree | None = None
	top_level: set = field(default_factory=set)
	ledger: Ledger = field(default_factory=Ledger)

	def plan(self) -> list[dict]:
		items = []
		for claim in self.ledger.sourced:
			if claim.signal in PLAN:
				action, step, measure = PLAN[claim.signal]
				items.append({"action": action, "step": step, "measure": measure,
					"because": claim.text, "evidence": [e.url for e in claim.evidence]})
		order = {"fix": 0, "decide": 1, "keep": 2}
		return sorted(items, key=lambda i: order[i["action"]])


def _days_since(iso: str, now: datetime) -> int:
	then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
	return (now - then).days


def teardown_repo(client, repo: str, now: datetime | None = None) -> RepoTeardown:
	now = now or datetime.now(timezone.utc)
	if repo.count("/") != 1:
		raise ValueError(f"expected owner/name, got {repo!r}")
	base = f"/repos/{repo}"
	meta = client.get(base)
	d = meta.data
	html = d["html_url"]
	t = RepoTeardown(repo=d["full_name"], html_url=html)
	src = Evidence(meta.url)

	def add(kind: str, text: str, signal: str | None = None, url: str | None = None) -> None:
		t.ledger.add(Claim(kind=kind, text=text, signal=signal, evidence=[Evidence(url) if url else src]))

	t.facts = {"stars": d.get("stargazers_count"), "forks": d.get("forks_count"),
		"open_issues_and_prs": d.get("open_issues_count"), "default_branch": d.get("default_branch"),
		"pushed_at": d.get("pushed_at"), "description": d.get("description")}
	add("fact", f"{d.get('stargazers_count', 0)} stars and {d.get('forks_count', 0)} forks. Popularity, not a quality measure.")

	lic = (d.get("license") or {}).get("spdx_id")
	if not lic or lic == "NOASSERTION":
		add("weakness", "No license GitHub can identify; reuse is legally unclear.", "license_missing")
	elif lic in PERMISSIVE:
		add("strength", f"Permissive license ({lic}).", "license_permissive")
	elif lic in COPYLEFT:
		add("fact", f"Copyleft license ({lic}); derivative works carry obligations.", "license_copyleft")
	else:
		add("fact", f"License {lic}.")

	if d.get("archived"):
		add("weakness", "Repository is archived (read-only).", "archived")
	if d.get("pushed_at"):
		days = _days_since(d["pushed_at"], now)
		if days <= 30:
			add("strength", f"Active: last push {days} days ago.", "active")
		elif days > 365:
			add("weakness", f"Stale: last push {days} days ago.", "stale")
		else:
			add("fact", f"Last push {days} days ago.")

	try:
		langs = client.get(f"{base}/languages")
		total = sum(langs.data.values()) or 1
		top = ", ".join(f"{k} {(v * 100 / total):.0f}%" if v * 100 / total >= 1 else f"{k} <1%" for k, v in sorted(langs.data.items(), key=lambda kv: -kv[1])[:4])
		if top:
			t.ledger.add(Claim("fact", f"Languages by bytes: {top}.", [Evidence(langs.url)]))
	except NotFound:
		pass

	root = client.get(f"{base}/contents")
	names = {e["name"] for e in root.data if isinstance(e, dict)}
	lower = {n.lower(): n for n in names}
	t.top_level = names
	root_url = root.url

	tree = load_tree(client, base, d.get("default_branch") or "HEAD")
	t.tree = tree
	partial = " The file listing was truncated by GitHub, so this may be incomplete." if tree and tree.truncated else ""
	if tree is not None:
		where = tree.test_locations()
		if where:
			add("strength", f"Tests found in: {', '.join(where)}.", "tests_present", tree.url)
		else:
			add("weakness", f"No test folders or test-named files anywhere in the repository.{partial}", "tests_missing", tree.url)
	elif names & TEST_DIRS:
		add("strength", f"Top-level test directory: {', '.join(sorted(names & TEST_DIRS))}.", "tests_present", root_url)
	else:
		add("weakness", "No top-level test directory found (full file listing unavailable; tests may live deeper).", "tests_missing", root_url)

	github_dir = set()
	if ".github" in names:
		try:
			github_dir = {e["name"] for e in client.get(f"{base}/contents/.github").data}
		except NotFound:
			pass
	try:
		wf = client.get(f"{base}/contents/.github/workflows")
		count = len([e for e in wf.data if e.get("name", "").endswith((".yml", ".yaml"))])
		if count:
			add("strength", f"{count} GitHub Actions workflow file(s).", "ci_present", wf.url)
		else:
			add("weakness", "No GitHub Actions workflows found.", "ci_missing", wf.url)
	except NotFound:
		add("weakness", "No GitHub Actions workflows found.", "ci_missing", root_url)

	if "security.md" in lower or "SECURITY.md" in github_dir:
		add("strength", "Publishes a security policy (SECURITY.md).", "security_policy", root_url)
	else:
		add("weakness", "No SECURITY.md in the root or .github.", "security_policy_missing", root_url)
	if "contributing.md" in lower or "CONTRIBUTING.md" in github_dir:
		add("strength", "Has contributor guide (CONTRIBUTING.md).", "contributing", root_url)
	else:
		add("weakness", "No CONTRIBUTING.md in the root or .github.", "contributing_missing", root_url)

	if tree is not None:
		locks = tree.named(LOCKFILES)
		if locks:
			shown = ", ".join(locks[:5]) + (f" and {len(locks) - 5} more" if len(locks) > 5 else "")
			add("strength", f"Committed lockfile(s): {shown}.", "lockfile", tree.url)
		else:
			add("weakness", f"No lockfile anywhere in the repository.{partial}", "lockfile_missing", tree.url)
	elif names & LOCKFILES:
		add("strength", f"Committed lockfile(s): {', '.join(sorted(names & LOCKFILES))}.", "lockfile", root_url)
	else:
		add("weakness", "No lockfile at the top level (full file listing unavailable).", "lockfile_missing", root_url)
	if "Dockerfile" in names or "docker-compose.yml" in names or "compose.yaml" in names:
		add("fact", "Container build files at the top level.", url=root_url)

	readme_size = next((e.get("size", 0) for e in root.data if e.get("name", "").lower().startswith("readme")), 0)
	parts = (["docs/ directory"] if "docs" in names else []) + ([f"README {readme_size // 1024} KB"] if readme_size >= 2048 else [])
	if parts:
		add("strength", f"Documentation: {' and '.join(parts)}.", "docs", root_url)
	else:
		add("weakness", f"Thin docs: no docs/ and README is {readme_size} bytes.", "docs_thin", root_url)

	try:
		rel = client.get(f"{base}/releases?per_page=1")
		if rel.data:
			r = rel.data[0]
			add("strength", f"Tagged releases; latest {r.get('tag_name')} published {str(r.get('published_at', ''))[:10]}.", "releases", rel.url)
		else:
			add("weakness", "No GitHub releases.", "releases_missing", rel.url)
	except NotFound:
		add("weakness", "No GitHub releases.", "releases_missing")

	try:
		contrib = client.get(f"{base}/contributors?per_page=100&anon=1")
		people = contrib.data or []
		# A "next" link means the first page (100) is full.
		many = bool(contrib.link)
		approx = "100+" if many else str(len(people))
		if people:
			commits = sum(p.get("contributions", 0) for p in people) or 1
			share = people[0].get("contributions", 0) / commits
			if share > 0.8 and len(people) < 10 and not many:
				add("weakness", f"Bus factor: top contributor has {share:.0%} of contributions among {len(people)} contributors.", "bus_factor_low", contrib.url)
			elif many or len(people) >= 10:
				add("strength", f"{approx} contributors; top contributor holds {share:.0%} of the first page's contributions.", "contributors_broad", contrib.url)
			else:
				add("fact", f"{len(people)} contributors.", url=contrib.url)
	except NotFound:
		pass
	return t
