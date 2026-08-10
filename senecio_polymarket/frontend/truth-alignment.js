/* AUD 049-R1 — full dashboard truth hardening.
 * Frontend-only observability adapter. It does not predict, settle, score, trade,
 * persist, change thresholds, or touch wallet/capital logic.
 */
(() => {
  'use strict';

  const SCORE_ROUTE = '/api/oracle/score?symbol=BTCUSDT';
  const PREDICTIONS_ROUTE = '/api/oracle/predictions/db?limit=50&symbol=BTCUSDT';
  const SCORE_TIMEOUT_MS = 8000;
  const STALE_AFTER_MS = 75000;
  const SCORE_STATUSES = new Set(['UNKNOWN', 'INSUFFICIENT_EVIDENCE', 'REJECTED', 'CALIBRATED']);
  const originalFetch = window.fetch.bind(window);

  let generation = 0;
  let activeController = null;
  let lastSuccessAt = null;
  let availability = 'UNAVAILABLE';

  class ContractError extends Error {
    constructor(code) {
      super(code);
      this.name = 'ContractError';
      this.code = code;
    }
  }

  const rawUrl = (input) => typeof input === 'string' ? input : input?.url;

  // Keep legacy app.js bound to the same primary truth sources without editing it.
  window.fetch = (input, init) => {
    const raw = rawUrl(input);
    if (raw === '/api/oracle/score') return originalFetch(SCORE_ROUTE, init);
    if (raw === '/api/oracle/predictions/db?limit=50') return originalFetch(PREDICTIONS_ROUTE, init);
    return originalFetch(input, init);
  };

  const node = (id) => document.getElementById(id);
  const text = (id, value) => {
    const el = node(id);
    if (el) el.textContent = value;
  };
  const isObject = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
  const isFiniteNumber = (v) => typeof v === 'number' && Number.isFinite(v);
  const isNonNegativeInteger = (v) => Number.isInteger(v) && v >= 0;
  const isPositiveInteger = (v) => Number.isInteger(v) && v > 0;
  const optionalFinite = (v) => v == null || isFiniteNumber(v);

  const fmtPct = (value, digits = 2) => value == null ? '—' : `${Number(value).toFixed(digits)}%`;
  const fmtMetric = (value, digits = 4) => value == null ? '—' : Number(value).toFixed(digits);
  const fmtWhen = (ms) => ms == null ? '—' : new Date(ms).toLocaleString(undefined, { hour12: false });
  const fmtAge = (ms) => ms == null ? '—' : `${Math.max(0, Math.floor((Date.now() - ms) / 1000))}s`;

  function validateMetricBlock(metric, label) {
    if (!isObject(metric)) throw new ContractError(`${label}_MISSING`);
    if (!SCORE_STATUSES.has(metric.score_status)) throw new ContractError(`${label}_STATUS_INVALID`);
    if (!isPositiveInteger(metric.minimum_n)) throw new ContractError(`${label}_MINIMUM_N_INVALID`);
    if (!isNonNegativeInteger(metric.verified) || !isNonNegativeInteger(metric.wins) || !isNonNegativeInteger(metric.losses)) {
      throw new ContractError(`${label}_COUNTS_INVALID`);
    }
    if (metric.wins + metric.losses !== metric.verified) throw new ContractError(`${label}_COUNTS_INCONSISTENT`);
    for (const [key, value] of [
      ['observed_win_rate_pct', metric.observed_win_rate_pct],
      ['wilson_lower_95', metric.wilson_lower_95],
      ['raw_confidence_brier', metric.raw_confidence_brier],
      ['raw_confidence_ece', metric.raw_confidence_ece],
    ]) {
      if (!optionalFinite(value)) throw new ContractError(`${label}_${key.toUpperCase()}_INVALID`);
    }
    if (metric.score_status === 'CALIBRATED') {
      if (!isFiniteNumber(metric.authoritative_score_pct) || metric.authoritative_score_pct < 0 || metric.authoritative_score_pct > 100) {
        throw new ContractError(`${label}_AUTHORITATIVE_SCORE_INVALID`);
      }
    } else if (metric.authoritative_score_pct !== null) {
      throw new ContractError(`${label}_NONCALIBRATED_SCORE_MUST_BE_NULL`);
    }
    return metric;
  }

  function validateScoreContract(score) {
    if (!isObject(score)) throw new ContractError('PAYLOAD_NOT_OBJECT');
    if (score.version !== 'oracle-score-truth-v1') throw new ContractError('VERSION_INVALID');
    if (score.requested_symbol !== 'BTCUSDT') throw new ContractError('REQUESTED_SYMBOL_INVALID');
    if (score.mode !== 'PAPER_ONLY') throw new ContractError('MODE_NOT_PAPER_ONLY');
    if (score.orders_enabled !== false) throw new ContractError('ORDERS_NOT_DISABLED');
    if (score.live_capital_locked !== true) throw new ContractError('CAPITAL_NOT_LOCKED');
    if (score.horizon_s !== 3600) throw new ContractError('HORIZON_INVALID');
    if (!SCORE_STATUSES.has(score.score_status)) throw new ContractError('SCORE_STATUS_INVALID');
    if (!isNonNegativeInteger(score.input_rows) || !isNonNegativeInteger(score.proof_qualified_rows) || !isNonNegativeInteger(score.excluded_rows)) {
      throw new ContractError('ROW_COUNTS_INVALID');
    }
    if (score.input_rows !== score.proof_qualified_rows + score.excluded_rows) throw new ContractError('ROW_COUNTS_INCONSISTENT');
    if (!isObject(score.exclusion_reasons)) throw new ContractError('EXCLUSION_REASONS_INVALID');
    for (const count of Object.values(score.exclusion_reasons)) {
      if (!isNonNegativeInteger(count)) throw new ContractError('EXCLUSION_COUNT_INVALID');
    }

    const selected = validateMetricBlock(score.selected, 'GLOBAL');
    if (selected.score_status !== score.score_status) throw new ContractError('GLOBAL_STATUS_MISMATCH');
    if (selected.verified !== score.proof_qualified_rows) throw new ContractError('GLOBAL_PROOF_COUNT_MISMATCH');

    if (score.score_status === 'CALIBRATED') {
      if (!isFiniteNumber(score.authoritative_score_pct) || score.authoritative_score_pct < 0 || score.authoritative_score_pct > 100) {
        throw new ContractError('AUTHORITATIVE_SCORE_INVALID');
      }
      if (selected.authoritative_score_pct !== score.authoritative_score_pct) throw new ContractError('AUTHORITATIVE_SCORE_MISMATCH');
    } else if (score.authoritative_score_pct !== null) {
      throw new ContractError('NONCALIBRATED_SCORE_MUST_BE_NULL');
    }

    let directions = null;
    if (selected.by_direction != null) {
      if (!isObject(selected.by_direction)) throw new ContractError('DIRECTION_BLOCK_INVALID');
      directions = {
        LONG: validateMetricBlock(selected.by_direction.LONG, 'LONG'),
        SHORT: validateMetricBlock(selected.by_direction.SHORT, 'SHORT'),
      };
    }

    return { score, selected, directions };
  }

  function renderExclusions(reasons) {
    const entries = Object.entries(reasons).filter(([, count]) => count > 0);
    if (!entries.length) return 'none';
    return entries
      .sort((a, b) => b[1] - a[1])
      .map(([reason, count]) => `${reason.replaceAll('_', ' ')}=${count}`)
      .join(' · ');
  }

  function setStatusData(value) {
    const el = node('score-status');
    if (el) el.dataset.status = value;
  }

  function renderFreshnessLabels() {
    text('score-availability', availability);
    text('score-last-success', fmtWhen(lastSuccessAt));
    text('score-refresh-age', fmtAge(lastSuccessAt));
  }

  function renderScore(rawScore, nowMs = Date.now()) {
    const { score, selected, directions } = validateScoreContract(rawScore);
    availability = 'FRESH';
    lastSuccessAt = nowMs;

    text('score-authoritative', score.authoritative_score_pct == null
      ? '— / NOT YET AUTHORITATIVE'
      : `${score.authoritative_score_pct.toFixed(2)}%`);
    text('score-status', score.score_status);
    setStatusData(score.score_status);
    text('score-proof-progress', `${score.proof_qualified_rows}/${selected.minimum_n}`);
    text('score-minimum-global', String(selected.minimum_n));

    if (directions) {
      text('score-long-progress', `${directions.LONG.verified}/${directions.LONG.minimum_n}`);
      text('score-long-minimum', String(directions.LONG.minimum_n));
      text('score-short-progress', `${directions.SHORT.verified}/${directions.SHORT.minimum_n}`);
      text('score-short-minimum', String(directions.SHORT.minimum_n));
    } else {
      text('score-long-progress', '—');
      text('score-long-minimum', 'server minimum unavailable');
      text('score-short-progress', '—');
      text('score-short-minimum', 'server minimum unavailable');
    }

    text('score-excluded', String(score.excluded_rows));
    text('score-exclusion-reasons', renderExclusions(score.exclusion_reasons));
    text('score-wins', String(selected.wins));
    text('score-losses', String(selected.losses));
    text('score-observed-winrate', fmtPct(selected.observed_win_rate_pct));
    text('score-wilson', fmtMetric(selected.wilson_lower_95));
    text('score-brier', fmtMetric(selected.raw_confidence_brier));
    text('score-ece', fmtMetric(selected.raw_confidence_ece));
    text('score-horizon', '1h / 3600s');
    text('score-safety', 'PAPER_ONLY · ORDERS OFF · CAPITAL LOCKED');
    text('score-error-detail', '—');
    renderFreshnessLabels();

    const meta = node('oracle-score-meta');
    if (meta) meta.textContent = `BTCUSDT · 1h proof-qualified · FRESH · age ${fmtAge(lastSuccessAt)}`;
    return { status: score.score_status, authoritative: score.authoritative_score_pct };
  }

  function renderUnavailable(kind, reason) {
    availability = kind;
    text('score-authoritative', '—');
    text('score-status', '—');
    setStatusData(kind);
    text('score-proof-progress', '—');
    text('score-minimum-global', '—');
    text('score-long-progress', '—');
    text('score-long-minimum', '—');
    text('score-short-progress', '—');
    text('score-short-minimum', '—');
    text('score-excluded', '—');
    text('score-exclusion-reasons', '—');
    text('score-wins', '—');
    text('score-losses', '—');
    text('score-observed-winrate', '—');
    text('score-wilson', '—');
    text('score-brier', '—');
    text('score-ece', '—');
    text('score-horizon', '—');
    text('score-safety', '—');
    text('score-error-detail', reason || kind);
    renderFreshnessLabels();
    const meta = node('oracle-score-meta');
    if (meta) meta.textContent = `BTCUSDT · ${kind} · last success ${fmtWhen(lastSuccessAt)} · age ${fmtAge(lastSuccessAt)}`;
    return kind;
  }

  async function refreshTruth(timeoutMs = SCORE_TIMEOUT_MS) {
    const myGeneration = ++generation;
    if (activeController) activeController.abort();
    const controller = new AbortController();
    activeController = controller;
    let timedOut = false;
    const timer = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, timeoutMs);

    try {
      const response = await originalFetch(SCORE_ROUTE, {
        headers: { Accept: 'application/json' },
        cache: 'no-store',
        signal: controller.signal,
      });
      if (myGeneration !== generation) return { ignored: true };
      if (timedOut) throw new Error('TIMEOUT');
      if (!response || !response.ok) {
        const status = response?.status ?? 'NO_RESPONSE';
        const error = new Error(`HTTP_${status}`);
        error.transport = true;
        throw error;
      }
      let payload;
      try {
        payload = await response.json();
      } catch (_) {
        throw new ContractError('MALFORMED_JSON');
      }
      if (myGeneration !== generation) return { ignored: true };
      if (timedOut) throw new Error('TIMEOUT');
      return { ignored: false, rendered: renderScore(payload) };
    } catch (error) {
      if (myGeneration !== generation) return { ignored: true };
      if (error instanceof ContractError) {
        renderUnavailable('CONTRACT_ERROR', error.code);
        return { ignored: false, error: error.code, availability: 'CONTRACT_ERROR' };
      }
      const reason = timedOut || error?.name === 'AbortError' || error?.message === 'TIMEOUT'
        ? 'TIMEOUT'
        : (error?.message || 'FETCH_ERROR');
      const kind = lastSuccessAt == null ? 'UNAVAILABLE' : 'STALE';
      renderUnavailable(kind, reason);
      return { ignored: false, error: reason, availability: kind };
    } finally {
      window.clearTimeout(timer);
      if (myGeneration === generation) activeController = null;
    }
  }

  function refreshAgeState(nowMs = Date.now()) {
    if (lastSuccessAt == null) {
      renderFreshnessLabels();
      return availability;
    }
    if (availability === 'FRESH' && nowMs - lastSuccessAt > STALE_AFTER_MS) {
      return renderUnavailable('STALE', 'REFRESH_AGE_EXCEEDED');
    }
    renderFreshnessLabels();
    const meta = node('oracle-score-meta');
    if (meta && availability === 'FRESH') meta.textContent = `BTCUSDT · 1h proof-qualified · FRESH · age ${fmtAge(lastSuccessAt)}`;
    return availability;
  }

  function normalizeOutcomeLabel(value) {
    const raw = String(value || '').trim();
    if (raw.startsWith('RAW / UNVERIFIED')) return raw;
    const upper = raw.toUpperCase();
    if (upper === 'WIN' || upper === 'LOSS') return `RAW / UNVERIFIED ${upper}`;
    if (upper === 'PEND') return 'RAW / UNVERIFIED · 1h pending';
    if (!raw || raw === '—') return 'RAW / UNVERIFIED · no proof-qualified outcome';
    return `RAW / UNVERIFIED ${raw}`;
  }

  function markRawOutcomeCells() {
    if (!document.querySelectorAll) return;
    document.querySelectorAll('#oracle-table tbody tr td:last-child:not(.placeholder)').forEach((cell) => {
      const next = normalizeOutcomeLabel(cell.textContent);
      if (cell.textContent !== next) {
        cell.textContent = next;
        cell.dataset.proofSemantics = 'RAW_STORED_OUTCOME_NOT_PROOF_QUALIFIED';
      }
    });
  }

  function installOutcomeObserver() {
    const tbody = document.querySelector ? document.querySelector('#oracle-table tbody') : null;
    if (!tbody) return;
    markRawOutcomeCells();
    if (typeof MutationObserver === 'function') {
      new MutationObserver(markRawOutcomeCells).observe(tbody, { childList: true, subtree: true });
    }
  }

  window.__senexTruth049 = Object.freeze({
    SCORE_ROUTE,
    PREDICTIONS_ROUTE,
    SCORE_TIMEOUT_MS,
    STALE_AFTER_MS,
    validateScoreContract,
    renderScore,
    renderUnavailable,
    refreshTruth,
    refreshAgeState,
    normalizeOutcomeLabel,
    markRawOutcomeCells,
    getState: () => ({ generation, lastSuccessAt, availability }),
  });

  refreshTruth();
  window.setInterval(refreshTruth, 30000);
  window.setInterval(refreshAgeState, 5000);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) refreshTruth();
  });
  document.addEventListener('DOMContentLoaded', installOutcomeObserver);
})();
