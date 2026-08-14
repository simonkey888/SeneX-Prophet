/* SENEX AUD-060 — source-backed dashboard with explicit stale/error state. */
(() => {
  'use strict';

  const truth = window.SenexDashboardTruth;
  if (!truth) throw new Error('dashboard_truth.js must load before app.js');

  const $ = (selector) => document.querySelector(selector);
  const esc = (value) => String(value ?? '—').replace(
    /[&<>'"]/g,
    (char) => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[char]),
  );
  const pct = (value, digits = 1) => value == null || !Number.isFinite(Number(value))
    ? '—' : `${(Number(value) * 100).toFixed(digits)}%`;
  const num = (value, digits = 3) => value == null || !Number.isFinite(Number(value))
    ? '—' : Number(value).toFixed(digits);
  const apr = (value) => value == null || !Number.isFinite(Number(value))
    ? '—' : `${(Number(value) * 100).toFixed(2)}%`;
  const money = (value, digits = 0) => value == null || !Number.isFinite(Number(value))
    ? '—' : `$${Number(value).toLocaleString(undefined, {maximumFractionDigits: digits})}`;
  const clock = (value) => {
    if (!value) return '—';
    let numeric = Number(value);
    if (Number.isFinite(numeric)) {
      if (numeric > 1e12) numeric /= 1000;
      return new Date(numeric * 1000).toLocaleTimeString('en-GB', {hour12: false});
    }
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? '—' : date.toLocaleTimeString('en-GB', {hour12: false});
  };
  const countdown = (seconds) => {
    if (seconds == null || !Number.isFinite(Number(seconds))) return '—';
    const bounded = Math.max(0, Math.round(Number(seconds)));
    return `${Math.floor(bounded / 60)}:${String(bounded % 60).padStart(2, '0')}`;
  };
  const boolKnown = (value) => typeof value === 'boolean';
  const symbolKey = (value) => String(value || '').toUpperCase().replace(/[\/-]/g, '').trim();

  const state = {
    context: null,
    predictions: [],
    score: null,
    domains: {
      context: truth.domainState(),
      score: truth.domainState(),
      predictions: truth.domainState(),
    },
  };

  async function getJSON(url) {
    const response = await fetch(url, {cache: 'no-store'});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  }

  function setValueClass(selector, tone) {
    const element = $(selector);
    element.className = `value${tone ? ` ${tone}` : ''}`;
  }

  function renderDomainHealth() {
    const labels = {
      context: ['#health-context', 'MARKET CONTEXT'],
      score: ['#health-score', 'BTC SCORE'],
      predictions: ['#health-predictions', 'DB PREDICTIONS'],
    };
    Object.entries(labels).forEach(([name, [selector, label]]) => {
      const domain = state.domains[name];
      const element = $(selector);
      element.textContent = truth.domainLabel(label, domain);
      element.className = `domain-health ${domain.status.toLowerCase()}`;
      element.dataset.claimClass = domain.status === 'OK' ? 'API_DERIVED' : 'UNKNOWN/STALE';
    });

    document.querySelectorAll('.panel[data-domain]').forEach((panel) => {
      const domainNames = String(panel.dataset.domain || '').split(/\s+/).filter(Boolean);
      const failed = domainNames.filter((name) => state.domains[name]?.status === 'ERROR');
      panel.classList.toggle('is-stale', failed.length > 0);
      panel.dataset.health = failed.length ? `${failed.join(' + ')} ERROR · STALE` : '';
    });
  }

  function domainSuccess(name) {
    state.domains[name] = truth.domainSuccess(state.domains[name]);
    renderDomainHealth();
  }

  function domainFailure(name, error) {
    state.domains[name] = truth.domainFailure(
      state.domains[name],
      error instanceof Error ? error.message : String(error || 'UNKNOWN_ERROR'),
    );
    if (name === 'context') {
      const footerSafety = $('#footer-safety');
      const footerFreshness = $('#footer-freshness');
      footerSafety.textContent = '[UNKNOWN/STALE] retained runtime safety · context ERROR';
      footerSafety.dataset.claimClass = 'UNKNOWN/STALE';
      footerFreshness.textContent = '[UNKNOWN/STALE] retained source freshness · context ERROR';
      footerFreshness.dataset.claimClass = 'UNKNOWN/STALE';
    }
    renderDomainHealth();
  }

  function setConn(polymarket) {
    const element = $('#conn-status');
    const status = polymarket && polymarket.status;
    const live = status === 'LIVE_WS' || status === 'LIVE_REST';
    element.className = `pill ${live ? 'pill-green' : 'pill-red'}`;
    element.textContent = status
      ? `${live ? 'POLYMARKET' : 'MARKET'} ${status}`
      : 'MARKET UNKNOWN';
    element.dataset.claimClass = status ? 'RUNTIME_OBSERVED' : 'UNKNOWN/STALE';
  }

  function sourceRow(name, detail, claimClass) {
    return `<div class="check source-check" data-claim-class="${esc(claimClass)}">
      <span class="name">${esc(name)}</span>
      <span class="detail">${esc(detail)}</span>
      <span class="claim-tag">${esc(claimClass)}</span>
    </div>`;
  }

  function renderContext(context) {
    const ctx = context && typeof context === 'object' ? context : {};
    state.context = ctx;
    const poly = ctx.polymarket && typeof ctx.polymarket === 'object' ? ctx.polymarket : {};
    const kalshi = ctx.kalshi && typeof ctx.kalshi === 'object' ? ctx.kalshi : {};
    const boros = ctx.boros && typeof ctx.boros === 'object' ? ctx.boros : {};
    const oracle = ctx.oracle && typeof ctx.oracle === 'object' ? ctx.oracle : {};
    const safety = truth.safetyView(ctx);

    setConn(poly);
    $('#stat-mode').textContent = safety.marketMode;
    $('#stat-clob').textContent = boolKnown(poly.ws_connected)
      ? (poly.ws_connected ? 'WS LIVE' : (poly.status || 'NOT CONNECTED'))
      : (poly.status || 'UNKNOWN');
    $('#stat-oracle').textContent = oracle.cycles_run == null ? 'UNKNOWN' : `cycle ${oracle.cycles_run}`;
    $('#stat-live').textContent = safety.liveCapital;
    ['#stat-mode', '#stat-clob', '#stat-oracle', '#stat-live'].forEach((selector) => {
      $(selector).dataset.claimClass = $(selector).textContent.includes('UNKNOWN')
        ? 'UNKNOWN/STALE' : 'API_DERIVED';
    });
    $('#score-next').textContent = clock(oracle.next_cycle_at);

    $('#poly-up').textContent = pct(poly.up_probability);
    $('#poly-down').textContent = pct(poly.down_probability);
    if (poly.directional_pressure == null || !Number.isFinite(Number(poly.directional_pressure))) {
      $('#poly-pressure').textContent = '— · DIAGNOSTIC';
      setValueClass('#poly-pressure', '');
    } else {
      const pressure = Number(poly.directional_pressure);
      $('#poly-pressure').textContent = `${pressure >= 0 ? '+' : ''}${pressure.toFixed(3)} · DIAGNOSTIC`;
      setValueClass('#poly-pressure', pressure > 0.05 ? 'pos' : pressure < -0.05 ? 'neg' : '');
    }
    $('#poly-close').textContent = countdown(poly.seconds_to_close);
    const market = poly.market && typeof poly.market === 'object' ? poly.market : {};
    $('#poly-question').textContent = market.question || 'No current BTC 5m market discovered';
    $('#poly-slug').textContent = market.slug || '—';
    $('#poly-resolution').textContent = `resolution source: ${market.resolution_source || 'UNKNOWN'}`;
    const polyClaim = poly.status ? 'RUNTIME_OBSERVED' : 'UNKNOWN/STALE';
    $('#poly-meta').textContent = `[${polyClaim}] ${poly.status || 'UNKNOWN'} · fresh ${poly.freshness_s ?? 'UNKNOWN'}s · directional ${safety.polymarketDirectional}`;
    $('#poly-panel').dataset.claimClass = polyClaim;
    const transport = boolKnown(poly.ws_connected)
      ? (poly.ws_connected ? 'WS subscribed' : 'WS not connected') : 'transport UNKNOWN';
    const depth = boolKnown(poly.depth_used_for_pressure)
      ? (poly.depth_used_for_pressure ? 'CURRENT / USED' : 'NOT USED') : 'UNKNOWN';
    const bookClaim = boolKnown(poly.ws_connected) || boolKnown(poly.depth_used_for_pressure)
      ? 'RUNTIME_OBSERVED' : 'UNKNOWN/STALE';
    $('#poly-book-meta').textContent = `[${bookClaim}] ${transport} · depth ${depth}`;
    $('#poly-book-panel').dataset.claimClass = bookClaim;
    renderBook(poly);
    renderPolyFeed(Array.isArray(poly.recent_events) ? poly.recent_events : []);
    renderKalshi(kalshi, safety.kalshiDirectional);
    renderBoros(boros, safety.borosDirectional);

    $('#safe-market-mode').textContent = safety.marketMode;
    $('#safe-trade-mode').textContent = safety.tradeMode;
    $('#safe-live').textContent = safety.liveCapital;
    $('#safe-orders').textContent = safety.orders;
    $('#safe-synth').textContent = safety.syntheticScheduler;
    $('#safe-adapters').textContent = safety.readOnlyAdapters;
    setValueClass('#safe-live', safety.liveCapital === 'LOCKED' ? 'pos' : safety.liveCapital === 'UNLOCKED' ? 'neg' : '');
    setValueClass('#safe-orders', safety.orders === 'DISABLED' ? 'pos' : safety.orders === 'ENABLED' ? 'neg' : '');
    setValueClass('#safe-synth', safety.syntheticScheduler === 'DISABLED' ? 'pos' : safety.syntheticScheduler === 'ENABLED' ? 'neg' : '');
    setValueClass('#safe-adapters', safety.readOnlyAdapters === 'READ-ONLY' ? 'pos' : safety.readOnlyAdapters === 'NOT READ-ONLY' ? 'neg' : '');

    const polyDiscoveryKnown = Boolean(poly.source) && boolKnown(poly.read_only);
    const polyDiscovery = polyDiscoveryKnown
      ? `${poly.source} · ${poly.read_only ? 'READ-ONLY' : 'NOT READ-ONLY'} · ${market.slug ? 'MARKET FOUND' : (poly.status || 'NO MARKET')}`
      : 'UNKNOWN · source/read_only evidence incomplete';
    const executionFieldsKnown = safety.tradeMode !== 'UNKNOWN'
      && safety.liveCapital !== 'UNKNOWN'
      && safety.orders !== 'UNKNOWN';
    const execution = executionFieldsKnown
      ? `trade=${safety.tradeMode} · live=${safety.liveCapital} · orders=${safety.orders}`
      : 'UNKNOWN · safety fields incomplete';
    const synthetic = safety.syntheticScheduler === 'UNKNOWN'
      ? 'UNKNOWN · field missing' : safety.syntheticScheduler;
    $('#source-integrity').innerHTML = [
      sourceRow('Polymarket discovery', polyDiscovery, polyDiscoveryKnown ? 'API_DERIVED' : 'UNKNOWN/STALE'),
      sourceRow('Polymarket orderbook', poly.status || 'UNKNOWN', poly.status ? 'RUNTIME_OBSERVED' : 'UNKNOWN/STALE'),
      sourceRow('CLOB WebSocket', boolKnown(poly.ws_connected) ? (poly.ws_connected ? 'CONNECTED' : 'NOT CONNECTED') : 'UNKNOWN', boolKnown(poly.ws_connected) ? 'RUNTIME_OBSERVED' : 'UNKNOWN/STALE'),
      sourceRow('Poly directional use', safety.polymarketDirectional, safety.polymarketDirectional.startsWith('UNKNOWN') ? 'STATIC_POLICY' : 'API_DERIVED'),
      sourceRow('Kalshi directional use', safety.kalshiDirectional, safety.kalshiDirectional.startsWith('UNKNOWN') ? 'UNKNOWN/STALE' : 'API_DERIVED'),
      sourceRow('Boros directional use', safety.borosDirectional, safety.borosDirectional.startsWith('UNKNOWN') ? 'UNKNOWN/STALE' : 'API_DERIVED'),
      sourceRow('Synthetic scheduler', synthetic, synthetic.startsWith('UNKNOWN') ? 'UNKNOWN/STALE' : 'API_DERIVED'),
      sourceRow('Execution safety', execution, executionFieldsKnown ? 'API_DERIVED' : 'UNKNOWN/STALE'),
    ].join('');

    $('#footer-safety').textContent = executionFieldsKnown
      ? `[API_DERIVED] ${execution}`
      : '[UNKNOWN/STALE] runtime safety fields incomplete';
    $('#footer-safety').dataset.claimClass = executionFieldsKnown ? 'API_DERIVED' : 'UNKNOWN/STALE';
    const freshnessKnown = [poly.freshness_s, kalshi.freshness_s, boros.freshness_s]
      .some((value) => value != null && Number.isFinite(Number(value)));
    const freshnessClaim = freshnessKnown ? 'RUNTIME_OBSERVED' : 'UNKNOWN/STALE';
    $('#footer-freshness').textContent = `[${freshnessClaim}] Poly ${poly.freshness_s ?? 'UNKNOWN'}s · Kalshi ${kalshi.freshness_s ?? 'UNKNOWN'}s · Boros ${boros.freshness_s ?? 'UNKNOWN'}s`;
    $('#footer-freshness').dataset.claimClass = freshnessClaim;
  }

  function renderBook(poly) {
    const body = $('#poly-book-body');
    const rows = [['UP', poly.up || {}], ['DOWN', poly.down || {}]];
    body.innerHTML = rows.map(([label, book]) => {
      const depth = (value, digits) => {
        if (!boolKnown(book.depth_current)) return 'UNKNOWN';
        return book.depth_current ? num(value, digits) : 'STALE';
      };
      return `<tr>
        <td class="sym">${label}</td>
        <td class="num">${num(book.best_bid, 3)}</td>
        <td class="num">${num(book.best_ask, 3)}</td>
        <td class="num">${num(book.spread, 3)}</td>
        <td class="num">${depth(book.bid_depth_5, 1)}</td>
        <td class="num">${depth(book.ask_depth_5, 1)}</td>
        <td class="num">${depth(book.depth_imbalance, 3)}</td>
      </tr>`;
    }).join('');
  }

  function renderPolyFeed(events) {
    const feed = $('#poly-feed');
    if (!events.length) {
      feed.innerHTML = '<div class="placeholder" style="padding:12px">No current public CLOB events in payload</div>';
      return;
    }
    feed.innerHTML = events.slice(0, 50).map((event) => {
      const detail = [
        event.outcome,
        event.side,
        event.price != null ? `px=${event.price}` : '',
        event.best_bid != null ? `bid=${event.best_bid}` : '',
        event.best_ask != null ? `ask=${event.best_ask}` : '',
      ].filter(Boolean).join(' ');
      return `<div class="feed-row"><span class="ts">${clock(event.timestamp)}</span><span class="type type-MARKET_TICK">${esc(event.event_type || 'CLOB')}</span><span class="body">${esc(detail || 'event without price fields')}</span></div>`;
    }).join('');
  }

  function renderKalshi(kalshi, directional) {
    const market = kalshi.market && typeof kalshi.market === 'object' ? kalshi.market : {};
    const claim = kalshi.status ? 'DIAGNOSTIC' : 'UNKNOWN/STALE';
    $('#kalshi-meta').textContent = `[${claim}] ${kalshi.status || 'UNKNOWN'} · 15m cross-venue diagnostic · directional ${directional}`;
    $('#kalshi-panel').dataset.claimClass = claim;
    $('#kalshi-yes').textContent = pct(market.yes_probability);
    $('#kalshi-no').textContent = pct(market.no_probability);
    $('#kalshi-volume').textContent = market.volume == null ? '—' : Number(market.volume).toLocaleString(undefined, {maximumFractionDigits: 0});
    $('#kalshi-oi').textContent = market.open_interest == null ? '—' : Number(market.open_interest).toLocaleString(undefined, {maximumFractionDigits: 0});
    $('#kalshi-title').textContent = market.title || 'No open KXBTC15M market in current payload';
    $('#kalshi-ticker').textContent = market.ticker || '—';
    $('#kalshi-status').textContent = `exchange=${market.exchange_active ?? 'UNKNOWN'} trading=${market.trading_active ?? 'UNKNOWN'} · closes ${countdown(market.seconds_to_close)}`;
  }

  function renderBoros(boros, directional) {
    const claim = boros.status ? 'DIAGNOSTIC' : 'UNKNOWN/STALE';
    $('#boros-meta').textContent = `[${claim}] ${boros.status || 'UNKNOWN'} · funding diagnostic · directional ${directional}`;
    $('#boros-panel').dataset.claimClass = claim;
    const rows = Array.isArray(boros.markets) ? boros.markets : [];
    const body = $('#boros-body');
    if (!rows.length) {
      body.innerHTML = `<tr><td colspan="7" class="placeholder">${esc(boros.last_error ? `Boros unavailable: ${boros.last_error}` : 'No BTC/ETH Boros markets in current payload')}</td></tr>`;
      return;
    }
    body.innerHTML = rows.slice(0, 20).map((market) => `<tr>
      <td class="sym">${esc(market.underlying_symbol || market.symbol)}</td>
      <td>${esc(market.name || market.market_id)}</td>
      <td class="num">${apr(market.mid_apr)}</td>
      <td class="num">${apr(market.mark_apr)}</td>
      <td class="num">${money(market.asset_mark_price, 2)}</td>
      <td class="num">${money(market.volume_24h, 0)}</td>
      <td class="num">${money(market.open_interest_notional, 0)}</td>
    </tr>`).join('');
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

  function clearDecisionContext(message) {
    $('#learn-state').textContent = 'UNKNOWN';
    $('#learn-n').textContent = 'UNKNOWN';
    $('#learn-mutations').textContent = 'UNKNOWN';
    $('#learn-poly-weight').textContent = 'UNKNOWN';
    $('#decision-context').innerHTML = `<div class="placeholder">${esc(message)}</div>`;
  }

  function renderPredictions(payload) {
    const rows = Array.isArray(payload.predictions) ? payload.predictions : [];
    state.predictions = rows;
    $('#oracle-pred-meta').textContent = `[API_DERIVED] ${payload.total_in_db ?? 'UNKNOWN'} total_in_db · CROSS-SYMBOL · showing ${rows.length}`;
    const body = $('#oracle-table tbody');
    body.innerHTML = rows.slice(0, 30).map((row) => {
      const step2 = step2Of(row);
      const learning = step2.learning_state_v1 || {};
      const poly = step2.polymarket_context_v1 || {};
      const polyText = poly.eligible
        ? `${pct(poly.up_probability)} · applied=${num(poly.pressure_component, 3)} · SNAPSHOT`
        : '—';
      const replay = learning.proof_qualified_n == null
        ? (learning.status || '—')
        : `${learning.status || '—'} replay_n=${learning.proof_qualified_n}`;
      return `<tr>
        <td>${clock(row.ts || row.created_at)}</td>
        <td class="sym">${esc(row.symbol)}</td>
        <td style="font-weight:700">${esc(row.prediction)}</td>
        <td class="num">${pct(row.confidence)}</td>
        <td>${esc(replay)}</td>
        <td>${esc(polyText)}</td>
        <td>${esc(row.outcome || 'PENDING')}</td>
      </tr>`;
    }).join('') || '<tr><td colspan="7" class="placeholder">No predictions in current API payload</td></tr>';

    const btc = rows.find((row) => symbolKey(row.symbol) === 'BTCUSDT');
    if (btc) renderDecisionContext(btc);
    else clearDecisionContext('No BTC decision in the current cross-symbol predictions window');
  }

  function renderDecisionContext(row) {
    const step2 = step2Of(row);
    const learning = step2.learning_state_v1 || {};
    const poly = step2.polymarket_context_v1 || {};
    const external = auditOf(row).external_markets_v1 || {};
    const kalshiMarket = external.kalshi?.market || {};
    const decision = truth.decisionView(row, state.score || {});
    $('#learn-state').textContent = decision.learningStatus;
    $('#learn-n').textContent = decision.learningReplayN;
    $('#learn-mutations').textContent = decision.learningMutations;
    if (typeof poly.directional_use === 'boolean') {
      $('#learn-poly-weight').textContent = poly.directional_use
        ? `ON · weight ${num(poly.effective_weight, 2)} · SNAPSHOT`
        : 'OFF · API SNAPSHOT';
    } else {
      $('#learn-poly-weight').textContent = 'UNKNOWN · field absent';
    }

    const directional = typeof poly.directional_use === 'boolean'
      ? (poly.directional_use ? 'ON · PAPER EXPERIMENT' : 'OFF') : 'UNKNOWN';
    $('#decision-context').innerHTML = [
      ['Snapshot timestamp', row.ts || row.created_at || 'UNKNOWN'],
      ['Prediction', `${row.prediction || '—'} · raw conviction ${pct(row.confidence)}`],
      ['Spot pressure snapshot', step2.base_total_pressure ?? step2.total_pressure ?? '—'],
      ['Polymarket raw pressure snapshot', poly.raw_directional_pressure ?? '—'],
      ['Polymarket applied snapshot', poly.pressure_component ?? '—'],
      ['Polymarket directional use snapshot', directional],
      ['Polymarket UP 5m snapshot', poly.up_probability != null ? pct(poly.up_probability) : '—'],
      ['Kalshi YES 15m snapshot', kalshiMarket.yes_probability != null ? pct(kalshiMarket.yes_probability) : '—'],
      ['Learning replay N snapshot', decision.learningReplayN],
      ['Current authority N · separate API', decision.authorityN],
    ].map(([name, detail]) => sourceRow(name, detail, 'DECISION_TIME_SNAPSHOT')).join('');
  }

  function renderScore(score) {
    state.score = score;
    // Contract: scoreView consumes authoritative_score_pct as authority and
    // observed_win_rate_pct only as the separately labeled raw diagnostic.
    const view = truth.scoreView(score);
    $('#score-total').textContent = view.totalInputRows;
    $('#score-proof-raw').textContent = view.proofQualifiedRaw;
    $('#score-independent').textContent = view.independent1h;
    $('#score-authority-wr').textContent = view.authorityWr;
    $('#score-authoritative').textContent = view.authoritativeScore;
    $('#score-status').textContent = view.status;
    $('#score-raw-diagnostic').textContent = view.rawObservedWr;
    $('#oracle-score-meta').textContent = `[API_DERIVED] ${view.status} · scope ${view.scope} · cohort ${view.cohort}`;
    const btc = state.predictions.find((row) => symbolKey(row.symbol) === 'BTCUSDT');
    if (btc) renderDecisionContext(btc);
  }

  async function refreshContext() {
    try {
      const payload = await getJSON('/api/market-context');
      renderContext(payload);
      domainSuccess('context');
    } catch (error) {
      domainFailure('context', error);
      const element = $('#conn-status');
      element.className = 'pill pill-red';
      element.textContent = `MARKET CONTEXT ERROR · ${error.message || error}`;
      element.dataset.claimClass = 'UNKNOWN/STALE';
    }
  }

  async function refreshScore() {
    try {
      const payload = await getJSON('/api/oracle/score?symbol=BTCUSDT');
      renderScore(payload);
      domainSuccess('score');
    } catch (error) {
      domainFailure('score', error);
      $('#oracle-score-meta').textContent = `[UNKNOWN/STALE] SCORE ERROR · ${error.message || error}`;
    }
  }

  async function refreshPredictions() {
    try {
      const payload = await getJSON('/api/oracle/predictions/db?limit=50');
      renderPredictions(payload);
      domainSuccess('predictions');
    } catch (error) {
      domainFailure('predictions', error);
      $('#oracle-pred-meta').textContent = `[UNKNOWN/STALE] PREDICTIONS ERROR · ${error.message || error}`;
    }
  }

  async function refreshOracle() {
    await Promise.allSettled([refreshScore(), refreshPredictions()]);
  }

  window.__SENEX_DASHBOARD__ = Object.freeze({
    refreshContext,
    refreshScore,
    refreshPredictions,
    renderScore,
    renderContext,
    renderPredictions,
    state,
  });

  renderDomainHealth();
  refreshContext();
  refreshOracle();
  setInterval(refreshContext, 2000);
  setInterval(refreshOracle, 10000);
  setInterval(renderDomainHealth, 1000);
})();
