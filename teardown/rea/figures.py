"""Typed financial figures and a ranking that refuses to mix measures.

ARR, revenue guidance, annual contract value and product-revenue run-rate are
different quantities. Each figure declares its kind and is ranked only against
figures of the same kind. A range whose high end exceeds its low end by more
than DISPUTE_RATIO is flagged as disputed: it is ranked by its low end and must
not be quoted as a single number.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .evidence import Evidence, EvidenceError

METRIC_KINDS: dict[str, str] = {
	"arr": "Annual recurring revenue (ARR)",
	"annualized_run_rate": "Annualized revenue run-rate",
	"revenue_guidance": "Company revenue guidance (not ARR)",
	"acv": "Annual contract value (ACV)",
	"product_revenue_run_rate": "Product revenue run-rate",
	"analyst_estimate_arr": "Analyst ARR estimate (not company-reported)",
}

DISPUTE_RATIO = 1.25


@dataclass
class Figure:
	entity: str
	kind: str
	low: float
	high: float
	unit: str = "USD_millions"
	as_of: str | None = None
	sources: list[Evidence] = field(default_factory=list)
	note: str | None = None

	def __post_init__(self) -> None:
		if self.kind not in METRIC_KINDS:
			raise EvidenceError(f"{self.entity}: unknown metric kind {self.kind!r}; expected one of {sorted(METRIC_KINDS)}")
		if self.low <= 0 or self.high <= 0:
			raise EvidenceError(f"{self.entity}: values must be positive")
		if self.low > self.high:
			raise EvidenceError(f"{self.entity}: low ({self.low}) is greater than high ({self.high})")

	@property
	def verified(self) -> bool:
		return bool(self.sources) and all(s.is_valid() for s in self.sources)

	@property
	def disputed(self) -> bool:
		return self.high / self.low > DISPUTE_RATIO

	@property
	def flags(self) -> list[str]:
		out = []
		if not self.verified:
			out.append("UNVERIFIED")
		if self.disputed:
			out.append("DISPUTED")
		if self.kind == "analyst_estimate_arr":
			out.append("ESTIMATE")
		return out

	@classmethod
	def from_dict(cls, data: dict) -> "Figure":
		try:
			return cls(
				entity=data["entity"],
				kind=data["kind"],
				low=float(data["low"]),
				high=float(data.get("high", data["low"])),
				unit=data.get("unit", "USD_millions"),
				as_of=data.get("as_of"),
				sources=[Evidence.from_dict(s) for s in data.get("sources", [])],
				note=data.get("note"),
			)
		except KeyError as missing:
			raise EvidenceError(f"figure is missing field {missing}: {data!r}") from None


def rank(figures: list[Figure]) -> dict[str, list[Figure]]:
	"""Rank within each metric kind, by the conservative (low) end, largest first."""
	units = {f.unit for f in figures}
	if len(units) > 1:
		raise EvidenceError(f"figures use different units {sorted(units)}; convert before ranking")
	groups: dict[str, list[Figure]] = {}
	for f in figures:
		groups.setdefault(f.kind, []).append(f)
	for kind in groups:
		groups[kind].sort(key=lambda f: (-f.low, -f.high, f.entity))
	return dict(sorted(groups.items(), key=lambda kv: list(METRIC_KINDS).index(kv[0])))
