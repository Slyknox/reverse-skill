"""Fetch-and-quote verification: does each source page still contain its quote?

A source is "verified" only when its page was fetched over https and the quote
appears in the page text after normalization (case, whitespace, curly quotes,
dashes and HTML entities are evened out; nothing else is fuzzy). PDFs are read
with the optional pypdf package when it is installed. Everything
else gets a status that says why it is not verified.
"""

from __future__ import annotations

import html
import ipaddress
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlparse

MAX_BYTES = 5 * 1024 * 1024
MAX_PDF_BYTES = 25 * 1024 * 1024  # annual reports and filings are often 5-20 MB
MAX_PDF_PAGES = 500
PDF_TYPES = ("application/pdf", "application/x-pdf")
TEXT_TYPES = ("text/html", "text/plain", "application/xhtml+xml", "application/json", "text/markdown", "application/xml", "text/xml")

STATUSES = {
	"verified": "Quote found on the page.",
	"quote_not_found": "Page fetched, but the quote is not on it.",
	"no_quote": "Source has no quote to check.",
	"fetch_failed": "Page could not be fetched.",
	"unsupported_type": "Page is not text, HTML or PDF, or a PDF reader is not installed; check it by hand.",
	"refused_url": "Not an https URL on a public host.",
}


class FetchError(Exception):
	def __init__(self, status: str, reason: str):
		super().__init__(reason)
		self.status = status
		self.reason = reason


class _Text(HTMLParser):
	SKIP = {"script", "style", "noscript", "template", "svg"}
	BLOCK = {"p", "div", "br", "li", "ul", "ol", "tr", "td", "th", "table", "section", "article", "header", "footer",
		"h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "dd", "dt", "figcaption", "main", "nav", "aside"}

	def __init__(self):
		super().__init__(convert_charrefs=True)
		self.parts: list[str] = []
		self._skip = 0

	def handle_starttag(self, tag, attrs):
		if tag in self.SKIP:
			self._skip += 1
		elif tag in self.BLOCK:
			self.parts.append(" ")

	def handle_endtag(self, tag):
		if tag in self.SKIP and self._skip:
			self._skip -= 1
		elif tag in self.BLOCK:
			self.parts.append(" ")

	def handle_data(self, data):
		if not self._skip:
			self.parts.append(data)


def html_to_text(page: str) -> str:
	parser = _Text()
	parser.feed(page)
	parser.close()
	# Inline elements (links, emphasis) join without a gap, so "<a>California</a>, and" stays "California, and".
	return "".join(parser.parts)


_FOLD = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
	"−": "-", " ": " ", " ": " ", " ": " "})


def normalize(text: str) -> str:
	text = re.sub(r"\s+", " ", html.unescape(text).translate(_FOLD))
	return re.sub(r" ([,.;:!?)\]])", r"\1", text).strip().lower()


def contains_quote(page_text: str, quote: str) -> bool:
	q = normalize(quote)
	# PDF text may carry a second, de-hyphenated copy after "\n\f\n"; a quote must sit wholly inside one copy.
	return bool(q) and any(q in normalize(part) for part in page_text.split("\n\f\n"))


def _public_https(url: str) -> bool:
	parsed = urlparse(url)
	if parsed.scheme != "https" or not parsed.hostname:
		return False
	try:
		infos = socket.getaddrinfo(parsed.hostname, parsed.port or 443, proto=socket.IPPROTO_TCP)
	except socket.gaierror:
		return True  # resolution failure is reported by the fetch itself
	for info in infos:
		ip = ipaddress.ip_address(info[4][0])
		if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
			return False
	return True


class _HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
	def redirect_request(self, req, fp, code, msg, headers, newurl):
		if not _public_https(newurl):
			raise FetchError("refused_url", f"redirect to a non-https or non-public URL: {newurl}")
		return super().redirect_request(req, fp, code, msg, headers, newurl)


class Fetcher:
	"""Fetches page text over https from public hosts, with a size cap. Caches per URL."""

	def __init__(self, timeout: float = 20.0):
		self.timeout = timeout
		self.cache: dict[str, str | FetchError] = {}
		self.opener = urllib.request.build_opener(_HttpsOnlyRedirects)

	def text(self, url: str) -> str:
		if url in self.cache:
			hit = self.cache[url]
			if isinstance(hit, FetchError):
				raise hit
			return hit
		try:
			value = self._fetch(url)
		except FetchError as err:
			self.cache[url] = err
			raise
		self.cache[url] = value
		return value

	def _fetch(self, url: str) -> str:
		if not _public_https(url):
			raise FetchError("refused_url", STATUSES["refused_url"])
		req = urllib.request.Request(url, headers={"User-Agent": "rea-teardown/0.1 (quote check)", "Accept": "text/html,text/plain;q=0.9,*/*;q=0.1"})
		try:
			with self.opener.open(req, timeout=self.timeout) as resp:
				ctype = (resp.headers.get_content_type() or "").lower()
				if ctype in PDF_TYPES or (ctype == "application/octet-stream" and urlparse(url).path.lower().endswith(".pdf")):
					body = resp.read(MAX_PDF_BYTES + 1)
					if len(body) > MAX_PDF_BYTES:
						raise FetchError("fetch_failed", "PDF larger than 25 MB")
					return pdf_to_text(body)
				if ctype not in TEXT_TYPES:
					raise FetchError("unsupported_type", f"content type {ctype}")
				body = resp.read(MAX_BYTES + 1)
				if len(body) > MAX_BYTES:
					raise FetchError("fetch_failed", "page larger than 5 MB")
				raw = body.decode(resp.headers.get_content_charset() or "utf-8", "replace")
		except FetchError:
			raise
		except urllib.error.HTTPError as err:
			raise FetchError("fetch_failed", f"HTTP {err.code}") from None
		except (urllib.error.URLError, TimeoutError, OSError) as err:
			raise FetchError("fetch_failed", str(getattr(err, "reason", err))) from None
		return html_to_text(raw) if ctype in ("text/html", "application/xhtml+xml") else raw


OCR_MARK = "[rea: text below was read by OCR from a scanned PDF]\n"
MAX_OCR_PAGES = 30
OCR_TIMEOUT = 60  # seconds per page image


def ocr_pages(reader) -> str:
	"""OCR the images of a PDF that has no text layer, with the tesseract command if it is installed.

	Each page's embedded images are converted to PNG (Pillow) and passed to
	`tesseract stdin stdout`. Refuses documents over MAX_OCR_PAGES rather than
	reading part of them.
	"""
	import shutil
	import subprocess

	exe = shutil.which("tesseract")
	if exe is None:
		raise FetchError("unsupported_type", "PDF has no text layer (scanned); install tesseract to OCR it, or check it by hand")
	if len(reader.pages) > MAX_OCR_PAGES:
		raise FetchError("unsupported_type", f"scanned PDF has {len(reader.pages)} pages, over the {MAX_OCR_PAGES}-page OCR limit; check it by hand")
	try:
		import io

		from PIL import Image  # noqa: F401  (pypdf needs Pillow to decode page images)
	except ImportError:
		raise FetchError("unsupported_type", "scanned PDF: install Pillow (pip install pillow) so its images can be OCRed") from None
	parts = []
	for page in reader.pages:
		for img in page.images:
			buf = io.BytesIO()
			try:
				img.image.save(buf, format="PNG")
			except Exception as err:  # pypdf/Pillow raise many types for unusual image encodings
				raise FetchError("unsupported_type", f"scanned PDF image could not be decoded for OCR ({type(err).__name__})") from None
			try:
				run = subprocess.run([exe, "stdin", "stdout"], input=buf.getvalue(), capture_output=True, timeout=OCR_TIMEOUT, check=True)
			except subprocess.TimeoutExpired:
				raise FetchError("fetch_failed", "OCR timed out") from None
			except subprocess.CalledProcessError as err:
				raise FetchError("fetch_failed", f"tesseract failed: {err.stderr.decode(errors='replace')[:200]}") from None
			parts.append(run.stdout.decode("utf-8", "replace"))
	text = "\n".join(parts)
	if not text.strip():
		raise FetchError("unsupported_type", "OCR found no text in the scanned PDF; check it by hand")
	return text


def pdf_to_text(data: bytes) -> str:
	"""Text of every page, via the optional pypdf package. Refuses rather than reads part of a document.

	PDF text often breaks words across lines with a hyphen ("compa-\\nny"). A quote
	may be checked against any form, so the result holds the text as extracted and,
	each after "\\n\\f\\n", the text with line-end hyphens dropped and kept.
	"""
	if not data.startswith(b"%PDF"):
		raise FetchError("fetch_failed", "served as PDF but is not a PDF file")
	try:
		from pypdf import PdfReader
		from pypdf.errors import PdfReadError
	except ImportError:
		raise FetchError("unsupported_type", "PDF source: install pypdf (pip install pypdf) to check it") from None
	import io

	try:
		reader = PdfReader(io.BytesIO(data))
		if reader.is_encrypted:
			raise FetchError("unsupported_type", "PDF is encrypted")
		if len(reader.pages) > MAX_PDF_PAGES:
			raise FetchError("fetch_failed", f"PDF has {len(reader.pages)} pages, over the {MAX_PDF_PAGES}-page limit")
		text = "\n".join(page.extract_text() or "" for page in reader.pages)
	except FetchError:
		raise
	except (PdfReadError, ValueError, KeyError, TypeError) as err:
		raise FetchError("fetch_failed", f"PDF could not be read: {err}") from None
	if not text.strip():
		text = ocr_pages(reader)  # raises FetchError when OCR is unavailable or finds nothing
		ocr = True
	else:
		ocr = False
	# A line-end hyphen is either a word split ("mil-" + "lion") or a real hyphen ("cloud-" + "based").
	joined = re.sub(r"(\w)-[ \t]*\n\s*(\w)", r"\1\2", text)
	kept = re.sub(r"(\w)-[ \t]*\n\s*(\w)", r"\1-\2", text)
	body = text if joined == text else f"{text}\n\f\n{joined}\n\f\n{kept}"
	return OCR_MARK + body if ocr else body


class FixtureFetcher:
	"""{url: page text}, {url: {"pdf_base64": "..."}} or {url: {"error": status, "reason": "..."}}; a missing url fails to fetch."""

	def __init__(self, pages: dict):
		self.pages = pages

	def text(self, url: str) -> str:
		page = self.pages.get(url)
		if page is None:
			raise FetchError("fetch_failed", "not in fixture")
		if isinstance(page, dict) and "pdf_base64" in page:
			import base64

			return pdf_to_text(base64.b64decode(page["pdf_base64"]))
		if isinstance(page, dict):
			raise FetchError(page.get("error", "fetch_failed"), page.get("reason", "fixture error"))
		return html_to_text(page) if "<" in page and ">" in page else page


@dataclass
class Check:
	url: str
	quote: str | None
	status: str
	reason: str | None = None


def check_source(fetcher, url: str, quote: str | None) -> Check:
	if not quote or not quote.strip():
		return Check(url, quote, "no_quote")
	try:
		page = fetcher.text(url)
	except FetchError as err:
		return Check(url, quote, err.status, err.reason)
	found = contains_quote(page, quote)
	if page.startswith(OCR_MARK):
		reason = ("found in OCR text of a scanned PDF; OCR can misread, so confirm by eye" if found
			else "not found in OCR text of a scanned PDF; OCR may have misread it, so check by hand")
		return Check(url, quote, "verified" if found else "quote_not_found", reason)
	return Check(url, quote, "verified" if found else "quote_not_found")
