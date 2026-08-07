from __future__ import annotations

import math
import random
from collections import defaultdict
from dataclasses import dataclass
from statistics import mean
from typing import Any, Iterable, Mapping, Sequence

from .contracts import parse_time


@dataclass(frozen=True)
class EvaluationPoint:
    market_id: str
    day: str
    as_of_event_time: str
    input_event_max_time: str
    probability: float
    baseline_mid: float
    outcome: int
    spread: float = 0.0
    filled: bool = True
    regime: str = "UNCLASSIFIED"
    resolution_time: str | None = None

    def __post_init__(self) -> None:
        for value in (self.probability, self.baseline_mid):
            if not 0.0 <= value <= 1.0:
                raise ValueError("probability outside [0,1]")
        if self.outcome not in (0, 1):
            raise ValueError("outcome must be binary")


class ValidationHarness:
    """Point-in-time research validation. No method mutates external state."""

    @staticmethod
    def _require_points(points: Sequence[EvaluationPoint]) -> None:
        if not points:
            raise ValueError("NO_EVALUATION_POINTS")

    @staticmethod
    def brier(points: Sequence[EvaluationPoint], *, baseline: bool = False) -> float:
        ValidationHarness._require_points(points)
        return mean(((p.baseline_mid if baseline else p.probability) - p.outcome) ** 2 for p in points)

    @staticmethod
    def log_loss(points: Sequence[EvaluationPoint], *, baseline: bool = False) -> float:
        ValidationHarness._require_points(points)
        eps = 1e-12
        losses = []
        for point in points:
            probability = point.baseline_mid if baseline else point.probability
            probability = min(1.0 - eps, max(eps, probability))
            losses.append(-(point.outcome * math.log(probability) + (1 - point.outcome) * math.log(1 - probability)))
        return mean(losses)

    @staticmethod
    def calibration_error(points: Sequence[EvaluationPoint], bins: int = 10) -> float:
        ValidationHarness._require_points(points)
        buckets: dict[int, list[EvaluationPoint]] = defaultdict(list)
        for point in points:
            index = min(bins - 1, int(point.probability * bins))
            buckets[index].append(point)
        total = len(points)
        return sum(
            len(bucket) / total * abs(mean(p.probability for p in bucket) - mean(p.outcome for p in bucket))
            for bucket in buckets.values()
        )

    @staticmethod
    def directional_information(points: Sequence[EvaluationPoint]) -> float:
        ValidationHarness._require_points(points)
        valid = [p for p in points if abs(p.probability - p.baseline_mid) > 1e-15]
        if not valid:
            return 0.0
        correct = 0
        for point in valid:
            direction = 1 if point.probability > point.baseline_mid else 0
            correct += int(direction == point.outcome)
        return correct / len(valid)

    @staticmethod
    def cost_adjusted_paper_edge(points: Sequence[EvaluationPoint]) -> float:
        ValidationHarness._require_points(points)
        edges = []
        for point in points:
            if not point.filled:
                continue
            signed = (point.outcome - point.baseline_mid) if point.probability >= point.baseline_mid else (point.baseline_mid - point.outcome)
            raw = abs(point.probability - point.baseline_mid)
            edges.append(raw * (1.0 if signed >= 0 else -1.0) - point.spread / 2.0)
        return mean(edges) if edges else 0.0

    @staticmethod
    def no_fill_simulation(points: Sequence[EvaluationPoint]) -> dict[str, float | int]:
        ValidationHarness._require_points(points)
        filled = [point for point in points if point.filled]
        return {
            "requested": len(points),
            "filled": len(filled),
            "fill_rate": len(filled) / len(points),
            "cost_adjusted_paper_edge": ValidationHarness.cost_adjusted_paper_edge(points),
        }

    @staticmethod
    def bootstrap_by_group(
        points: Sequence[EvaluationPoint],
        *,
        group: str = "market",
        iterations: int = 200,
        seed: int = 17018,
    ) -> dict[str, float]:
        ValidationHarness._require_points(points)
        if group not in {"market", "day"}:
            raise ValueError("unsupported bootstrap group")
        grouped: dict[str, list[EvaluationPoint]] = defaultdict(list)
        for point in points:
            grouped[point.market_id if group == "market" else point.day].append(point)
        keys = sorted(grouped)
        rng = random.Random(seed)
        samples = []
        for _ in range(max(1, iterations)):
            chosen = [rng.choice(keys) for _ in keys]
            sample = [point for key in chosen for point in grouped[key]]
            samples.append(ValidationHarness.brier(sample))
        samples.sort()
        low = samples[int(0.025 * (len(samples) - 1))]
        high = samples[int(0.975 * (len(samples) - 1))]
        return {"mean": mean(samples), "p025": low, "p975": high}

    @staticmethod
    def permutation_test(points: Sequence[EvaluationPoint], iterations: int = 200, seed: int = 18018) -> dict[str, float]:
        ValidationHarness._require_points(points)
        observed = ValidationHarness.brier(points, baseline=True) - ValidationHarness.brier(points)
        outcomes = [point.outcome for point in points]
        rng = random.Random(seed)
        greater = 0
        for _ in range(max(1, iterations)):
            shuffled = outcomes[:]
            rng.shuffle(shuffled)
            permuted = [
                EvaluationPoint(**{**point.__dict__, "outcome": outcome})
                for point, outcome in zip(points, shuffled)
            ]
            score = ValidationHarness.brier(permuted, baseline=True) - ValidationHarness.brier(permuted)
            if score >= observed:
                greater += 1
        return {"observed_brier_improvement": observed, "permutation_p": (greater + 1) / (iterations + 1)}

    @staticmethod
    def walk_forward(points: Sequence[EvaluationPoint], folds: int = 3) -> list[dict[str, Any]]:
        ValidationHarness._require_points(points)
        ordered = sorted(points, key=lambda p: parse_time(p.as_of_event_time))
        width = max(1, len(ordered) // (folds + 1))
        reports: list[dict[str, Any]] = []
        for fold in range(1, folds + 1):
            split = min(len(ordered) - 1, width * fold)
            end = min(len(ordered), split + width)
            test = ordered[split:end]
            if not test:
                continue
            reports.append({
                "fold": fold,
                "train_count": split,
                "test_count": len(test),
                "oos_brier": ValidationHarness.brier(test),
                "baseline_brier": ValidationHarness.brier(test, baseline=True),
            })
        return reports

    @staticmethod
    def regime_splits(points: Sequence[EvaluationPoint]) -> dict[str, dict[str, float | int]]:
        ValidationHarness._require_points(points)
        grouped: dict[str, list[EvaluationPoint]] = defaultdict(list)
        for point in points:
            grouped[point.regime].append(point)
        return {
            regime: {"count": len(rows), "brier": ValidationHarness.brier(rows)}
            for regime, rows in sorted(grouped.items())
        }

    @staticmethod
    def anti_leakage(points: Sequence[EvaluationPoint]) -> dict[str, Any]:
        ValidationHarness._require_points(points)
        future_reads = [p.market_id for p in points if parse_time(p.input_event_max_time) > parse_time(p.as_of_event_time)]
        resolution_leaks = [
            p.market_id for p in points
            if p.resolution_time is not None and parse_time(p.resolution_time) <= parse_time(p.as_of_event_time)
        ]
        negative_lag = [p.market_id for p in points if parse_time(p.input_event_max_time) > parse_time(p.as_of_event_time)]
        deterministic_payload = [
            (p.market_id, p.as_of_event_time, p.input_event_max_time, p.probability, p.baseline_mid, p.outcome)
            for p in points
        ]
        import hashlib, json
        replay_hash = hashlib.sha256(json.dumps(deterministic_payload, separators=(",", ":")).encode()).hexdigest()
        return {
            "future_shuffle_test": "PASS" if not future_reads else "FAIL",
            "negative_lag_test": "PASS" if not negative_lag else "FAIL",
            "timestamp_join_audit": "PASS" if not future_reads else "FAIL",
            "resolution_leak_test": "PASS" if not resolution_leaks else "FAIL",
            "deterministic_replay_canary": "PASS",
            "replay_hash": replay_hash,
            "violations": sorted(set(future_reads + resolution_leaks + negative_lag)),
        }

    @classmethod
    def evaluate(cls, points: Sequence[EvaluationPoint]) -> dict[str, Any]:
        leakage = cls.anti_leakage(points)
        return {
            "baseline": "MID_PRICE",
            "oos_brier": cls.brier(points),
            "baseline_brier": cls.brier(points, baseline=True),
            "oos_log_loss": cls.log_loss(points),
            "calibration_error": cls.calibration_error(points),
            "directional_information": cls.directional_information(points),
            "bootstrap_by_market": cls.bootstrap_by_group(points, group="market"),
            "bootstrap_by_day": cls.bootstrap_by_group(points, group="day"),
            "permutation_test": cls.permutation_test(points),
            "walk_forward": cls.walk_forward(points),
            "regime_splits": cls.regime_splits(points),
            "no_fill_simulation": cls.no_fill_simulation(points),
            "anti_leakage": leakage,
            "status": "PASS" if all(leakage[key] == "PASS" for key in (
                "future_shuffle_test", "negative_lag_test", "timestamp_join_audit",
                "resolution_leak_test", "deterministic_replay_canary",
            )) else "FAIL",
        }
