"""Authoritative paper fee contract bound to public Polymarket market info.

The contract compares the current public fee documentation with the pinned
official V2 SDK implementation.  If those two official sources disagree for a
fee-enabled market, execution fails closed with ``FEE_MODEL_UNVERIFIED``.
No authenticated endpoint, wallet, signing, or order submission is reachable.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping

FEE_MODEL_VERSION = "POLYMARKET_PUBLIC_FEE_CONFORMANCE_V1"
DOCS_FORMULA_VERSION = "DOCS_C_X_RATE_X_P_X_ONE_MINUS_P_2026-08"
SDK_FORMULA_VERSION = "PY_CLOB_CLIENT_V2_1.0.1_394ECC1"
OFFICIAL_REFERENCE = {
    "documentation": "https://docs.polymarket.com/trading/fees",
    "market_info": "https://docs.polymarket.com/api-reference/markets/get-clob-market-info",
    "sdk": "https://github.com/Polymarket/py-clob-client-v2",
    "sdk_release": "v1.0.1",
    "sdk_commit": "394ecc18ab9ab20b48095b0b5c5de0042bdd6bb3",
    "sdk_fee_file_blob": "6f2c7c6441e7f8455a32e8c4fb1f9455e567729d",
}
FEE_QUANTUM = Decimal("0.00001")


class FeeModelError(ValueError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def _decimal(value: Any, *, field: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise FeeModelError(f"INVALID_{field.upper()}") from exc
    if not result.is_finite():
        raise FeeModelError(f"INVALID_{field.upper()}")
    return result


def _round_fee(value: Decimal) -> Decimal:
    rounded = value.quantize(FEE_QUANTUM, rounding=ROUND_HALF_UP)
    return Decimal("0") if rounded < FEE_QUANTUM else rounded


def docs_fee(*, shares: Decimal, price: Decimal, fee_rate: Decimal) -> Decimal:
    """Current public documentation: C × feeRate × p × (1-p)."""
    return _round_fee(shares * fee_rate * price * (Decimal("1") - price))


def pinned_sdk_fee(*, shares: Decimal, price: Decimal, fee_rate: Decimal, exponent: int) -> Decimal:
    """Pinned official SDK v1.0.1 semantics reduced to share-denominated fee.

    ``adjust_buy_amount_for_fees`` calculates
    ``(amount / price) * fee_rate * (p*(1-p))**exponent``.  Since
    ``amount/price`` equals shares, this is the equivalent fee in USDC for the
    same trade vector.
    """
    return _round_fee(shares * fee_rate * (price * (Decimal("1") - price)) ** exponent)


@dataclass(frozen=True)
class FeeSchedule:
    condition_id: str
    fee_rate: Decimal
    fee_enabled: bool
    taker_only: bool
    exponent: int
    itode: bool | None
    raw_schedule: Mapping[str, Any]
    raw_schedule_hash: str
    source_evidence_hash: str
    conformance_status: str
    conformance_reason: str
    model_version: str = FEE_MODEL_VERSION
    source: str = "PUBLIC_GET_CLOB_MARKET_INFO"

    @classmethod
    def from_market_info(
        cls,
        *,
        condition_id: str,
        payload: Mapping[str, Any],
        source_evidence_hash: str,
    ) -> "FeeSchedule":
        fee_details = payload.get("fd")
        if not isinstance(fee_details, Mapping):
            raise FeeModelError("FEE_MODEL_UNVERIFIED")
        if "r" not in fee_details or "to" not in fee_details or "e" not in fee_details:
            raise FeeModelError("FEE_MODEL_UNVERIFIED")
        rate = _decimal(fee_details.get("r"), field="fee_rate")
        if rate < 0 or rate > 1:
            raise FeeModelError("FEE_MODEL_UNVERIFIED")
        taker_only = fee_details.get("to")
        if not isinstance(taker_only, bool):
            raise FeeModelError("FEE_MODEL_UNVERIFIED")
        try:
            exponent = int(fee_details.get("e"))
        except (TypeError, ValueError) as exc:
            raise FeeModelError("FEE_MODEL_UNVERIFIED") from exc
        if exponent < 0 or exponent > 8:
            raise FeeModelError("FEE_MODEL_UNVERIFIED")
        if rate > 0 and not taker_only:
            raise FeeModelError("FEE_MODEL_UNVERIFIED")
        raw = dict(fee_details)
        raw_hash = hashlib.sha256(_canonical(raw)).hexdigest()
        itode = payload.get("itode")
        if itode is not None and not isinstance(itode, bool):
            raise FeeModelError("FEE_MODEL_UNVERIFIED")

        if rate == 0:
            status = "VERIFIED_FEE_DISABLED"
            reason = "BOTH_OFFICIAL_FORMULAS_RESOLVE_TO_ZERO"
        elif exponent == 1:
            status = "VERIFIED_OFFICIAL_SOURCES_AGREE"
            reason = "DOCS_AND_PINNED_SDK_FORMULAS_EQUIVALENT"
        else:
            status = "UNVERIFIED_OFFICIAL_SOURCE_CONFLICT"
            reason = "DOCS_FORMULA_OMITS_SDK_EXPONENT"

        return cls(
            condition_id=str(condition_id),
            fee_rate=rate,
            fee_enabled=rate > 0,
            taker_only=taker_only,
            exponent=exponent,
            itode=itode,
            raw_schedule=raw,
            raw_schedule_hash=raw_hash,
            source_evidence_hash=str(source_evidence_hash),
            conformance_status=status,
            conformance_reason=reason,
        )

    @classmethod
    def deterministic_fixture(
        cls,
        *,
        condition_id: str = "fixture-condition",
        fee_rate: str = "0.07",
        enabled: bool = True,
        itode: bool = True,
        exponent: int = 1,
    ) -> "FeeSchedule":
        raw = {"r": fee_rate if enabled else "0", "e": exponent, "to": True}
        return cls.from_market_info(
            condition_id=condition_id,
            payload={"fd": raw, "itode": itode},
            source_evidence_hash=hashlib.sha256(_canonical({"fixture": raw, "itode": itode})).hexdigest(),
        )

    @property
    def verified(self) -> bool:
        return self.conformance_status in {
            "VERIFIED_FEE_DISABLED",
            "VERIFIED_OFFICIAL_SOURCES_AGREE",
        }

    def to_evidence(self) -> dict[str, Any]:
        return {
            "condition_id": self.condition_id,
            "fee_rate": format(self.fee_rate, "f"),
            "fee_enabled": self.fee_enabled,
            "taker_only": self.taker_only,
            "exponent": self.exponent,
            "itode": self.itode,
            "raw_schedule": dict(self.raw_schedule),
            "raw_schedule_hash": self.raw_schedule_hash,
            "source_evidence_hash": self.source_evidence_hash,
            "model_version": self.model_version,
            "source": self.source,
            "conformance_status": self.conformance_status,
            "conformance_reason": self.conformance_reason,
            "official_reference": dict(OFFICIAL_REFERENCE),
            "docs_formula_version": DOCS_FORMULA_VERSION,
            "sdk_formula_version": SDK_FORMULA_VERSION,
        }


@dataclass(frozen=True)
class FeeResult:
    fee_usd: Decimal
    docs_fee_usd: Decimal
    sdk_fee_usd: Decimal
    shares: Decimal
    price: Decimal
    liquidity_classification: str
    model_version: str
    schedule_hash: str
    fee_enabled: bool
    conformance_status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "fee_usd": format(self.fee_usd, "f"),
            "docs_fee_usd": format(self.docs_fee_usd, "f"),
            "sdk_fee_usd": format(self.sdk_fee_usd, "f"),
            "shares": format(self.shares, "f"),
            "price": format(self.price, "f"),
            "liquidity_classification": self.liquidity_classification,
            "model_version": self.model_version,
            "schedule_hash": self.schedule_hash,
            "fee_enabled": self.fee_enabled,
            "conformance_status": self.conformance_status,
        }


def calculate_fee(
    *,
    shares: Decimal | str | float,
    price: Decimal | str | float,
    schedule: FeeSchedule,
    liquidity_classification: str = "TAKER",
) -> FeeResult:
    quantity = _decimal(shares, field="shares")
    p = _decimal(price, field="price")
    if quantity < 0 or p <= 0 or p >= 1:
        raise FeeModelError("INVALID_FEE_INPUT")
    classification = str(liquidity_classification).upper()
    if classification not in {"TAKER", "MAKER"}:
        raise FeeModelError("FEE_MODEL_UNVERIFIED")
    if classification == "MAKER" or not schedule.fee_enabled:
        docs = sdk = selected = Decimal("0")
    else:
        if not schedule.taker_only or not schedule.verified:
            raise FeeModelError("FEE_MODEL_UNVERIFIED")
        docs = docs_fee(shares=quantity, price=p, fee_rate=schedule.fee_rate)
        sdk = pinned_sdk_fee(
            shares=quantity,
            price=p,
            fee_rate=schedule.fee_rate,
            exponent=schedule.exponent,
        )
        if docs != sdk:
            raise FeeModelError("FEE_MODEL_UNVERIFIED")
        selected = docs
    return FeeResult(
        fee_usd=selected,
        docs_fee_usd=docs,
        sdk_fee_usd=sdk,
        shares=quantity,
        price=p,
        liquidity_classification=classification,
        model_version=schedule.model_version,
        schedule_hash=schedule.raw_schedule_hash,
        fee_enabled=schedule.fee_enabled,
        conformance_status=schedule.conformance_status,
    )


def official_conformance_vectors() -> list[dict[str, str]]:
    return [
        {"category": "CRYPTO", "rate": "0.07", "shares": "100", "price": "0.50", "docs_expected": "1.75000"},
        {"category": "CRYPTO", "rate": "0.07", "shares": "100", "price": "0.30", "docs_expected": "1.47000"},
        {"category": "SPORTS", "rate": "0.05", "shares": "100", "price": "0.50", "docs_expected": "1.25000"},
        {"category": "FINANCE", "rate": "0.04", "shares": "100", "price": "0.90", "docs_expected": "0.36000"},
        {"category": "MINIMUM", "rate": "0.04", "shares": "0.0001", "price": "0.50", "docs_expected": "0.00000"},
    ]


def run_official_conformance() -> dict[str, Any]:
    results: list[dict[str, Any]] = []
    for vector in official_conformance_vectors():
        shares = Decimal(vector["shares"])
        price = Decimal(vector["price"])
        rate = Decimal(vector["rate"])
        docs_observed = docs_fee(shares=shares, price=price, fee_rate=rate)
        sdk_exponent_one = pinned_sdk_fee(shares=shares, price=price, fee_rate=rate, exponent=1)
        sdk_exponent_two = pinned_sdk_fee(shares=shares, price=price, fee_rate=rate, exponent=2)
        results.append({
            **vector,
            "docs_observed": format(docs_observed, "f"),
            "sdk_exponent_one": format(sdk_exponent_one, "f"),
            "sdk_exponent_two": format(sdk_exponent_two, "f"),
            "docs_vector_pass": docs_observed == Decimal(vector["docs_expected"]),
            "docs_sdk_e1_agree": docs_observed == sdk_exponent_one,
            "docs_sdk_e2_conflict": docs_observed != sdk_exponent_two,
        })
    conflict_schedule = FeeSchedule.deterministic_fixture(exponent=2)
    fail_closed = False
    try:
        calculate_fee(shares="100", price="0.5", schedule=conflict_schedule)
    except FeeModelError as exc:
        fail_closed = exc.reason == "FEE_MODEL_UNVERIFIED"
    docs_and_sdk_e1_pass = all(
        item["docs_vector_pass"] and item["docs_sdk_e1_agree"]
        for item in results
    )
    material_e2_conflict_detected = any(
        item["docs_sdk_e2_conflict"] and Decimal(item["docs_observed"]) > 0
        for item in results
    )
    result = docs_and_sdk_e1_pass and material_e2_conflict_detected and fail_closed
    return {
        "model_version": FEE_MODEL_VERSION,
        "docs_formula_version": DOCS_FORMULA_VERSION,
        "sdk_formula_version": SDK_FORMULA_VERSION,
        "official_reference": dict(OFFICIAL_REFERENCE),
        "vectors": results,
        "official_source_conflict_detected": True,
        "conflict_behavior": "ABSTAIN_FEE_MODEL_UNVERIFIED",
        "fail_closed_verified": fail_closed,
        "result": "PASS" if result else "FAIL",
    }
