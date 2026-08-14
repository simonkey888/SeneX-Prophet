/* SENEX AUD-060 — pure dashboard truth models (browser + Node testable). */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root) root.SenexDashboardTruth = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  const CLAIM_CLASSES = Object.freeze([
    'RUNTIME_OBSERVED',
    'API_DERIVED',
    'STATIC_POLICY',
    'DIAGNOSTIC',
    'DECISION_TIME_SNAPSHOT',
    'UNKNOWN/STALE',
  ]);

  const present = (value) => value !== null && value !== undefined && value !== '';
  const display = (value) => present(value) ? String(value) : '—';
  const pctAlready = (value, digits = 1) => {
    if (!present(value) || !Number.isFinite(Number(value))) return '—';
    return `${Number(value).toFixed(digits)}%`;
  };

  function scoreView(score) {
    const source = score && typeof score === 'object' ? score : {};
    const authority = source.authority_1h && typeof source.authority_1h === 'object'
      ? source.authority_1h : {};
    const global = authority.global && typeof authority.global === 'object'
      ? authority.global : {};
    return {
      totalInputRows: display(source.total_predictions ?? source.input_rows),
      proofQualifiedRaw: display(source.proof_qualified_rows_raw),
      independent1h: display(source.independent_1h_rows),
      authorityN: display(global.verified ?? source.independent_1h_rows),
      authorityWr: pctAlready(global.win_rate_pct),
      authoritativeScore: pctAlready(source.authoritative_score_pct),
      status: display(source.score_status || 'UNKNOWN'),
      rawObservedWr: pctAlready(source.observed_win_rate_pct),
      rawObservedClaim: 'DIAGNOSTIC',
      authorityClaim: 'API_DERIVED',
      scope: display(source.requested_symbol || 'UNKNOWN'),
      cohort: display(source.authority_cohort || 'UNKNOWN'),
    };
  }

  function auditOf(row) {
    return row && row.audit && typeof row.audit === 'object' ? row.audit : {};
  }

  function step2Of(row) {
    const audit = auditOf(row);
    const pipeline = audit.pipeline && typeof audit.pipeline === 'object' ? audit.pipeline : {};
    return pipeline.step2_features && typeof pipeline.step2_features === 'object'
      ? pipeline.step2_features : {};
  }

  function decisionView(row) {
    const step2 = step2Of(row);
    const learning = step2.learning_state_v1 && typeof step2.learning_state_v1 === 'object'
      ? step2.learning_state_v1 : {};
    return {
      learningReplayN: display(learning.proof_qualified_n),
      learningStatus: display(learning.status || 'UNKNOWN'),
      learningMutations: display(learning.mutations),
      claimClass: 'DECISION_TIME_SNAPSHOT',
    };
  }

  function domainState() {
    return {
      status: 'LOADING',
      stale: false,
      lastSuccessMs: null,
      lastAttemptMs: null,
      error: null,
    };
  }

  function domainSuccess(previous, nowMs = Date.now()) {
    return {
      ...(previous || domainState()),
      status: 'OK',
      stale: false,
      lastSuccessMs: nowMs,
      lastAttemptMs: nowMs,
      error: null,
    };
  }

  function domainFailure(previous, error, nowMs = Date.now()) {
    const prior = previous || domainState();
    return {
      ...prior,
      status: 'ERROR',
      stale: prior.lastSuccessMs !== null,
      lastAttemptMs: nowMs,
      error: display(error || 'UNKNOWN_ERROR'),
    };
  }

  function ageSeconds(timestampMs, nowMs = Date.now()) {
    if (!Number.isFinite(Number(timestampMs))) return null;
    return Math.max(0, Math.floor((Number(nowMs) - Number(timestampMs)) / 1000));
  }

  function domainLabel(name, state, nowMs = Date.now()) {
    const current = state || domainState();
    const age = ageSeconds(current.lastSuccessMs, nowMs);
    const last = age === null ? 'no success yet' : `last success ${age}s ago`;
    if (current.status === 'ERROR') {
      const freshness = current.stale ? 'STALE' : 'NO CURRENT DATA';
      return `${name} · ERROR ${current.error} · ${freshness} · ${last}`;
    }
    if (current.status === 'OK') return `${name} · OK · ${last}`;
    return `${name} · ${current.status} · UNKNOWN`;
  }

  function boolView(value, trueLabel, falseLabel) {
    if (typeof value !== 'boolean') return 'UNKNOWN';
    return value ? trueLabel : falseLabel;
  }

  function directionalView(value, missingPolicy = false) {
    if (typeof value === 'boolean') return `${value ? 'ON' : 'OFF'} · API_DERIVED`;
    return missingPolicy ? 'UNKNOWN · POLICY_DEFAULT_OFF (not runtime evidence)' : 'UNKNOWN';
  }

  function safetyView(context) {
    const ctx = context && typeof context === 'object' ? context : {};
    const safety = ctx.safety && typeof ctx.safety === 'object' ? ctx.safety : {};
    const poly = ctx.polymarket && typeof ctx.polymarket === 'object' ? ctx.polymarket : {};
    const kalshi = ctx.kalshi && typeof ctx.kalshi === 'object' ? ctx.kalshi : {};
    const boros = ctx.boros && typeof ctx.boros === 'object' ? ctx.boros : {};
    return {
      marketMode: display(ctx.mode || 'UNKNOWN'),
      tradeMode: present(safety.trade_mode) ? String(safety.trade_mode) : 'UNKNOWN',
      liveCapital: boolView(safety.live_capital_locked, 'LOCKED', 'UNLOCKED'),
      orders: boolView(safety.orders_enabled, 'ENABLED', 'DISABLED'),
      readOnlyAdapters: boolView(safety.read_only_market_adapters, 'READ-ONLY', 'NOT READ-ONLY'),
      syntheticScheduler: boolView(ctx.synthetic_demo_enabled, 'ENABLED', 'DISABLED'),
      polymarketDirectional: directionalView(poly.directional_use, true),
      kalshiDirectional: directionalView(kalshi.directional_use),
      borosDirectional: directionalView(boros.directional_use),
    };
  }

  return Object.freeze({
    CLAIM_CLASSES,
    scoreView,
    decisionView,
    domainState,
    domainSuccess,
    domainFailure,
    domainLabel,
    safetyView,
  });
});
