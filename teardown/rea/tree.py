"""Recursive file listing from the git tree API, for checks below the top level."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .github import NotFound

# Third-party or generated folders: files inside them say nothing about the project's own practice.
IGNORED_SEGMENTS = {"node_modules", "vendor", "third_party", "third-party", ".git", "dist", "build", "site-packages", ".venv", "venv"}
TEST_DIRS = {"tests", "test", "__tests__", "spec", "e2e", "testing"}
TEST_FILE = re.compile(r"(^test_.*\.py$|_test\.(py|go)$|\.(test|spec)\.(js|jsx|ts|tsx|mjs|cjs)$|Tests?\.(java|kt|cs)$)")


@dataclass
class Tree:
	url: str
	files: list[str] = field(default_factory=list)
	truncated: bool = False

	def test_locations(self, limit: int = 5) -> list[str]:
		"""Distinct folders that hold tests: a test-named folder, or the folder of a test-named file."""
		found: list[str] = []
		for path in self.files:
			parts = path.split("/")
			hit = None
			for i, seg in enumerate(parts[:-1]):
				if seg in TEST_DIRS:
					hit = "/".join(parts[: i + 1])
					break
			if hit is None and TEST_FILE.search(parts[-1]):
				hit = "/".join(parts[:-1]) or "."
			if hit and hit not in found:
				found.append(hit)
		return found[:limit]

	def named(self, names: set[str]) -> list[str]:
		return [p for p in self.files if p.rsplit("/", 1)[-1] in names]


def load_tree(client, base: str, branch: str) -> Tree | None:
	try:
		resp = client.get(f"{base}/git/trees/{branch}?recursive=1")
	except NotFound:
		return None
	files = [e["path"] for e in resp.data.get("tree", [])
		if e.get("type") == "blob" and not (set(e["path"].split("/")) & IGNORED_SEGMENTS)]
	return Tree(url=resp.url, files=files, truncated=bool(resp.data.get("truncated")))
