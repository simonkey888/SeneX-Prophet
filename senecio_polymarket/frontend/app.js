/* SENECIO ORACLE — Polymarket Cockpit Frontend */
(() => {
  'use strict';

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => document.querySelectorAll(sel);

  // ---- state ----
  const state = {
    ws: null,
    sse: null,
    filterType: '',
    events: [],
    maxFeedRows: 200,
    counters: { MARKET_TICK: 0, WALLET_ALERT: 0, MARKET_CANDIDATE: 0, SIGNAL: 0, EXECUTION_SIM: 0, RISK_STATE: 0, AUDIT_TRACE: 0 },
    candidates: [],
    wallets: [],
    positions: new Map(),
    closedPositions: [],
    equityCurve: [10000],
    confidenceHist: [],
    latestSignal: null,
    latestRisk: null,
    cursors: { clob: 0, onchain: 0 },
    scannerAMeta: '—',
    scannerBMeta: '—',
  };

  // ---- WebSocket / SSE ----
  function connect() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = `${proto}//${location.host}/ws`;
    setStatus('amber', 'connecting…');
    try {
      state.ws = new WebSocket(url);
      state.ws.onopen = () => setStatus('green', 'ws live');
      state.ws.onclose = () => {
        setStatus('red', 'ws closed — fallback to SSE');
        state.ws = null;
        connectSSE();
      };
      state.ws.onerror = () => {
        setStatus('red', 'ws error');
        try { state.ws.close(); } catch (_) {}
      };
      state.ws.onmessage = (e) => {
        try {
          const ev = JSON.parse(e.data);
          handleEvent(ev);
        } catch (err) { console.error('parse', err); }
      };
    } catch (e) {
      connectSSE();
    }
  }

  function connectSSE() {
    const src = new EventSource(`/sse`);
    setStatus('amber', 'sse fallback');
    src.addEventListener('message', (e) => {
      try {
        const ev = JSON.parse(e.data);
        handleEvent(ev);
      } catch (_) {}
    });
    src.addEventListener('open', () => setStatus('green', 'sse live'));
    src.addEventListener('error', () => setStatus('red', 'sse error'));
    state.sse = src;
  }

  function setStatus(color, text) {
    const el = $('#conn-status');
    el.className = `pill pill-${color}`;
    el.textContent = text;
  }

  // ---- event handler ----
  function handleEvent(ev) {
    state.events.unshift(ev);
    if (state.events.length > state.maxFeedRows) state.events.pop();
    state.counters[ev.event_type] = (state.counters[ev.event_type] || 0) + 1;

    switch (ev.event_type) {
      case 'MARKET_TICK':       onTick(ev); break;
      case 'WALLET_ALERT':      onWallet(ev); break;
      case 'MARKET_CANDIDATE':  onCandidate(ev); break;
      case 'SIGNAL':            onSignal(ev); break;
      case 'EXECUTION_SIM':     onExec(ev); break;
      case 'RISK_STATE':        onRisk(ev); break;
      case 'AUDIT_TRACE':       /* logged only */ break;
    }
    renderFeedRow(ev);
    renderTopbar();
  }

  function onTick(ev) {
    // update cursor display if present
    const off = ev.payload?.cursor_offset;
    if (typeof off === 'number') state.cursors.clob = off;
  }

  function onWallet(ev) {
    state.cursors.onchain = (ev.payload?.block_number || state.cursors.onchain) - 18_500_000;
    state.wallets.unshift(ev);
    if (state.wallets.length > 20) state.wallets.pop();
    renderWalletTable();
  }

  function onCandidate(ev) {
    state.candidates.unshift(ev);
    if (state.candidates.length > 15) state.candidates.pop();
    if (ev.payload.scanner === 'A_premarket_gap') state.scannerAMeta = `last @ ${new Date(ev.ts).toLocaleTimeString()}`;
    if (ev.payload.scanner === 'B_trend_join_long') state.scannerBMeta = `last @ ${new Date(ev.ts).toLocaleTimeString()}`;
    renderScannerTable();
  }

  function onSignal(ev) {
    state.latestSignal = ev;
    state.confidenceHist.push(ev.payload.confidence);
    if (state.confidenceHist.length > 100) state.confidenceHist.shift();
    renderDecisionTrace();
    drawConfidenceChart();
  }

  function onExec(ev) {
    const p = ev.payload;
    if (p.status === 'FILLED' && p.side === 'BUY') {
      // open position — derive fields from payload.position
      const pos = p.position;
      if (pos) {
        state.positions.set(ev.symbol, {
          symbol: ev.symbol,
          qty: pos.qty,
          entry_price: pos.entry,
          stop: pos.stop,
          target: pos.target,
          ts: ev.ts,
        });
      }
    } else if (p.status === 'FILLED' && p.side === 'SELL') {
      state.positions.delete(ev.symbol);
      state.closedPositions.push({ symbol: ev.symbol, pnl: p.realized_pnl, ts: ev.ts });
    }
    renderPositionsTable();
  }

  function onRisk(ev) {
    state.latestRisk = ev.payload;
    state.equityCurve.push(ev.payload.equity);
    if (state.equityCurve.length > 120) state.equityCurve.shift();
    renderRiskGrid();
    drawEquityChart();
  }

  // ---- renderers ----
  function renderTopbar() {
    const total = Object.values(state.counters).reduce((a, b) => a + b, 0);
    $('#stat-events').innerHTML  = `demo events: <b>${total}</b>`;
    $('#stat-ticks').innerHTML   = `demo ticks: <b>${state.counters.MARKET_TICK}</b>`;
    $('#stat-signals').innerHTML = `demo signals: <b>${state.counters.SIGNAL}</b>`;
    $('#stat-fills').innerHTML   = `demo fills: <b>${state.counters.EXECUTION_SIM}</b>`;
    const eq = state.latestRisk?.equity ?? 10000;
    $('#stat-equity').innerHTML  = `demo equity: <b>$${eq.toLocaleString(undefined, { maximumFractionDigits: 0 })}</b>`;
    const cursor = $('#cursor-display');
    if (cursor) cursor.textContent = `synthetic demo cursor: clob=${state.cursors.clob} / onchain=${state.cursors.onchain}`;
  }

  function renderFeedRow(ev) {
    if (state.filterType && ev.event_type !== state.filterType) return;
    const feed = $('#feed');
    const row = document.createElement('div');
    row.className = 'feed-row';
    const ts = new Date(ev.ts).toLocaleTimeString('en-US', { hour12: false });
    const sym = ev.symbol || '';
    let body = '';
    switch (ev.event_type) {
      case 'MARKET_TICK':
        body = `${sym} $${ev.payload.price} vol=${(ev.payload.volume||0).toFixed(0)} bid=${ev.payload.bid} ask=${ev.payload.ask}`;
        break;
      case 'WALLET_ALERT':
        body = `${ev.payload.wallet?.slice(0,8)} ${ev.payload.action} ${sym} $${(ev.payload.size_usd||0).toLocaleString()} (${ev.payload.label})`;
        break;
      case 'MARKET_CANDIDATE':
        body = `${sym} [${ev.payload.scanner}] score=${ev.payload.score} • ${(ev.payload.reasons||[]).join(' | ')}`;
        break;
      case 'SIGNAL':
        body = `${sym} → ${ev.payload.action} conf=${(ev.payload.confidence*100).toFixed(0)}% ev=${ev.payload.ev?.toFixed(3)} $${ev.payload.sizing_usd}`;
        break;
      case 'EXECUTION_SIM':
        body = `${sym} ${ev.payload.side||''} ${ev.payload.status} qty=${ev.payload.qty} @ $${ev.payload.fill_price} slip=${ev.payload.slippage_bps}bps`;
        break;
      case 'RISK_STATE':
        body = `equity=$${ev.payload.equity} cash=$${ev.payload.cash} open=${ev.payload.open_positions} dd=${ev.payload.drawdown_pct}%`;
        break;
      case 'AUDIT_TRACE':
        body = `[${ev.payload.layer}] ${ev.payload.msg} (${ev.payload.severity})`;
        break;
      default:
        body = JSON.stringify(ev.payload).slice(0, 120);
    }
    row.innerHTML = `<span class="ts">${ts}</span><span class="type type-${ev.event_type}">${ev.event_type.replace('_',' ').slice(0,12)}</span><span class="body">${escapeHtml(body)}</span>`;
    feed.prepend(row);
    // cap feed rows in DOM
    while (feed.children.length > state.maxFeedRows) feed.removeChild(feed.lastChild);
  }

  function renderScannerTable() {
    const tbody = $('#scanner-table tbody');
    tbody.innerHTML = state.candidates.slice(0, 10).map((c, i) => `
      <tr>
        <td>${i+1}</td>
        <td class="sym">${c.symbol}</td>
        <td>${c.payload.scanner === 'A_premarket_gap' ? 'A·GAP' : 'B·TJL'}</td>
        <td class="num">${c.payload.score.toFixed(1)}</td>
        <td style="color:var(--text-dim);font-size:10px;">${(c.payload.reasons||[]).slice(0,2).join(' · ')}</td>
        <td style="color:var(--text-faint);font-size:10px;">${new Date(c.ts).toLocaleTimeString('en-US',{hour12:false})}</td>
      </tr>
    `).join('');
    $('#scanner-meta').textContent = `A: ${state.scannerAMeta} | B: ${state.scannerBMeta}`;
  }

  function renderWalletTable() {
    const tbody = $('#wallet-table tbody');
    tbody.innerHTML = state.wallets.slice(0, 10).map(w => `
      <tr>
        <td style="font-size:10px;">${(w.payload.wallet||'').slice(0,10)}</td>
        <td class="sym">${w.symbol || w.payload.token}</td>
        <td style="color:${actionColor(w.payload.action)};font-weight:600;">${w.payload.action}</td>
        <td class="num">$${(w.payload.size_usd||0).toLocaleString(undefined,{maximumFractionDigits:0})}</td>
        <td style="color:var(--text-dim);font-size:10px;">${w.payload.label}</td>
        <td style="color:var(--text-faint);font-size:10px;">${new Date(w.ts).toLocaleTimeString('en-US',{hour12:false})}</td>
      </tr>
    `).join('');
    $('#wallet-meta').textContent = `${state.wallets.length} recent`;
  }

  function actionColor(a) {
    if (['BUY','ACCUMULATE'].includes(a)) return 'var(--long)';
    if (['SELL','DISTRIBUTE'].includes(a)) return 'var(--short)';
    if (['CONCENTRATION_ALERT','CLUSTER_ALERT'].includes(a)) return 'var(--warn)';
    return 'var(--text-dim)';
  }

  function renderDecisionTrace() {
    const sig = state.latestSignal;
    const el = $('#decision-trace');
    if (!sig) return;
    const checks = sig.payload.checks || {};
    const order = ['base_rate','catalyst','market_structure','wallet_behavior','calibration'];
    const checkHtml = order.map(k => {
      const c = checks[k] || {};
      const pass = c.pass;
      const detail = Object.entries(c)
        .filter(([kk]) => kk !== 'pass')
        .map(([kk,v]) => `${kk}=${typeof v === 'object' ? JSON.stringify(v) : (typeof v === 'number' ? round(v) : v)}`)
        .join(' · ');
      return `<div class="check">
        <span class="icon ${pass ? 'pass' : 'fail'}">${pass ? '✓' : '✕'}</span>
        <span class="name">${k}</span>
        <span class="detail">${escapeHtml(detail)}</span>
        <span class="pct">${pass ? 'PASS' : 'FAIL'}</span>
      </div>`;
    }).join('');
    const reasons = (sig.payload.reasons || []).map(r => `<li>${escapeHtml(r)}</li>`).join('');
    el.innerHTML = `
      ${checkHtml}
      <div class="decision-action action-${sig.payload.action}">
        <span class="act action-${sig.payload.action}">${sig.payload.action} · ${sig.symbol}</span>
        <span class="sizing">size $${sig.payload.sizing_usd} · conf ${(sig.payload.confidence*100).toFixed(0)}% · EV ${sig.payload.ev?.toFixed(3)}</span>
      </div>
      <ul class="decision-reasons">${reasons}</ul>
    `;
  }

  function renderPositionsTable() {
    const tbody = $('#positions-table tbody');
    const rows = [...state.positions.values()].map(p => `
      <tr>
        <td class="sym">${p.symbol}</td>
        <td class="num">${p.qty.toFixed(4)}</td>
        <td class="num">$${p.entry_price.toFixed(2)}</td>
        <td class="num" style="color:var(--danger);">$${p.stop.toFixed(2)}</td>
        <td class="num" style="color:var(--accent);">$${p.target.toFixed(2)}</td>
        <td style="color:var(--text-faint);font-size:10px;">${new Date(p.ts).toLocaleTimeString('en-US',{hour12:false})}</td>
      </tr>
    `).join('');
    tbody.innerHTML = rows || `<tr><td colspan="6" style="color:var(--text-faint);text-align:center;padding:12px;">no open positions</td></tr>`;
  }

  function renderRiskGrid() {
    const r = state.latestRisk;
    if (!r) return;
    const cells = [
      { label: 'Equity', value: `$${r.equity.toLocaleString(undefined,{maximumFractionDigits:0})}`, cls: r.equity >= 10000 ? 'pos' : 'neg' },
      { label: 'Cash', value: `$${r.cash.toLocaleString(undefined,{maximumFractionDigits:0})}` },
      { label: 'Realized PnL', value: `$${r.realized_pnl.toLocaleString(undefined,{maximumFractionDigits:0})}`, cls: r.realized_pnl >= 0 ? 'pos' : 'neg' },
      { label: 'Drawdown', value: `${r.drawdown_pct}%`, cls: r.drawdown_pct > 0 ? 'neg' : 'pos' },
      { label: 'Open Pos', value: `${r.open_positions}` },
      { label: 'Win Rate', value: `${r.win_rate.toFixed(1)}%`, cls: r.win_rate >= 50 ? 'pos' : 'neg' },
    ];
    $('#risk-grid').innerHTML = cells.map(c => `
      <div class="risk-cell">
        <div class="label">${c.label}</div>
        <div class="value ${c.cls || ''}">${c.value}</div>
      </div>
    `).join('');
  }

  // ---- charts ----
  function drawEquityChart() {
    const cv = $('#equity-chart');
    const ctx = cv.getContext('2d');
    // Set canvas pixel size based on displayed CSS size (retina-aware)
    const cssW = cv.clientWidth || cv.parentElement.clientWidth || 320;
    const cssH = cv.clientHeight || 140;
    const dpr = window.devicePixelRatio || 1;
    cv.width = Math.floor(cssW * dpr);
    cv.height = Math.floor(cssH * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const w = cssW;
    const h = cssH;
    ctx.clearRect(0, 0, w, h);
    // bg
    ctx.fillStyle = '#11161f';
    ctx.fillRect(0, 0, w, h);
    if (state.equityCurve.length < 2) {
      ctx.fillStyle = '#4d5666';
      ctx.font = '11px monospace';
      ctx.fillText('collecting data…', 12, h / 2);
      return;
    }
    const min = Math.min(...state.equityCurve);
    const max = Math.max(...state.equityCurve);
    const range = Math.max(1, max - min);
    // baseline
    ctx.strokeStyle = '#1c2330';
    ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const y = (h * i) / 4;
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(w, y);
      ctx.stroke();
    }
    // line
    ctx.strokeStyle = '#00ffa3';
    ctx.lineWidth = 2;
    ctx.beginPath();
    state.equityCurve.forEach((v, i) => {
      const x = (i / (state.equityCurve.length - 1)) * w;
      const y = h - ((v - min) / range) * h * 0.9 - h * 0.05;
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
    // fill
    ctx.lineTo(w, h);
    ctx.lineTo(0, h);
    ctx.closePath();
    ctx.fillStyle = 'rgba(0,255,163,0.12)';
    ctx.fill();
    // labels
    ctx.fillStyle = '#4d5666';
    ctx.font = '11px monospace';
    ctx.fillText(`$${max.toFixed(0)}`, 8, 14);
    ctx.fillText(`$${min.toFixed(0)}`, 8, h - 4);
  }

  function drawConfidenceChart() {
    const cv = $('#confidence-chart');
    const ctx = cv.getContext('2d');
    const cssW = cv.clientWidth || cv.parentElement.clientWidth || 320;
    const cssH = cv.clientHeight || 140;
    const dpr = window.devicePixelRatio || 1;
    cv.width = Math.floor(cssW * dpr);
    cv.height = Math.floor(cssH * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const w = cssW;
    const h = cssH;
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = '#11161f';
    ctx.fillRect(0, 0, w, h);
    if (state.confidenceHist.length === 0) {
      ctx.fillStyle = '#4d5666';
      ctx.font = '11px monospace';
      ctx.fillText('awaiting signals…', 12, h / 2);
      return;
    }
    // histogram of confidence (0..1) — 10 bins
    const bins = new Array(10).fill(0);
    state.confidenceHist.forEach(c => {
      const idx = Math.min(9, Math.floor(c * 10));
      bins[idx]++;
    });
    const maxBin = Math.max(...bins, 1);
    const barW = w / 10 - 4;
    ctx.fillStyle = '#00d4ff';
    bins.forEach((b, i) => {
      const barH = (b / maxBin) * h * 0.85;
      const x = i * (w / 10) + 2;
      const y = h - barH;
      ctx.fillRect(x, y, barW, barH);
    });
    // threshold line at 0.55
    const tx = 0.55 * w;
    ctx.strokeStyle = '#ff3d6e';
    ctx.setLineDash([4, 4]);
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(tx, 0); ctx.lineTo(tx, h);
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.fillStyle = '#ff3d6e';
    ctx.font = '10px monospace';
    ctx.fillText('min 0.55', tx + 4, 12);
  }

  // ---- utils ----
  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }
  function round(v) {
    if (typeof v !== 'number') return v;
    return Math.round(v * 1000) / 1000;
  }

  // ---- filter chips ----
  $$('.chip').forEach(c => c.addEventListener('click', () => {
    $$('.chip').forEach(x => x.classList.remove('active'));
    c.classList.add('active');
    state.filterType = c.dataset.type;
    // re-render feed from buffer
    $('#feed').innerHTML = '';
    state.events.slice().reverse().forEach(ev => renderFeedRow(ev));
  }));

  // ---- resize handler: redraw charts on window resize / orientation change ----
  let resizeTimer = null;
  function onResize() {
    if (resizeTimer) clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      drawEquityChart();
      drawConfidenceChart();
    }, 150);
  }
  window.addEventListener('resize', onResize);
  window.addEventListener('orientationchange', () => setTimeout(onResize, 300));

  // =====================================================================
  // ORACLE PANEL (ACT XIX) — Real predictions from Supabase via API
  // =====================================================================
  const oracle = {
    predictions: [],
    score: null,
    health: null,
    confChart: null,
    refreshTimer: null,
  };

  function fmtTs(iso) {
    if (!iso) return '—';
    try {
      const d = new Date(iso);
      return d.toLocaleTimeString('en-GB', { hour12: false }) + ' ' +
             d.toLocaleDateString('en-GB', { day: '2-digit', month: 'short' });
    } catch (_) { return iso.slice(11, 19); }
  }

  function fmtPrice(p) {
    if (p == null) return '—';
    return '$' + Number(p).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function fmtEv(ev) {
    if (ev == null) return '—';
    const v = Number(ev);
    const sign = v >= 0 ? '+' : '';
    return sign + v.toExponential(2);
  }

  function fmtConf(c) {
    if (c == null) return '—';
    return (Number(c) * 100).toFixed(1) + '%';
  }

  function fmtCountdown(iso) {
    if (!iso) return '—';
    const diff = new Date(iso).getTime() - Date.now();
    if (diff <= 0) return 'now';
    const min = Math.floor(diff / 60000);
    const sec = Math.floor((diff % 60000) / 1000);
    return min + 'm' + (sec < 10 ? '0' : '') + sec + 's';
  }

  function renderOracleTable() {
    const tbody = document.querySelector('#oracle-table tbody');
    if (!tbody) return;
    if (!oracle.predictions.length) {
      tbody.innerHTML = '<tr><td colspan="8" class="placeholder">no predictions yet</td></tr>';
      return;
    }
    const rows = oracle.predictions.slice(0, 50).map(p => {
      const pred = (p.prediction || '').toLowerCase();
      const out = p.outcome || (p.price_15m_later == null ? 'PEND' : '—');
      return `<tr>
        <td style="font-size:10px;color:var(--text-faint)">${fmtTs(p.ts)}</td>
        <td class="sym">${p.symbol || '—'}</td>
        <td class="pred-${pred}">${p.prediction || '—'}</td>
        <td class="num">${fmtConf(p.confidence)}</td>
        <td class="num">${fmtEv(p.ev)}</td>
        <td class="num">${fmtPrice(p.price_now)}</td>
        <td class="exchange">${p.exchange_used || '—'}</td>
        <td class="outcome-${out}">${out}</td>
      </tr>`;
    }).join('');
    tbody.innerHTML = rows;
    const meta = document.getElementById('oracle-pred-meta');
    if (meta) meta.textContent = `${oracle.predictions.length} shown`;
  }

  function renderOracleScore() {
    const s = oracle.score || {};
    const h = oracle.health || {};
    const total = document.getElementById('score-total');
    const verified = document.getElementById('score-verified');
    const winrate = document.getElementById('score-winrate');
    const next = document.getElementById('score-next');
    const shadow = document.getElementById('score-btc-shadow');
    const shadowDetail = document.getElementById('score-btc-shadow-detail');
    const arbiter = document.getElementById('score-arbiter');
    const arbiterDetail = document.getElementById('score-arbiter-detail');
    if (total) total.textContent = s.total_predictions ?? h.oracle?.supabase_total ?? '—';
    if (verified) verified.textContent = s.verified ?? '—';
    if (winrate) {
      const wr = s.win_rate_pct;
      winrate.textContent = wr != null ? wr.toFixed(1) + '%' : '—';
      const card = winrate.closest('.score-card');
      if (card) card.classList.toggle('bad', wr != null && wr < 50);
    }
    if (next) next.textContent = fmtCountdown(h.oracle?.next_cycle_at);
    if (shadow) {
      const v2 = oracle.shadow || {};
      shadow.textContent = `${v2.shadow_action || 'FLAT'} · ${v2.gate_status || 'UNKNOWN'}`;
      const card = shadow.closest('.score-card');
      if (card) {
        card.classList.toggle('pass', v2.gate_status === 'PASS');
        card.classList.toggle('reject', v2.gate_status === 'REJECT');
      }
      if (shadowDetail) {
        const c = v2.cohort || {};
        shadowDetail.textContent = c.n != null
          ? `recent n=${c.n} · posterior=${((c.posterior_accuracy || 0) * 100).toFixed(1)}% · lower95=${((c.wilson_lower_95 || 0) * 100).toFixed(1)}%`
          : (v2.reasons || ['no verified cohort']).join(' · ');
      }
    }
    if (arbiter) {
      const v3 = oracle.arbiter || {};
      arbiter.textContent = `${v3.action || 'FLAT'} · ${v3.decision || 'UNKNOWN'}`;
      if (arbiterDetail) arbiterDetail.textContent = (v3.reasons || ['waiting for both engines']).join(' · ');
    }
  }

  function drawOracleConfChart() {
    const canvas = document.getElementById('oracle-conf-chart');
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const dpr = window.devicePixelRatio || 1;
    const W = canvas.clientWidth * dpr;
    const H = (canvas.clientHeight || 120) * dpr;
    canvas.width = W; canvas.height = H;
    ctx.clearRect(0, 0, W, H);

    const preds = oracle.predictions.slice(0, 50).reverse(); // chronological
    if (preds.length < 2) {
      ctx.fillStyle = '#4d5666';
      ctx.font = (11 * dpr) + 'px monospace';
      ctx.textAlign = 'center';
      ctx.fillText('Need ≥2 predictions to draw trend', W / 2, H / 2);
      return;
    }

    const padL = 30 * dpr, padR = 10 * dpr, padT = 10 * dpr, padB = 16 * dpr;
    const plotW = W - padL - padR;
    const plotH = H - padT - padB;

    // grid + axes (0% to 100% confidence)
    ctx.strokeStyle = '#1c2330';
    ctx.lineWidth = 1 * dpr;
    ctx.fillStyle = '#4d5666';
    ctx.font = (9 * dpr) + 'px monospace';
    ctx.textAlign = 'right';
    for (let pct = 0; pct <= 100; pct += 25) {
      const y = padT + plotH * (1 - pct / 100);
      ctx.beginPath();
      ctx.moveTo(padL, y); ctx.lineTo(W - padR, y); ctx.stroke();
      ctx.fillText(pct + '%', padL - 4 * dpr, y + 3 * dpr);
    }

    // points + line
    const points = preds.map((p, i) => {
      const x = padL + (i / (preds.length - 1)) * plotW;
      const conf = Number(p.confidence) || 0;
      const y = padT + plotH * (1 - conf);
      return { x, y, pred: p.prediction, conf };
    });

    // line
    ctx.strokeStyle = '#00ffa3';
    ctx.lineWidth = 1.5 * dpr;
    ctx.beginPath();
    points.forEach((p, i) => i === 0 ? ctx.moveTo(p.x, p.y) : ctx.lineTo(p.x, p.y));
    ctx.stroke();

    // points colored by direction
    points.forEach(p => {
      ctx.fillStyle = p.pred === 'LONG' ? '#00ffa3' : p.pred === 'SHORT' ? '#ff3d6e' : '#ffb000';
      ctx.beginPath();
      ctx.arc(p.x, p.y, 2.5 * dpr, 0, Math.PI * 2);
      ctx.fill();
    });
  }

  async function fetchOracleData() {
    try {
      const [predRes, scoreRes, healthRes, shadowRes, arbiterRes] = await Promise.all([
        fetch('/api/oracle/predictions/db?limit=50').then(r => r.json()),
        fetch('/api/oracle/score').then(r => r.json()).catch(() => null),
        fetch('/api/health').then(r => r.json()),
        fetch('/api/oracle/btc-v2-shadow').then(r => r.json()).catch(() => null),
        fetch('/api/oracle/arbiter-v3').then(r => r.json()).catch(() => null),
      ]);
      oracle.predictions = predRes.predictions || [];
      oracle.score = scoreRes;
      oracle.health = healthRes;
      oracle.shadow = shadowRes;
      oracle.arbiter = arbiterRes;
      renderOracleTable();
      renderOracleScore();
      drawOracleConfChart();
    } catch (e) {
      console.error('oracle fetch error', e);
    }
  }

  function startOraclePanel() {
    fetchOracleData();
    // Refresh every 60s (cycle is 15min, but countdown + new rows should update)
    oracle.refreshTimer = setInterval(fetchOracleData, 60000);
    // Update countdown every 5s without refetching
    setInterval(() => {
      if (oracle.health?.oracle?.next_cycle_at) renderOracleScore();
    }, 5000);
    // Redraw chart on resize
    window.addEventListener('resize', () => setTimeout(drawOracleConfChart, 150));
  }

  // ---- initial fetch ----
  async function bootstrap() {
    try {
      const r = await fetch('/api/stats').then(r => r.json());
      if (r.cursors) state.cursors = { clob: r.cursors.clob?.offset || 0, onchain: r.cursors.onchain?.offset || 0 };
      renderTopbar();
    } catch (_) {}
    connect();
    startOraclePanel();
    // periodic refresh of stats (every 5s)
    setInterval(async () => {
      try {
        const r = await fetch('/api/stats').then(r => r.json());
        if (r.cursors) state.cursors = { clob: r.cursors.clob?.offset || 0, onchain: r.cursors.onchain?.offset || 0 };
      } catch (_) {}
    }, 5000);
  }

  bootstrap();
})();
