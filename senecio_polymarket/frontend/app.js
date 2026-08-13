/* SENEX REAL-MARKET-V1 — no synthetic dashboard sources */
(() => {
  'use strict';
  const $ = (s) => document.querySelector(s);
  const esc = (v) => String(v ?? '—').replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
  const pct = (v, d=1) => v == null || !Number.isFinite(Number(v)) ? '—' : `${(Number(v)*100).toFixed(d)}%`;
  const num = (v, d=3) => v == null || !Number.isFinite(Number(v)) ? '—' : Number(v).toFixed(d);
  const apr = (v) => v == null || !Number.isFinite(Number(v)) ? '—' : `${(Number(v)*100).toFixed(2)}%`;
  const money = (v, d=0) => v == null || !Number.isFinite(Number(v)) ? '—' : `$${Number(v).toLocaleString(undefined,{maximumFractionDigits:d})}`;
  const clock = (value) => {
    if (!value) return '—';
    let n = Number(value);
    if (Number.isFinite(n)) {
      if (n > 1e12) n /= 1000;
      return new Date(n * 1000).toLocaleTimeString('en-GB', {hour12:false});
    }
    const d = new Date(value);
    return Number.isNaN(d.getTime()) ? '—' : d.toLocaleTimeString('en-GB', {hour12:false});
  };
  const countdown = (s) => {
    if (s == null || !Number.isFinite(Number(s))) return '—';
    s = Math.max(0, Math.round(Number(s)));
    return `${Math.floor(s/60)}:${String(s%60).padStart(2,'0')}`;
  };

  const state = { context: null, predictions: [], score: null };

  async function getJSON(url) {
    const r = await fetch(url, {cache:'no-store'});
    if (!r.ok) throw new Error(`${r.status}`);
    return r.json();
  }

  function setConn(poly) {
    const el = $('#conn-status');
    const live = poly && (poly.status === 'LIVE_WS' || poly.status === 'LIVE_REST');
    el.className = `pill ${live ? 'pill-green' : 'pill-red'}`;
    el.textContent = live ? `POLYMARKET ${poly.status}` : `MARKET ${poly?.status || 'UNAVAILABLE'}`;
  }

  function renderContext(ctx) {
    state.context = ctx;
    const poly = ctx.polymarket || {};
    const kalshi = ctx.kalshi || {};
    const boros = ctx.boros || {};
    const oracle = ctx.oracle || {};
    setConn(poly);
    $('#stat-mode').textContent = ctx.mode || '—';
    $('#stat-clob').textContent = poly.ws_connected ? 'WS LIVE' : (poly.status || '—');
    $('#stat-oracle').textContent = `cycle ${oracle.cycles_run ?? 0}`;
    $('#score-next').textContent = clock(oracle.next_cycle_at);

    const upP = poly.up_probability;
    const downP = poly.down_probability;
    $('#poly-up').textContent = pct(upP);
    $('#poly-down').textContent = pct(downP);
    const pressure = Number(poly.directional_pressure || 0);
    $('#poly-pressure').textContent = `${pressure >= 0 ? '+' : ''}${pressure.toFixed(3)}`;
    $('#poly-pressure').className = `value ${pressure > 0.05 ? 'pos' : pressure < -0.05 ? 'neg' : ''}`;
    $('#poly-close').textContent = countdown(poly.seconds_to_close);
    const market = poly.market || {};
    $('#poly-question').textContent = market.question || 'No current BTC 5m market discovered';
    $('#poly-slug').textContent = market.slug || '—';
    $('#poly-resolution').textContent = `resolution: ${market.resolution_source || '—'}`;
    $('#poly-meta').textContent = `${poly.status || '—'} · fresh ${poly.freshness_s ?? '—'}s`;
    $('#poly-book-meta').textContent = poly.ws_connected ? 'WS subscribed' : 'REST bootstrap';
    renderBook(poly);
    renderPolyFeed(poly.recent_events || []);
    renderKalshi(kalshi);
    renderBoros(boros);

    const safety = ctx.safety || {};
    $('#safe-market-mode').textContent = ctx.mode || '—';
    $('#safe-trade-mode').textContent = safety.trade_mode || 'PAPER';
    $('#safe-live').textContent = safety.live_capital_locked === false ? 'UNLOCKED' : 'LOCKED';
    $('#safe-synth').textContent = ctx.synthetic_demo_enabled ? 'ON' : 'OFF';
    $('#safe-synth').className = `value ${ctx.synthetic_demo_enabled ? 'neg' : 'pos'}`;

    $('#source-integrity').innerHTML = [
      ['Polymarket discovery', poly.market ? 'REAL · Gamma API' : 'UNAVAILABLE'],
      ['Polymarket orderbook', poly.status || 'UNAVAILABLE'],
      ['CLOB WebSocket', poly.ws_connected ? 'CONNECTED' : 'NOT CONNECTED'],
      ['Kalshi KXBTC15M', kalshi.status || 'UNAVAILABLE'],
      ['Boros funding context', boros.status || 'UNAVAILABLE'],
      ['Synthetic feed', ctx.synthetic_demo_enabled ? 'ENABLED' : 'DISABLED'],
      ['Execution', 'PAPER / LIVE LOCKED'],
    ].map(([k,v]) => `<div class="check"><span class="name">${esc(k)}</span><span class="detail">${esc(v)}</span></div>`).join('');
    $('#footer-freshness').textContent = `Poly ${poly.freshness_s ?? '—'}s · Kalshi ${kalshi.freshness_s ?? '—'}s · Boros ${boros.freshness_s ?? '—'}s`;
  }

  function renderBook(poly) {
    const body = $('#poly-book-body');
    const rows = [['UP', poly.up || {}], ['DOWN', poly.down || {}]];
    body.innerHTML = rows.map(([label,b]) => `<tr>
      <td class="sym">${label}</td>
      <td class="num">${num(b.best_bid,3)}</td>
      <td class="num">${num(b.best_ask,3)}</td>
      <td class="num">${num(b.spread,3)}</td>
      <td class="num">${num(b.bid_depth_5,1)}</td>
      <td class="num">${num(b.ask_depth_5,1)}</td>
      <td class="num">${num(b.depth_imbalance,3)}</td>
    </tr>`).join('');
  }

  function renderPolyFeed(events) {
    const feed = $('#poly-feed');
    if (!events.length) {
      feed.innerHTML = '<div class="placeholder" style="padding:12px">waiting for public CLOB events…</div>';
      return;
    }
    feed.innerHTML = events.slice(0,50).map(ev => {
      const detail = [ev.outcome, ev.side, ev.price != null ? `px=${ev.price}` : '', ev.best_bid != null ? `bid=${ev.best_bid}` : '', ev.best_ask != null ? `ask=${ev.best_ask}` : ''].filter(Boolean).join(' ');
      return `<div class="feed-row"><span class="ts">${clock(ev.timestamp)}</span><span class="type type-MARKET_TICK">${esc(ev.event_type || 'CLOB')}</span><span class="body">${esc(detail)}</span></div>`;
    }).join('');
  }

  function renderKalshi(kalshi) {
    const m = kalshi.market || {};
    $('#kalshi-meta').textContent = `${kalshi.status || '—'} · 15m diagnostic`;
    $('#kalshi-yes').textContent = pct(m.yes_probability);
    $('#kalshi-no').textContent = pct(m.no_probability);
    $('#kalshi-volume').textContent = m.volume != null ? Number(m.volume).toLocaleString(undefined,{maximumFractionDigits:0}) : '—';
    $('#kalshi-oi').textContent = m.open_interest != null ? Number(m.open_interest).toLocaleString(undefined,{maximumFractionDigits:0}) : '—';
    $('#kalshi-title').textContent = m.title || 'No open KXBTC15M market';
    $('#kalshi-ticker').textContent = m.ticker || 'KXBTC15M';
    $('#kalshi-status').textContent = `exchange=${m.exchange_active ?? '—'} trading=${m.trading_active ?? '—'} · closes ${countdown(m.seconds_to_close)}`;
  }

  function renderBoros(boros) {
    $('#boros-meta').textContent = `${boros.status || '—'} · non-directional`;
    const rows = Array.isArray(boros.markets) ? boros.markets : [];
    const body = $('#boros-body');
    if (!rows.length) {
      body.innerHTML = `<tr><td colspan="7" class="placeholder">${esc(boros.last_error ? `Boros unavailable: ${boros.last_error}` : 'No BTC/ETH Boros markets returned')}</td></tr>`;
      return;
    }
    body.innerHTML = rows.slice(0,20).map(m => `<tr>
      <td class="sym">${esc(m.underlying_symbol || m.symbol)}</td>
      <td>${esc(m.name || m.market_id)}</td>
      <td class="num">${apr(m.mid_apr)}</td>
      <td class="num">${apr(m.mark_apr)}</td>
      <td class="num">${money(m.asset_mark_price,2)}</td>
      <td class="num">${money(m.volume_24h,0)}</td>
      <td class="num">${money(m.open_interest_notional,0)}</td>
    </tr>`).join('');
  }

  function auditOf(row) { return row && typeof row.audit === 'object' && row.audit ? row.audit : {}; }
  function step2Of(row) {
    const a = auditOf(row); const p = a.pipeline || {};
    return p.step2_features || {};
  }

  function renderPredictions(payload) {
    const rows = Array.isArray(payload.predictions) ? payload.predictions : [];
    state.predictions = rows;
    $('#oracle-pred-meta').textContent = `${payload.total_in_db ?? rows.length} total`;
    const body = $('#oracle-table tbody');
    body.innerHTML = rows.slice(0,30).map(r => {
      const s2 = step2Of(r);
      const learn = s2.learning_state_v1 || {};
      const poly = s2.polymarket_context_v1 || {};
      return `<tr>
        <td>${clock(r.ts || r.created_at)}</td>
        <td class="sym">${esc(r.symbol)}</td>
        <td style="font-weight:700">${esc(r.prediction)}</td>
        <td class="num">${pct(r.confidence)}</td>
        <td>${esc(learn.status || '—')} ${learn.proof_qualified_n != null ? `n=${learn.proof_qualified_n}` : ''}</td>
        <td>${poly.eligible ? `${pct(poly.up_probability)} · p=${num(poly.pressure_component,3)}` : '—'}</td>
        <td>${esc(r.outcome || 'PENDING')}</td>
      </tr>`;
    }).join('') || '<tr><td colspan="7" class="placeholder">no predictions</td></tr>';

    const btc = rows.find(r => r.symbol === 'BTCUSDT');
    if (btc) renderDecisionContext(btc);
  }

  function renderDecisionContext(row) {
    const s2 = step2Of(row);
    const learn = s2.learning_state_v1 || {};
    const poly = s2.polymarket_context_v1 || {};
    const ext = auditOf(row).external_markets_v1 || {};
    const kalshiMarket = ext.kalshi?.market || {};
    $('#learn-state').textContent = learn.status || '—';
    $('#learn-n').textContent = learn.proof_qualified_n ?? '—';
    $('#learn-mutations').textContent = learn.mutations ?? '—';
    $('#decision-context').innerHTML = [
      ['Prediction', `${row.prediction || '—'} · conf ${pct(row.confidence)}`],
      ['Spot pressure', s2.base_total_pressure ?? s2.total_pressure ?? '—'],
      ['Polymarket pressure', poly.pressure_component ?? '—'],
      ['Polymarket UP 5m', poly.up_probability != null ? pct(poly.up_probability) : '—'],
      ['Kalshi YES 15m', kalshiMarket.yes_probability != null ? pct(kalshiMarket.yes_probability) : '—'],
      ['Polymarket eligible', poly.eligible ? 'YES' : 'NO'],
      ['Learning', `${learn.status || '—'} n=${learn.proof_qualified_n ?? 0}`],
    ].map(([k,v]) => `<div class="check"><span class="name">${esc(k)}</span><span class="detail">${esc(v)}</span></div>`).join('');
  }

  function renderScore(s) {
    state.score = s;
    $('#score-total').textContent = s.total_predictions ?? '—';
    $('#score-verified').textContent = s.verified ?? '—';
    $('#score-winrate').textContent = s.verified ? `${Number(s.win_rate_pct || 0).toFixed(1)}%` : 'UNKNOWN';
  }

  async function refreshContext() {
    try { renderContext(await getJSON('/api/market-context')); }
    catch (e) {
      const el = $('#conn-status'); el.className='pill pill-red'; el.textContent=`market context error ${e.message}`;
    }
  }

  async function refreshOracle() {
    try {
      const [score, preds] = await Promise.all([
        getJSON('/api/oracle/score'),
        getJSON('/api/oracle/predictions/db?limit=50'),
      ]);
      renderScore(score); renderPredictions(preds);
    } catch (_) {}
  }

  refreshContext(); refreshOracle();
  setInterval(refreshContext, 2000);
  setInterval(refreshOracle, 10000);
})();
