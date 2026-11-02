"""Minimal read-only GitHub REST client (stdlib only) and an offline fixture client."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

API = "https://api.github.com"
_LAST_PAGE = re.compile(r'[?&]page=(\d+)>; rel="last"')


class NotFound(Exception):
	pass


class GitHubError(Exception):
	pass


class Response:
	def __init__(self, url: str, data, link: str | None = None):
		self.url = url
		self.data = data
		self.link = link

	def total_from_link(self) -> int:
		"""With per_page=1, the 'last' page number is the item count."""
		if self.link:
			m = _LAST_PAGE.search(self.link)
			if m:
				return int(m.group(1))
		return len(self.data) if isinstance(self.data, list) else 0


class Client:
	def __init__(self, token: str | None = None, timeout: float = 20.0):
		self.token = token if token is not None else os.environ.get("GITHUB_TOKEN")
		self.timeout = timeout

	def get(self, path: str) -> Response:
		url = f"{API}{path}"
		req = urllib.request.Request(url, headers={
			"Accept": "application/vnd.github+json",
			"User-Agent": "rea-teardown/0.1",
		})
		if self.token:
			req.add_header("Authorization", f"Bearer {self.token}")
		try:
			with urllib.request.urlopen(req, timeout=self.timeout) as resp:
				return Response(url, json.load(resp), resp.headers.get("Link"))
		except urllib.error.HTTPError as err:
			if err.code == 404:
				raise NotFound(url) from None
			hint = " (rate limit or access refused; set GITHUB_TOKEN)" if err.code in (401, 403, 429) else ""
			raise GitHubError(f"GitHub answered {err.code} for {url}{hint}") from None
		except urllib.error.URLError as err:
			raise GitHubError(f"could not reach {url}: {err.reason}") from None


class FixtureClient:
	"""Serves recorded responses: {"<path>": {"data": ..., "link": "..."}}; missing path = 404."""

	def __init__(self, fixture: dict):
		self.fixture = fixture

	def get(self, path: str) -> Response:
		entry = self.fixture.get(path)
		if entry is None:
			raise NotFound(f"{API}{path}")
		return Response(f"{API}{path}", entry.get("data"), entry.get("link"))
