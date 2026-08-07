from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .contracts import canonical_json


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _AppendOnlyJsonl:
    def __init__(self, path: Path | None = None):
        self.path = None if path is None else Path(path)
        self._records: list[dict[str, Any]] = []
        if self.path and self.path.exists():
            self._records = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]
            self._verify_chain()

    @property
    def records(self) -> tuple[dict[str, Any], ...]:
        return tuple(dict(item) for item in self._records)

    def _verify_chain(self) -> None:
        previous = "0" * 64
        for index, record in enumerate(self._records, 1):
            expected = self._digest(index, previous, record["payload"])
            if record.get("sequence") != index or record.get("previous_hash") != previous or record.get("record_hash") != expected:
                raise ValueError("APPEND_ONLY_REGISTRY_CHAIN_INVALID")
            previous = expected

    @staticmethod
    def _digest(sequence: int, previous: str, payload: Mapping[str, Any]) -> str:
        envelope = {"sequence": sequence, "previous_hash": previous, "payload": dict(payload)}
        return hashlib.sha256(canonical_json(envelope).encode("utf-8")).hexdigest()

    def append(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        sequence = len(self._records) + 1
        previous = self._records[-1]["record_hash"] if self._records else "0" * 64
        record_hash = self._digest(sequence, previous, payload)
        record = {
            "sequence": sequence,
            "previous_hash": previous,
            "record_hash": record_hash,
            "payload": dict(payload),
        }
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            try:
                os.write(fd, (canonical_json(record) + "\n").encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
        self._records.append(record)
        return dict(record)

    def verify(self) -> bool:
        try:
            self._verify_chain()
        except ValueError:
            return False
        return True


class ExperimentRegistry:
    VALID_RESULTS = {"PASS", "FAIL", "INCONCLUSIVE"}

    def __init__(self, path: Path | None = None):
        self._log = _AppendOnlyJsonl(path)

    @property
    def records(self) -> tuple[dict[str, Any], ...]:
        return self._log.records

    def preregister(
        self,
        *,
        experiment_id: str,
        hypothesis: str,
        formula_or_feature_versions: Mapping[str, str],
        dataset_hash: str,
        featureset_hash: str,
        pre_registered_metrics: list[str],
        pre_registered_pass_fail_rule: str,
        start_time: str,
        end_time: str,
        supersedes: str | None = None,
    ) -> dict[str, Any]:
        if not pre_registered_metrics or not pre_registered_pass_fail_rule:
            raise ValueError("EXPERIMENT_PREREGISTRATION_INCOMPLETE")
        existing = [r for r in self.records if r["payload"].get("experiment_id") == experiment_id and r["payload"].get("record_type") == "PREREGISTRATION"]
        if existing:
            raise ValueError("EXPERIMENT_ALREADY_PREREGISTERED")
        payload = {
            "record_type": "PREREGISTRATION",
            "experiment_id": experiment_id,
            "hypothesis": hypothesis,
            "formula_or_feature_versions": dict(formula_or_feature_versions),
            "dataset_hash": dataset_hash,
            "featureset_hash": featureset_hash,
            "pre_registered_metrics": list(pre_registered_metrics),
            "pre_registered_pass_fail_rule": pre_registered_pass_fail_rule,
            "start_time": start_time,
            "end_time": end_time,
            "OOS_metrics": {},
            "leakage_gate": "PENDING",
            "result": "INCONCLUSIVE",
            "reason": "PREREGISTERED_NOT_EVALUATED",
            "supersedes": supersedes,
            "observed_at": _now(),
        }
        return self._log.append(payload)

    def record_result(
        self,
        *,
        experiment_id: str,
        oos_metrics: Mapping[str, float | int | str | None],
        leakage_gate: str,
        result: str,
        reason: str,
    ) -> dict[str, Any]:
        if result not in self.VALID_RESULTS:
            raise ValueError("INVALID_EXPERIMENT_RESULT")
        prereg = [r for r in self.records if r["payload"].get("experiment_id") == experiment_id and r["payload"].get("record_type") == "PREREGISTRATION"]
        if len(prereg) != 1:
            raise ValueError("EXPERIMENT_NOT_PREREGISTERED")
        prior_results = [r for r in self.records if r["payload"].get("experiment_id") == experiment_id and r["payload"].get("record_type") == "RESULT"]
        if prior_results:
            raise ValueError("EXPERIMENT_RESULT_ALREADY_RECORDED")
        base = prereg[0]["payload"]
        payload = {
            **{key: base[key] for key in (
                "experiment_id", "hypothesis", "formula_or_feature_versions", "dataset_hash",
                "featureset_hash", "pre_registered_metrics", "pre_registered_pass_fail_rule",
                "start_time", "end_time",
            )},
            "record_type": "RESULT",
            "OOS_metrics": dict(oos_metrics),
            "leakage_gate": leakage_gate,
            "result": result,
            "reason": reason,
            "supersedes": prereg[0]["record_hash"],
            "observed_at": _now(),
        }
        return self._log.append(payload)

    def latest(self) -> list[dict[str, Any]]:
        latest: dict[str, dict[str, Any]] = {}
        for record in self.records:
            latest[record["payload"]["experiment_id"]] = record
        return [latest[key] for key in sorted(latest)]

    def verify(self) -> bool:
        return self._log.verify()


class ContradictionLedger:
    def __init__(self, path: Path | None = None):
        self._log = _AppendOnlyJsonl(path)

    @property
    def records(self) -> tuple[dict[str, Any], ...]:
        return self._log.records

    def open(
        self,
        *,
        contradiction_id: str,
        claim_a: str,
        claim_b: str,
        evidence_refs: list[str],
        observed_at: str | None = None,
    ) -> dict[str, Any]:
        if any(r["payload"].get("contradiction_id") == contradiction_id for r in self.records):
            raise ValueError("CONTRADICTION_ID_ALREADY_EXISTS")
        return self._log.append({
            "contradiction_id": contradiction_id,
            "observed_at": observed_at or _now(),
            "claim_a": claim_a,
            "claim_b": claim_b,
            "evidence_refs": list(evidence_refs),
            "status": "OPEN",
            "resolution_ref": None,
            "supersedes": None,
        })

    def resolve(self, contradiction_id: str, resolution_ref: str) -> dict[str, Any]:
        matching = [r for r in self.records if r["payload"].get("contradiction_id") == contradiction_id]
        if not matching or matching[-1]["payload"].get("status") != "OPEN":
            raise ValueError("CONTRADICTION_NOT_OPEN")
        base = matching[-1]
        payload = dict(base["payload"])
        payload.update({
            "observed_at": _now(),
            "status": "RESOLVED",
            "resolution_ref": resolution_ref,
            "supersedes": base["record_hash"],
        })
        return self._log.append(payload)

    def verify(self) -> bool:
        return self._log.verify()
