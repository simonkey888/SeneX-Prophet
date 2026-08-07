from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class CrossMarketState:
    source: str
    instrument: str
    observed_at: str
    fields: Mapping[str, float | int | str | None]
    provenance: str = "FIXTURE_OR_SYNTHETIC_ONLY"
    schema_version: str = "senex-cross-market-state-v1"

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["fields"] = dict(self.fields)
        return value


class DisabledCrossMarketAdapter:
    """Interface placeholder. External live adapters are disabled by contract."""

    enabled = False
    external_network_reads = 0
    authenticated_reads = 0
    writes = 0

    def fetch_live(self, *args: Any, **kwargs: Any) -> CrossMarketState:
        raise RuntimeError("CROSS_MARKET_EXTERNAL_LIVE_ADAPTER_DISABLED")

    @staticmethod
    def from_fixture(payload: Mapping[str, Any]) -> CrossMarketState:
        required = {"source", "instrument", "observed_at", "fields"}
        missing = required - set(payload)
        if missing:
            raise ValueError(f"missing fixture fields: {sorted(missing)}")
        fields = payload["fields"]
        if not isinstance(fields, Mapping):
            raise ValueError("fields must be mapping")
        return CrossMarketState(
            source=str(payload["source"]),
            instrument=str(payload["instrument"]),
            observed_at=str(payload["observed_at"]),
            fields=dict(fields),
        )
