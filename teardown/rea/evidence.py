"""Evidence and claims: the receipts layer.

A claim reaches a report's main body only with at least one https source.
Anything else is kept, but in a separate "unverified" list that is never
ranked, scored or turned into a build-plan item.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlparse

CLAIM_KINDS = ("strength", "weakness", "fact", "revenue_model")


class EvidenceError(ValueError):
	pass


@dataclass(frozen=True)
class Evidence:
	url: str
	quote: str | None = None
	retrieved_at: str | None = None

	def is_valid(self) -> bool:
		parsed = urlparse(self.url)
		return parsed.scheme == "https" and bool(parsed.netloc)

	@classmethod
	def from_dict(cls, data: dict) -> "Evidence":
		if not isinstance(data, dict) or not isinstance(data.get("url"), str):
			raise EvidenceError(f"source must be an object with a url: {data!r}")
		return cls(url=data["url"], quote=data.get("quote"), retrieved_at=data.get("retrieved_at"))


@dataclass
class Claim:
	kind: str
	text: str
	evidence: list[Evidence] = field(default_factory=list)
	signal: str | None = None
	countermeasure: str | None = None

	def __post_init__(self) -> None:
		if self.kind not in CLAIM_KINDS:
			raise EvidenceError(f"unknown claim kind {self.kind!r}; expected one of {CLAIM_KINDS}")
		if not self.text or not self.text.strip():
			raise EvidenceError("claim text is empty")

	@property
	def sourced(self) -> bool:
		return bool(self.evidence) and all(e.is_valid() for e in self.evidence)


@dataclass
class Ledger:
	"""Splits claims into sourced and unverified on entry."""

	sourced: list[Claim] = field(default_factory=list)
	unverified: list[Claim] = field(default_factory=list)

	def add(self, claim: Claim) -> None:
		(self.sourced if claim.sourced else self.unverified).append(claim)

	def of_kind(self, kind: str) -> list[Claim]:
		return [c for c in self.sourced if c.kind == kind]
