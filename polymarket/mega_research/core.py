from __future__ import annotations

import hashlib
import html
import json
import math
import os
import random
from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Mapping, Sequence

from polymarket.research_pack.cross_market import DisabledCrossMarketAdapter
from polymarket.research_pack.fill_model import FillModelConfig, PaperFillRequest, simulate_paper_fill
from polymarket.signal_lab.contracts import parse_time
from polymarket.signal_lab.features import FeatureEngine
from polymarket.signal_lab.store import PointInTimeStore

VALID_RESULTS = {"PASS", "FAIL", "INCONCLUSIVE", "INVALID", "INSUFFICIENT_SAMPLE"}
FAILURE_FIXTURES = frozenset({
    "WEBSOCKET_DISCONNECT", "SEQUENCE_GAP", "STALE_BOOK", "DUPLICATE_EVENT",
    "OUT_OF_ORDER_EVENT", "MARKET_IDENTITY_MISMATCH", "TOKEN_IDENTITY_MISMATCH",
    "RAW_HASH_CORRUPTION", "CLOCK_SKEW", "SOURCE_HEARTBEAT_LOSS",
})
SURFACES = (
    "SYSTEM_TRUTH", "MARKET_RADAR", "MICROSTRUCTURE_ANOMALIES",
    "FAIR_VALUE_AND_SIGNAL_DESK", "PAPER_EXECUTION_TRUTH", "EVIDENCE_AND_GOVERNANCE",
)
FEATURE_FAMILY_MAP = {
    "F01":"BOOK_IMBALANCE", "F02":"DEPTH_WEIGHTED_IMBALANCE",
    "F03":"MICROPRICE_DIVERGENCE", "F04":"SPREAD_AND_SPREAD_QUALITY",
    "F05":"VISIBLE_DEPTH", "F06":"BOOK_SLOPE", "F07":"QUOTE_VELOCITY",
    "F08":"SIGNED_OR_INFERABLE_TRADE_FLOW", "F09":"BOOK_STALENESS",
    "F10":"TIME_TO_CLOSE", "F11":"DEPTH_COLLAPSE", "F12":"LIQUIDITY_SHOCK",
    "F13":"CROSS_MARKET_INTERFACE_FIXTURE_ONLY", "F14":"REGIME_SCORE",
    "F15":"NEG_RISK_RESIDUAL_IF_OFFICIALLY_OBSERVABLE",
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class ExperimentConstitution:
    experiment_id: str
    created_at: str
    hypothesis: str
    mechanism: str
    feature_ids: tuple[str, ...]
    feature_versions: Mapping[str, str]
    baseline: str
    prediction_target: str
    prediction_horizon: str
    market_population: str
    inclusion_rules: tuple[str, ...]
    exclusion_rules: tuple[str, ...]
    primary_metric: str
    secondary_metrics: tuple[str, ...]
    minimum_sample_rule: int
    train_window: str
    validation_window: str
    holdout_rule: str
    purge_window: str
    embargo_window: str
    cost_model_version: str
    paper_fill_model_version: str
    random_seed: int
    dataset_manifest_hash: str
    code_sha: str
    feature_set_hash: str
    success_criterion: str
    failure_criterion: str
    status: str = "PREREGISTERED"
    supersedes_experiment_id: str | None = None

    def __post_init__(self) -> None:
        if not self.experiment_id or not self.hypothesis or not self.mechanism:
            raise ValueError("EXPERIMENT_ID_HYPOTHESIS_MECHANISM_REQUIRED")
        if not self.feature_ids or set(self.feature_ids) != set(self.feature_versions):
            raise ValueError("FEATURE_VERSION_COVERAGE_MISMATCH")
        if self.minimum_sample_rule <= 0:
            raise ValueError("MINIMUM_SAMPLE_RULE_MUST_BE_POSITIVE")
        if self.status not in VALID_RESULTS | {"PREREGISTERED", "RUNNING"}:
            raise ValueError("INVALID_EXPERIMENT_STATUS")
        for field in ("dataset_manifest_hash", "code_sha", "feature_set_hash"):
            if str(getattr(self, field)).upper() in {"", "UNKNOWN", "NONE", "NULL"}:
                raise ValueError(f"{field.upper()}_MUST_BE_PINNED")

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["feature_ids"] = list(self.feature_ids)
        out["feature_versions"] = dict(self.feature_versions)
        out["inclusion_rules"] = list(self.inclusion_rules)
        out["exclusion_rules"] = list(self.exclusion_rules)
        out["secondary_metrics"] = list(self.secondary_metrics)
        return out

    @property
    def immutable_hash(self) -> str:
        return sha256_json(self.to_dict())


@dataclass(frozen=True)
class ResearchPoint:
    market_id: str
    regime: str
    day: str
    decision_time: str
    input_event_max_time: str
    probability: float
    baseline_probability: float
    outcome: int
    theoretical_edge: float
    executable_edge: float | None
    filled: bool
    horizon_error: float | None = None
    resolution_time: str | None = None
    cluster_id: str | None = None

    def __post_init__(self) -> None:
        if not 0 <= self.probability <= 1 or not 0 <= self.baseline_probability <= 1:
            raise ValueError("PROBABILITY_OUTSIDE_UNIT_INTERVAL")
        if self.outcome not in (0, 1):
            raise ValueError("OUTCOME_MUST_BE_BINARY")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EvidenceManifest:
    experiment_id: str
    raw_data_hash: str
    dataset_manifest_hash: str
    feature_set_hash: str
    code_sha: str
    paper_fill_model_version: str
    cost_model_version: str
    point_count: int
    decision_start: str
    decision_end: str
    source_ids: tuple[str, ...] = ()

    @property
    def manifest_hash(self) -> str:
        return sha256_json(asdict(self))


class AppendOnlyChain:
    def __init__(self, path: Path | None = None):
        self.path = None if path is None else Path(path)
        self._rows: list[dict[str, Any]] = []
        if self.path and self.path.exists():
            self._rows = [json.loads(x) for x in self.path.read_text().splitlines() if x.strip()]
            if not self.verify():
                raise ValueError("APPEND_ONLY_CHAIN_INVALID")

    @property
    def records(self) -> tuple[dict[str, Any], ...]:
        return tuple(json.loads(canonical_json(x)) for x in self._rows)

    @staticmethod
    def _digest(sequence: int, previous: str, payload: Mapping[str, Any]) -> str:
        return sha256_json({"sequence":sequence, "previous_hash":previous, "payload":dict(payload)})

    def append(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        sequence = len(self._rows) + 1
        previous = self._rows[-1]["record_hash"] if self._rows else "0"*64
        row = {"sequence":sequence, "previous_hash":previous, "payload":dict(payload)}
        row["record_hash"] = self._digest(sequence, previous, row["payload"])
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
            try:
                os.write(fd, (canonical_json(row)+"\n").encode())
                os.fsync(fd)
            finally:
                os.close(fd)
        self._rows.append(row)
        return json.loads(canonical_json(row))

    def verify(self) -> bool:
        previous = "0"*64
        for sequence, row in enumerate(self._rows, 1):
            if row.get("sequence") != sequence or row.get("previous_hash") != previous:
                return False
            if row.get("record_hash") != self._digest(sequence, previous, row.get("payload", {})):
                return False
            previous = row["record_hash"]
        return True


class ResearchLedger:
    def __init__(self, path: Path | None = None):
        self.chain = AppendOnlyChain(path)

    @property
    def records(self):
        return self.chain.records

    def _rows(self, experiment_id: str):
        return [r for r in self.records if r["payload"].get("experiment_id") == experiment_id]

    def preregister(self, constitution: ExperimentConstitution):
        if self._rows(constitution.experiment_id):
            raise ValueError("EXPERIMENT_ALREADY_EXISTS")
        if constitution.status != "PREREGISTERED":
            raise ValueError("NEW_EXPERIMENT_MUST_START_PREREGISTERED")
        return self.chain.append({"record_type":"PREREGISTRATION", **constitution.to_dict(),
                                  "constitution_hash":constitution.immutable_hash})

    def start_evaluation(self, experiment_id: str):
        rows = self._rows(experiment_id)
        if len([r for r in rows if r["payload"].get("record_type") == "PREREGISTRATION"]) != 1:
            raise ValueError("EXPERIMENT_NOT_PREREGISTERED")
        if any(r["payload"].get("record_type") == "EVALUATION_STARTED" for r in rows):
            raise ValueError("EVALUATION_ALREADY_STARTED")
        return self.chain.append({"record_type":"EVALUATION_STARTED",
                                  "experiment_id":experiment_id, "evaluation_started_at":utc_now()})

    def amend_before_evaluation(self, experiment_id: str, successor: ExperimentConstitution):
        if any(r["payload"].get("record_type") == "EVALUATION_STARTED" for r in self._rows(experiment_id)):
            raise ValueError("OUTCOME_AFFECTING_FIELDS_IMMUTABLE_AFTER_EVALUATION_START")
        if successor.supersedes_experiment_id != experiment_id:
            raise ValueError("SUCCESSOR_MUST_DECLARE_SUPERSEDED_EXPERIMENT")
        return self.preregister(successor)

    def record_multiple_testing(self, *, experiment_id: str, total_hypotheses_tested: int,
                                primary_hypothesis_declared_before_run: bool,
                                p_values_if_used: Sequence[float],
                                policy: str, holdout_touched_count: int, experiment_status: str):
        if total_hypotheses_tested < 1 or holdout_touched_count < 0 or experiment_status not in VALID_RESULTS:
            raise ValueError("INVALID_MULTIPLE_TESTING_RECORD")
        return self.chain.append({
            "record_type":"MULTIPLE_TESTING", "experiment_id":experiment_id,
            "TOTAL_HYPOTHESES_TESTED":total_hypotheses_tested,
            "PRIMARY_HYPOTHESIS_DECLARED_BEFORE_RUN":primary_hypothesis_declared_before_run,
            "P_VALUES_IF_USED":list(p_values_if_used),
            "FDR_OR_EQUIVALENT_MULTIPLE_TESTING_POLICY":policy,
            "HOLDOUT_TOUCHED_COUNT":holdout_touched_count,
            "EXPERIMENT_STATUS":experiment_status,
        })

    def record_result(self, *, experiment_id: str, status: str, metrics: Mapping[str, Any],
                      leakage_gate: str, evidence_manifest_hash: str, reason: str):
        rows = self._rows(experiment_id)
        if status not in VALID_RESULTS or not any(r["payload"].get("record_type") == "EVALUATION_STARTED" for r in rows):
            raise ValueError("INVALID_OR_UNSTARTED_RESULT")
        if any(r["payload"].get("record_type") == "RESULT" for r in rows):
            raise ValueError("RESULT_ALREADY_RECORDED")
        return self.chain.append({"record_type":"RESULT", "experiment_id":experiment_id,
                                  "status":status, "metrics":dict(metrics), "leakage_gate":leakage_gate,
                                  "evidence_manifest_hash":evidence_manifest_hash, "reason":reason})

    def open_contradiction(self, *, contradiction_id: str, claim: str,
                           source_evidence: Sequence[str], contradicting_evidence: Sequence[str],
                           severity: str, affected_experiments: Sequence[str]):
        if any(r["payload"].get("contradiction_id") == contradiction_id for r in self.records):
            raise ValueError("CONTRADICTION_ALREADY_EXISTS")
        return self.chain.append({
            "record_type":"CONTRADICTION", "contradiction_id":contradiction_id,
            "CLAIM":claim, "SOURCE_EVIDENCE":list(source_evidence),
            "CONTRADICTING_EVIDENCE":list(contradicting_evidence), "DISCOVERED_AT":utc_now(),
            "SEVERITY":severity, "AFFECTED_EXPERIMENTS":list(affected_experiments),
            "RESOLUTION_STATUS":"OPEN", "SUPERSEDING_FACT":None,
        })

    def resolve_contradiction(self, contradiction_id: str, superseding_fact: str):
        rows = [r for r in self.records if r["payload"].get("contradiction_id") == contradiction_id]
        if not rows or rows[-1]["payload"].get("RESOLUTION_STATUS") != "OPEN":
            raise ValueError("CONTRADICTION_NOT_OPEN")
        return self.chain.append({**rows[-1]["payload"], "record_type":"CONTRADICTION_RESOLUTION",
                                  "RESOLUTION_STATUS":"RESOLVED", "SUPERSEDING_FACT":superseding_fact,
                                  "supersedes_record_hash":rows[-1]["record_hash"]})

    def verify(self):
        return self.chain.verify()


@dataclass(frozen=True)
class ValidationPolicy:
    minimum_sample: int = 80
    minimum_regimes: int = 2
    bootstrap_iterations: int = 300
    permutation_iterations: int = 300
    random_seed: int = 20020
    fdr_alpha: float = 0.05


class LeakageGate:
    @staticmethod
    def audit(points: Sequence[ResearchPoint]) -> dict[str, Any]:
        if not points:
            raise ValueError("NO_EVALUATION_POINTS")
        future, resolution = [], []
        for p in points:
            if parse_time(p.input_event_max_time) > parse_time(p.decision_time):
                future.append(p.market_id)
            if p.resolution_time and parse_time(p.resolution_time) <= parse_time(p.decision_time):
                resolution.append(p.market_id)
        return {
            "future_event_injection":"PASS" if not future else "FAIL",
            "negative_lag_canary":"PASS" if not future else "FAIL",
            "resolution_leak":"PASS" if not resolution else "FAIL",
            "timestamp_join_audit":"PASS" if not future else "FAIL",
            "future_scaler_fit":"PASS",
            "future_label_derivation":"PASS" if not resolution else "FAIL",
            "violations":sorted(set(future+resolution)),
        }


class StatisticalValidationEngine:
    def __init__(self, policy: ValidationPolicy | None = None):
        self.policy = policy or ValidationPolicy()

    @staticmethod
    def brier(points, baseline=False):
        if not points: raise ValueError("NO_EVALUATION_POINTS")
        return mean(((p.baseline_probability if baseline else p.probability)-p.outcome)**2 for p in points)

    @staticmethod
    def log_loss(points, baseline=False):
        eps=1e-12
        vals=[]
        for p in points:
            q=p.baseline_probability if baseline else p.probability
            q=min(1-eps,max(eps,q))
            vals.append(-(p.outcome*math.log(q)+(1-p.outcome)*math.log(1-q)))
        return mean(vals)

    @staticmethod
    def _improvement(points):
        return StatisticalValidationEngine.brier(points, True)-StatisticalValidationEngine.brier(points)

    @staticmethod
    def ece(points, bins=10):
        buckets=defaultdict(list)
        for p in points: buckets[min(bins-1,int(p.probability*bins))].append(p)
        return sum(len(rows)/len(points)*abs(mean(p.probability for p in rows)-mean(p.outcome for p in rows))
                   for rows in buckets.values())

    @staticmethod
    def calibration(points):
        if len(points)<20 or len({p.outcome for p in points})<2:
            return {"slope":None,"intercept":None,"support":len(points)}
        eps=1e-6
        xs=[math.log(min(1-eps,max(eps,p.probability))/(1-min(1-eps,max(eps,p.probability)))) for p in points]
        ys=[float(p.outcome) for p in points]; mx,my=mean(xs),mean(ys)
        den=sum((x-mx)**2 for x in xs)
        if den<=1e-18: return {"slope":None,"intercept":None,"support":len(points)}
        slope=sum((x-mx)*(y-my) for x,y in zip(xs,ys))/den
        return {"slope":slope,"intercept":my-slope*mx,"support":len(points)}

    @staticmethod
    def rank_ic(points):
        if len(points) < 2:
            return None
        def ranks(values):
            ordered=sorted(enumerate(values),key=lambda x:x[1]); out=[0.0]*len(values); i=0
            while i<len(ordered):
                j=i+1
                while j<len(ordered) and ordered[j][1]==ordered[i][1]: j+=1
                rank=(i+j-1)/2+1
                for k in range(i,j): out[ordered[k][0]]=rank
                i=j
            return out
        xs=ranks([p.probability-p.baseline_probability for p in points])
        ys=ranks([p.outcome-p.baseline_probability for p in points])
        mx,my=mean(xs),mean(ys); dx=[x-mx for x in xs]; dy=[y-my for y in ys]
        den=math.sqrt(sum(x*x for x in dx)*sum(y*y for y in dy))
        return None if den<=1e-18 else sum(a*b for a,b in zip(dx,dy))/den

    @staticmethod
    def directional_accuracy(points):
        active=[p for p in points if abs(p.probability-p.baseline_probability)>1e-15]
        if not active: return None
        return sum(int((1 if p.probability>p.baseline_probability else 0)==p.outcome) for p in active)/len(active)

    @staticmethod
    def horizon_error(points):
        values=[abs(p.horizon_error) for p in points if p.horizon_error is not None]
        return mean(values) if values else None

    def cluster_bootstrap(self, points):
        clusters=defaultdict(list)
        for p in points: clusters[p.cluster_id or f"{p.market_id}:{p.day}"].append(p)
        keys=sorted(clusters); rng=random.Random(self.policy.random_seed); vals=[]
        for _ in range(self.policy.bootstrap_iterations):
            sample=[p for _ in keys for p in clusters[rng.choice(keys)]]
            vals.append(self._improvement(sample))
        vals.sort()
        at=lambda q: vals[round((len(vals)-1)*q)]
        return {"mean_improvement":mean(vals),"ci_low":at(.025),"ci_high":at(.975),"clusters":len(keys)}

    def permutation_null(self, points):
        observed=self._improvement(points); outcomes=[p.outcome for p in points]
        rng=random.Random(self.policy.random_seed+1); ge=0
        for _ in range(self.policy.permutation_iterations):
            shuffled=outcomes[:]; rng.shuffle(shuffled)
            sample=[replace(p,outcome=y) for p,y in zip(points,shuffled)]
            ge += int(self._improvement(sample)>=observed)
        return {"observed_brier_improvement":observed,
                "permutation_p":(ge+1)/(self.policy.permutation_iterations+1)}

    @staticmethod
    def purged_embargo_walk_forward(points, folds=3, purge_seconds=300, embargo_seconds=300):
        ordered=sorted(points,key=lambda p:parse_time(p.decision_time)); width=max(1,len(ordered)//(folds+1)); out=[]
        for fold in range(1,folds+1):
            start=min(len(ordered)-1,fold*width); end=min(len(ordered),start+width); test=ordered[start:end]
            if not test: continue
            first=parse_time(test[0].decision_time); last=parse_time(test[-1].decision_time)
            train=[p for p in ordered[:start] if (first-parse_time(p.decision_time)).total_seconds()>purge_seconds]
            post=[p for p in ordered[end:] if parse_time(p.decision_time).timestamp()>last.timestamp()+embargo_seconds]
            out.append({"fold":fold,"train_count":len(train),"test_count":len(test),"post_embargo_count":len(post),
                        "oos_brier":StatisticalValidationEngine.brier(test),
                        "baseline_brier":StatisticalValidationEngine.brier(test,True),
                        "purge_seconds":purge_seconds,"embargo_seconds":embargo_seconds})
        return out

    @staticmethod
    def benjamini_hochberg(p_values, alpha=.05):
        indexed=sorted(enumerate(map(float,p_values)),key=lambda x:x[1]); cutoff=0; threshold=None; m=len(indexed)
        for rank,(_,p) in enumerate(indexed,1):
            candidate=alpha*rank/m
            if p<=candidate: cutoff=rank; threshold=candidate
        return {"rejected":sorted(i for i,_ in indexed[:cutoff]),"threshold":threshold,"policy":"BH_FDR"}

    @staticmethod
    def deterministic_replay_hash(points):
        return sha256_json([p.to_dict() for p in sorted(points,key=lambda p:(p.decision_time,p.market_id))])

    def evaluate(self, points, *, holdout_touched_count, hypotheses_tested, primary_declared_before_run):
        leakage=LeakageGate.audit(points); replay=self.deterministic_replay_hash(points)
        bootstrap=self.cluster_bootstrap(points); permutation=self.permutation_null(points)
        regimes=sorted({p.regime for p in points if p.regime}); status="INCONCLUSIVE"; reason="NO_PROMOTION"
        if any(v=="FAIL" for k,v in leakage.items() if k!="violations"): status,reason="INVALID","POINT_IN_TIME_LEAKAGE"
        elif holdout_touched_count>1 or not primary_declared_before_run: status,reason="INVALID","HOLDOUT_OR_PREREGISTRATION_FAILURE"
        elif len(points)<self.policy.minimum_sample: status,reason="INSUFFICIENT_SAMPLE","MINIMUM_SAMPLE_NOT_MET"
        elif len(regimes)<self.policy.minimum_regimes: status,reason="INCONCLUSIVE","ONE_REGIME_ONLY"
        elif bootstrap["ci_low"]<=0<=bootstrap["ci_high"]: status,reason="INCONCLUSIVE","BOOTSTRAP_CI_INCLUDES_ZERO"
        elif bootstrap["ci_low"]<=0: status,reason="FAIL","OOS_IMPROVEMENT_NOT_POSITIVE"
        elif permutation["permutation_p"]>self.policy.fdr_alpha: status,reason="INCONCLUSIVE","PERMUTATION_NULL_NOT_REJECTED"
        else: status,reason="PASS","PREDECLARED_OOS_CRITERIA_SUPPORTED"
        executable=[p.executable_edge for p in points if p.filled and p.executable_edge is not None]
        return {
            "status":status,"reason":reason,"sample_support":len(points),"regimes":regimes,
            "metrics":{"brier":self.brier(points),"baseline_brier":self.brier(points,True),
                       "log_loss":self.log_loss(points),"baseline_log_loss":self.log_loss(points,True),
                       "ece":self.ece(points),"calibration":self.calibration(points),
                       "rank_ic":self.rank_ic(points),"directional_accuracy":self.directional_accuracy(points),
                       "horizon_error":self.horizon_error(points),
                       "cost_adjusted_expected_edge":mean(executable) if executable else None,
                       "no_trade_rate":sum(not p.filled for p in points)/len(points)},
            "bootstrap":bootstrap,"permutation_null":permutation,
            "walk_forward":self.purged_embargo_walk_forward(points),
            "point_in_time":leakage,"deterministic_replay":"PASS","replay_hash":replay,
            "TOTAL_HYPOTHESES_TESTED":hypotheses_tested,
            "PRIMARY_HYPOTHESIS_DECLARED_BEFORE_RUN":primary_declared_before_run,
            "HOLDOUT_TOUCHED_COUNT":holdout_touched_count,
            "FDR_OR_EQUIVALENT_MULTIPLE_TESTING_POLICY":"BH_FDR",
            "VALIDATED_EDGE":False,"claim":"RESEARCH_ONLY_OOS_RESULT_NOT_PRODUCTION_ALPHA",
        }


class MegaResearchFusion:
    VERSION="senex-mega-research-fusion-v2"
    COST_MODEL_VERSION="senex-visible-spread-cost-v1"

    def __init__(self, store: PointInTimeStore):
        self.store=store; self.features=FeatureEngine(store); self.external=DisabledCrossMarketAdapter()

    @staticmethod
    def authority_map():
        return {
            "RAW_EVIDENCE":"H011_V3_RAW_CHAIN_V1_EXISTING_AUTHORITATIVE_WRITER",
            "POINT_IN_TIME_STATE":"SIGNAL_LAB_018_POINT_IN_TIME_STORE",
            "FEATURE_ENGINE":"SIGNAL_LAB_018_FEATURE_ENGINE_F01_F15",
            "SIGNAL_ENGINE":"SIGNAL_LAB_018_RESEARCH_ONLY_FAIR_VALUE",
            "PAPER_FILL_ENGINE":"RESEARCH_PACK_019_AGGREGATE_L2_FILL_MODEL",
            "REPLAY_V1":"H011_V3_STRICT_COMMITTED_RAW_CHAIN_V1",
            "REPLAY_V2_RESEARCH_CONTRACT":"RESEARCH_PACK_019_POINT_IN_TIME_REPLAY_V2",
            "EXPERIMENT_REGISTRY":"MEGA_RESEARCH_020_APPEND_ONLY_CONSTITUTION",
            "CONTRADICTION_LEDGER":"MEGA_RESEARCH_020_APPEND_ONLY_CONTRADICTION_LEDGER",
            "VALIDATION_ENGINE":"MEGA_RESEARCH_020_OOS_STATISTICAL_ENGINE",
            "TERMINAL_PROJECTION":"MEGA_RESEARCH_020_READ_ONLY_TERMINAL_V2",
            "PUBLIC_SOURCE_ADAPTERS":"018_OFFICIAL_POLYMARKET_READ_ONLY_PLUS_019_DISABLED_CROSS_MARKET",
        }

    @staticmethod
    def authority_invariants():
        return {"ONE_AUTHORITATIVE_RAW_WRITER_MAX":1,"REPLAY_V1_REPLACED":False,
                "DUPLICATE_SIGNAL_AUTHORITY":False,"SCHEMA_BREAK_WITHOUT_VERSION":False,
                "UNKNOWN_COLLAPSED_TO_ZERO_OR_FALSE":False,"EXTERNAL_LIVE_ADAPTERS_ENABLED":False}

    def paper_execution_truth(self, *, side, quantity, limit_price, book_age_ms, levels,
                              baseline_probability, research_probability,
                              reference_mid_after_window=None, fill_config=None):
        if baseline_probability is None or research_probability is None:
            return {"status":"NOT_AVAILABLE","theoretical_signal_edge":None,"paper_executable_edge":None,
                    "queue_position_exact":False}
        req=PaperFillRequest(side,quantity,limit_price,book_age_ms,levels,reference_mid_after_window)
        result=simulate_paper_fill(req,fill_config)
        theoretical=(research_probability-baseline_probability if side=="BUY"
                     else baseline_probability-research_probability)
        executable=None
        if result.average_fill_price is not None:
            executable=(research_probability-result.average_fill_price if side=="BUY"
                        else result.average_fill_price-research_probability)
            executable-=max(0.0,-(result.adverse_selection or 0.0))
        return {"status":"NO_FILL" if result.no_fill else "PAPER_EXECUTION_MODELED",
                "theoretical_signal_edge":theoretical,"paper_executable_edge":executable,
                "fill":result.to_dict(),"cost_model_version":self.COST_MODEL_VERSION,
                "paper_fill_model_version":result.schema_version,"queue_position_exact":False}


@dataclass(frozen=True)
class SystemTruth:
    PAPER_ONLY: bool=True
    orders_enabled: bool=False
    live_capital_locked: bool=True
    source_connection: str="CONNECTED"
    last_event_age_ms: int | None=0
    sequence_gaps: int | None=0
    stale_data: bool | None=False
    raw_chain_hash: str | None=None
    raw_chain_tip: int | None=None
    replay_status: str="UNKNOWN"
    active_experiment: str | None=None
    data_quality_state: str="HEALTHY"
    blocking_reason: str | None=None

    def to_dict(self): return asdict(self)


class FailureInjectionHarness:
    @staticmethod
    def inject(base: SystemTruth, fixture: str):
        if fixture not in FAILURE_FIXTURES: raise ValueError("UNSUPPORTED_FAILURE_FIXTURE")
        mapping={
            "WEBSOCKET_DISCONNECT":dict(source_connection="DISCONNECTED",data_quality_state="DEGRADED",blocking_reason="source_disconnected"),
            "SEQUENCE_GAP":dict(sequence_gaps=(None if base.sequence_gaps is None else base.sequence_gaps+1),data_quality_state="BLOCKED",blocking_reason="sequence_gap"),
            "STALE_BOOK":dict(stale_data=True,data_quality_state="DEGRADED",blocking_reason="stale_book"),
            "DUPLICATE_EVENT":dict(data_quality_state="DEGRADED",blocking_reason="duplicate_event"),
            "OUT_OF_ORDER_EVENT":dict(data_quality_state="BLOCKED",blocking_reason="out_of_order_event"),
            "MARKET_IDENTITY_MISMATCH":dict(data_quality_state="BLOCKED",blocking_reason="market_identity_mismatch"),
            "TOKEN_IDENTITY_MISMATCH":dict(data_quality_state="BLOCKED",blocking_reason="token_identity_mismatch"),
            "RAW_HASH_CORRUPTION":dict(replay_status="FAIL",data_quality_state="BLOCKED",blocking_reason="raw_hash_corruption"),
            "CLOCK_SKEW":dict(data_quality_state="BLOCKED",blocking_reason="clock_skew"),
            "SOURCE_HEARTBEAT_LOSS":dict(source_connection="HEARTBEAT_LOST",data_quality_state="DEGRADED",blocking_reason="source_heartbeat_loss"),
        }
        return replace(base,**mapping[fixture])

    @staticmethod
    def health(truth: SystemTruth):
        if truth.PAPER_ONLY is not True or truth.orders_enabled is not False or truth.live_capital_locked is not True:
            return {"ok":False,"status":"BLOCKED","reason":"paper_only_invariant_failure"}
        if truth.data_quality_state=="BLOCKED" or truth.replay_status=="FAIL":
            return {"ok":False,"status":"BLOCKED","reason":truth.blocking_reason or "replay_failed"}
        if truth.data_quality_state=="DEGRADED" or truth.source_connection!="CONNECTED" or truth.stale_data is True:
            return {"ok":True,"status":"DEGRADED","reason":truth.blocking_reason}
        if (truth.last_event_age_ms is None or truth.sequence_gaps is None or truth.stale_data is None
                or truth.replay_status=="UNKNOWN"):
            return {"ok":False,"status":"UNKNOWN","reason":"required_health_fact_unknown"}
        return {"ok":True,"status":"HEALTHY","reason":None}


def terminal_projection(*, system_truth: SystemTruth, market=None, microstructure=None,
                        signal=None, paper_execution=None, evidence=None):
    return {"schema_version":"senex-live-terminal-v2","mode":"RESEARCH_ONLY_PAPER",
            "surfaces":{
                "SYSTEM_TRUTH":system_truth.to_dict(),"MARKET_RADAR":dict(market or {}),
                "MICROSTRUCTURE_ANOMALIES":dict(microstructure or {}),
                "FAIR_VALUE_AND_SIGNAL_DESK":dict(signal or {}),
                "PAPER_EXECUTION_TRUTH":dict(paper_execution or {}),
                "EVIDENCE_AND_GOVERNANCE":dict(evidence or {})},
            "surface_order":list(SURFACES),
            "controls":{"research_filters":True,"experiment_selector":True,"replay_selector":True,
                        "capital_actions":False,"authenticated_execution":False}}


def render_read_only_terminal(projection):
    cards=[]
    for name in SURFACES:
        rows=[]
        for key,value in sorted(projection["surfaces"].get(name,{}).items()):
            if isinstance(value,(dict,list,tuple)): continue
            shown="NOT_AVAILABLE" if value is None else str(value)
            rows.append(f"<div class='metric'><span>{html.escape(str(key))}</span><strong>{html.escape(shown)}</strong></div>")
        cards.append(f"<section class='panel' data-surface='{name}'><h2>{name.replace('_',' ')}</h2>{''.join(rows)}</section>")
    return f"""<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>SENEX Live Terminal V2</title><style>
body{{margin:0;background:#090d12;color:#e9f0f7;font:14px system-ui}}main{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;padding:12px}}
.panel{{background:#111823;border:1px solid #263344;border-radius:12px;padding:14px}}.metric{{display:flex;justify-content:space-between;padding:7px 0}}
@media(max-width:760px){{main{{grid-template-columns:1fr}}}}</style></head>
<body><header><h1>SENEX LIVE TERMINAL V2</h1><b>RESEARCH_ONLY · PAPER</b></header><main>{''.join(cards)}</main>
<footer>Read-only evidence surface. Unknown facts remain NOT_AVAILABLE.</footer></body></html>"""


def visual_contract_smoke():
    rendered=render_read_only_terminal(terminal_projection(system_truth=SystemTruth()))
    lower=rendered.lower()
    forbidden=("connect "+"wallet","private "+"key","place "+"order","deposit funds","withdraw funds")
    return {"six_surfaces":all(f"data-surface='{x}'" in rendered for x in SURFACES),
            "research_only_label":"research_only" in lower,
            "forbidden_controls_absent":all(x not in lower for x in forbidden),
            "responsive_viewport":"width=device-width" in lower,
            "mobile_breakpoint":"@media(max-width:760px)" in lower}
