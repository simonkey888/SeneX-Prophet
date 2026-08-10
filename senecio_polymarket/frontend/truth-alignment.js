/* AUD 048 — Oracle dashboard truth alignment.
 * Presentation-only adapter. No prediction, scoring, order, wallet, or safety logic.
 */
(() => {
  'use strict';

  const SCORE_ROUTE = '/api/oracle/score?symbol=BTCUSDT';
  const SCORE_STATUSES = new Set(['UNKNOWN', 'INSUFFICIENT_EVIDENCE', 'REJECTED', 'CALIBRATED']);
  const originalFetch = window.fetch.bind(window);

  // Bind the legacy app's generic score request to the cockpit's primary BTC instrument.
  // This runs before app.js, so every cockpit score request is unambiguous.
  window.fetch = (input, init) => {
    const raw = typeof input === 'string' ? input : input?.url;
    if (raw === '/api/oracle/score') return originalFetch(SCORE_ROUTE, init);
    return originalFetch(input, init);
  };

  const text = (id, value) => {
    const node = document.getElementById(id);
    if (node) node.textContent = value;
  };

  const fmtPct = (value) => value == null ? '—' : `${Number(value).toFixed(2)}%`;
  const fmtMetric = (value, digits = 4) => value == null ? '—' : Number(value).toFixed(digits);

  function renderExclusions(reasons) {
    const entries = Object.entries(reasons || {}).filter(([, count]) => Number(count) > 0);
    if (!entries.length) return 'none';
    return entries
      .sort((a, b) => Number(b[1]) - Number(a[1]))
      .map(([reason, count]) => `${reason.replaceAll('_', ' ')}=${count}`)
      .join(' · ');
  }

  function directionMetric(score, direction) {
    return score?.selected?.by_direction?.[direction]
      || score?.by_symbol?.BTCUSDT?.by_direction?.[direction]
      || null;
  }

  function renderScore(score) {
    const status = SCORE_STATUSES.has(score?.score_status) ? score.score_status : 'UNKNOWN';
    const selected = score?.selected || {};
    const authoritative = score?.authoritative_score_pct;
    const longMetric = directionMetric(score, 'LONG');
    const shortMetric = directionMetric(score, 'SHORT');

    text('score-authoritative', authoritative == null
      ? '— / NOT YET AUTHORITATIVE'
      : `${Number(authoritative).toFixed(2)}%`);
    text('score-status', status);
    text('score-proof-progress', `${Number(score?.proof_qualified_rows || 0)}/100`);
    text('score-minimum-global', '100');
    text('score-long-progress', `${Number(longMetric?.verified || 0)}/30`);
    text('score-short-progress', `${Number(shortMetric?.verified || 0)}/30`);
    text('score-excluded', String(Number(score?.excluded_rows || 0)));
    text('score-exclusion-reasons', renderExclusions(score?.exclusion_reasons));
    text('score-wins', String(Number(selected?.wins || 0)));
    text('score-losses', String(Number(selected?.losses || 0)));
    text('score-observed-winrate', fmtPct(selected?.observed_win_rate_pct));
    text('score-wilson', fmtMetric(selected?.wilson_lower_95));
    text('score-brier', fmtMetric(selected?.raw_confidence_brier));
    text('score-ece', fmtMetric(selected?.raw_confidence_ece));

    const statusNode = document.getElementById('score-status');
    if (statusNode) statusNode.dataset.status = status;

    const meta = document.getElementById('oracle-score-meta');
    if (meta) meta.textContent = 'BTCUSDT · proof-qualified · live';
  }

  function renderUnavailable() {
    text('score-authoritative', '— / NOT YET AUTHORITATIVE');
    text('score-status', 'UNKNOWN');
    text('score-proof-progress', '0/100');
    text('score-long-progress', '0/30');
    text('score-short-progress', '0/30');
    text('score-exclusion-reasons', 'score API unavailable');
  }

  async function refreshTruth() {
    try {
      const response = await originalFetch(SCORE_ROUTE, {
        headers: { Accept: 'application/json' },
        cache: 'no-store',
      });
      if (!response.ok) throw new Error(`score HTTP ${response.status}`);
      renderScore(await response.json());
    } catch (error) {
      console.error('oracle truth alignment fetch error', error);
      renderUnavailable();
    }
  }

  refreshTruth();
  window.setInterval(refreshTruth, 30000);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) refreshTruth();
  });
})();
