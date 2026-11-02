"""Company or product teardown from a sourced dossier.

The engine does not research companies itself; it never invents a fact. You
supply a dossier of claims and figures, each with sources. It separates what is
sourced from what is not, builds the pros and cons matrix, and turns each
sourced strength into a "keep" item and each sourced weakness into a
countermeasure item (yours, or an explicit TODO when you gave none).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .evidence import Claim, Evidence, EvidenceError, Ledger
from .figures import Figure


@dataclass
class CompanyTeardown:
	entity: str
	as_of: str | None
	ledger: Ledger = field(default_factory=Ledger)
	figures: list[Figure] = field(default_factory=list)

	def plan(self) -> list[dict]:
		items = []
		for c in self.ledger.of_kind("weakness"):
			items.append({"action": "counter", "step": c.countermeasure or "TODO: define a countermeasure and how you will measure it.",
				"because": c.text, "evidence": [e.url for e in c.evidence]})
		for c in self.ledger.of_kind("strength"):
			items.append({"action": "keep", "step": c.countermeasure or f"Match or exceed: {c.text}",
				"because": c.text, "evidence": [e.url for e in c.evidence]})
		return items


def load_dossier(data: dict) -> CompanyTeardown:
	if "entity" not in data:
		raise EvidenceError("dossier needs an 'entity'")
	t = CompanyTeardown(entity=data["entity"], as_of=data.get("as_of"))
	for raw in data.get("claims", []):
		t.ledger.add(Claim(kind=raw.get("kind", ""), text=raw.get("text", ""),
			evidence=[Evidence.from_dict(s) for s in raw.get("sources", [])],
			countermeasure=raw.get("countermeasure")))
	t.figures = [Figure.from_dict({"entity": data["entity"], **f}) for f in data.get("figures", [])]
	return t


def load_dossier_file(path: str) -> CompanyTeardown:
	with open(path, encoding="utf-8") as fh:
		return load_dossier(json.load(fh))
