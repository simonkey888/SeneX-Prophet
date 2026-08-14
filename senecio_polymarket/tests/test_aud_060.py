from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"


class _DashboardParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: list[str] = []
        self.panels: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids.append(values["id"])
        classes = set((values.get("class") or "").split())
        if "panel" in classes:
            self.panels.append(values)


class DashboardTruthModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if shutil.which("node") is None:
            raise unittest.SkipTest("node is required for frontend behavioral regressions")

    def _run_node(self, source: str) -> dict:
        result = subprocess.run(
            ["node", "-e", source],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_raw18_independent10_and_null_authority_are_distinct(self):
        module = (FRONTEND / "dashboard_truth.js").as_posix()
        payload = {
            "total_predictions": 18,
            "proof_qualified_rows_raw": 18,
            "independent_1h_rows": 10,
            "verified": 10,
            "observed_win_rate_pct": 77.8,
            "authoritative_score_pct": None,
            "score_status": "INSUFFICIENT_EVIDENCE",
            "authority_1h": {"global": {"verified": 10, "win_rate_pct": 60.0}},
        }
        output = self._run_node(
            f"""
            const truth = require({json.dumps(module)});
            const view = truth.scoreView({json.dumps(payload)});
            console.log(JSON.stringify(view));
            """
        )
        self.assertEqual(output["proofQualifiedRaw"], "18")
        self.assertEqual(output["independent1h"], "10")
        self.assertEqual(output["authorityN"], "10")
        self.assertEqual(output["authorityWr"], "60.0%")
        self.assertEqual(output["authoritativeScore"], "—")
        self.assertEqual(output["status"], "INSUFFICIENT_EVIDENCE")
        self.assertEqual(output["rawObservedWr"], "77.8%")
        self.assertEqual(output["rawObservedClaim"], "DIAGNOSTIC")
        self.assertEqual(output["authorityClaim"], "API_DERIVED")

    def test_learning_snapshot_excludes_current_score_authority_n(self):
        module = (FRONTEND / "dashboard_truth.js").as_posix()
        row = {
            "symbol": "BTCUSDT",
            "prediction": "LONG",
            "audit": {
                "pipeline": {
                    "step2_features": {
                        "learning_state_v1": {
                            "status": "ACTIVE",
                            "proof_qualified_n": 7,
                            "mutations": 2,
                        }
                    }
                }
            },
        }
        output = self._run_node(
            f"""
            const truth = require({json.dumps(module)});
            const view = truth.decisionView({json.dumps(row)}, {{independent_1h_rows: 10}});
            console.log(JSON.stringify(view));
            """
        )
        self.assertEqual(output["learningReplayN"], "7")
        self.assertEqual(output["claimClass"], "DECISION_TIME_SNAPSHOT")
        self.assertNotIn("authorityN", output)

    def test_fetch_failure_preserves_last_success_but_marks_stale_then_recovers(self):
        module = (FRONTEND / "dashboard_truth.js").as_posix()
        output = self._run_node(
            f"""
            const truth = require({json.dumps(module)});
            let state = truth.domainState();
            state = truth.domainSuccess(state, 1000);
            const failed = truth.domainFailure(state, 'HTTP 503', 5000);
            const recovered = truth.domainSuccess(failed, 9000);
            console.log(JSON.stringify({{failed, recovered}}));
            """
        )
        self.assertEqual(output["failed"]["status"], "ERROR")
        self.assertTrue(output["failed"]["stale"])
        self.assertEqual(output["failed"]["lastSuccessMs"], 1000)
        self.assertEqual(output["failed"]["error"], "HTTP 503")
        self.assertEqual(output["recovered"]["status"], "OK")
        self.assertFalse(output["recovered"]["stale"])
        self.assertIsNone(output["recovered"]["error"])

    def test_missing_safety_and_source_fields_are_unknown_not_positive(self):
        module = (FRONTEND / "dashboard_truth.js").as_posix()
        output = self._run_node(
            f"""
            const truth = require({json.dumps(module)});
            console.log(JSON.stringify(truth.safetyView({{polymarket: {{}}, kalshi: {{}}, boros: {{}}, safety: {{}}}})));
            """
        )
        for key in (
            "tradeMode", "liveCapital", "orders", "readOnlyAdapters",
            "syntheticScheduler", "polymarketDirectional", "kalshiDirectional",
            "borosDirectional",
        ):
            self.assertIn("UNKNOWN", output[key], key)

    def test_main_refresh_flow_marks_stale_preserves_values_and_recovers(self):
        module = json.dumps((FRONTEND / "dashboard_truth.js").as_posix())
        app = json.dumps((FRONTEND / "app.js").as_posix())
        source = r"""
        const fs = require('fs');
        const vm = require('vm');
        class FakeElement {
          constructor(domain = '') {
            this.textContent = '';
            this.innerHTML = '';
            this.className = '';
            this.dataset = {domain};
            this.classList = {toggle: (name, on) => {
              const values = new Set(this.className.split(/\s+/).filter(Boolean));
              if (on) values.add(name); else values.delete(name);
              this.className = [...values].join(' ');
            }};
          }
        }
        const elements = new Map();
        const get = (selector) => {
          if (!elements.has(selector)) elements.set(selector, new FakeElement());
          return elements.get(selector);
        };
        const panels = [new FakeElement('score context'), new FakeElement('predictions'), new FakeElement('context')];
        global.window = {SenexDashboardTruth: require(__MODULE__)};
        global.document = {
          querySelector: get,
          querySelectorAll: (selector) => selector === '.panel[data-domain]' ? panels : [],
        };
        global.setInterval = () => 0;
        let failScore = false;
        let failContext = false;
        const score = {
          total_predictions: 18,
          proof_qualified_rows_raw: 18,
          independent_1h_rows: 10,
          observed_win_rate_pct: 77.8,
          authoritative_score_pct: null,
          score_status: 'INSUFFICIENT_EVIDENCE',
          requested_symbol: 'BTCUSDT',
          authority_cohort: 'INDEPENDENT_NONOVERLAP_1H',
          authority_1h: {global: {verified: 10, win_rate_pct: 60}},
        };
        const context = {
          mode: 'REAL_ONLY', synthetic_demo_enabled: false,
          polymarket: {source: 'POLYMARKET_PUBLIC', status: 'LIVE_WS', read_only: true, ws_connected: true, market: {slug: 'btc'}, recent_events: []},
          kalshi: {status: 'LIVE', directional_use: false, market: {}},
          boros: {status: 'LIVE', directional_use: false, markets: []},
          oracle: {cycles_run: 2},
          safety: {trade_mode: 'PAPER', live_capital_locked: true, orders_enabled: false, read_only_market_adapters: true},
        };
        const predictions = {total_in_db: 100, predictions: [{
          ts: '2026-08-14T02:57:00Z', symbol: 'BTCUSDT', prediction: 'LONG', confidence: 0.73,
          audit: {pipeline: {step2_features: {
            learning_state_v1: {status: 'ACTIVE', proof_qualified_n: 7, mutations: 0},
            polymarket_context_v1: {directional_use: false, up_probability: 0.54},
          }}},
        }]};
        global.fetch = async (url) => {
          const isScore = url.includes('/score');
          const isContext = url.includes('market-context');
          if ((isScore && failScore) || (isContext && failContext)) return {ok: false, status: 503, json: async () => ({})};
          const payload = isScore ? score : isContext ? context : predictions;
          return {ok: true, status: 200, json: async () => payload};
        };
        vm.runInThisContext(fs.readFileSync(__APP__, 'utf8'), {filename: __APP__});
        (async () => {
          const api = window.__SENEX_DASHBOARD__;
          await Promise.all([api.refreshContext(), api.refreshScore(), api.refreshPredictions()]);
          const before = {
            score: get('#score-proof-raw').textContent,
            connText: get('#conn-status').textContent,
            connClass: get('#conn-status').className,
            decision: get('#decision-context').innerHTML,
          };

          failScore = true;
          await api.refreshScore();
          const scoreStale = {
            scoreHealth: get('#health-score').textContent,
            preserved: get('#score-proof-raw').textContent,
            decision: get('#decision-context').innerHTML,
          };
          failScore = false;
          await api.refreshScore();

          failContext = true;
          await api.refreshContext();
          const contextStale = {
            contextHealth: get('#health-context').textContent,
            connText: get('#conn-status').textContent,
            connClass: get('#conn-status').className,
            connClaim: get('#conn-status').dataset.claimClass,
            stats: ['#stat-mode', '#stat-clob', '#stat-oracle', '#stat-live'].map((selector) => ({
              text: get(selector).textContent,
              claim: get(selector).dataset.claimClass,
            })),
          };
          failContext = false;
          await api.refreshContext();
          console.log(JSON.stringify({before, scoreStale, contextStale, recovered: {
            scoreHealth: get('#health-score').textContent,
            contextHealth: get('#health-context').textContent,
            connText: get('#conn-status').textContent,
            connClass: get('#conn-status').className,
            stats: ['#stat-mode', '#stat-clob', '#stat-oracle', '#stat-live'].map((selector) => ({
              text: get(selector).textContent,
              claim: get(selector).dataset.claimClass,
            })),
          }}));
        })().catch((error) => { console.error(error); process.exitCode = 1; });
        """.replace("__MODULE__", module).replace("__APP__", app)
        output = self._run_node(source)
        self.assertEqual(output["before"]["score"], "18")
        self.assertIn("LIVE_WS", output["before"]["connText"])
        self.assertIn("pill-green", output["before"]["connClass"])
        self.assertIn("DECISION_TIME_SNAPSHOT", output["before"]["decision"])
        self.assertNotIn("Current authority N", output["before"]["decision"])

        self.assertEqual(output["scoreStale"]["preserved"], "18")
        self.assertIn("ERROR", output["scoreStale"]["scoreHealth"])
        self.assertIn("STALE", output["scoreStale"]["scoreHealth"])
        self.assertIn("DECISION_TIME_SNAPSHOT", output["scoreStale"]["decision"])
        self.assertNotIn("Current authority N", output["scoreStale"]["decision"])

        self.assertIn("ERROR", output["contextStale"]["contextHealth"])
        self.assertIn("STALE", output["contextStale"]["contextHealth"])
        self.assertIn("ERROR", output["contextStale"]["connText"])
        self.assertIn("pill-red", output["contextStale"]["connClass"])
        self.assertEqual(output["contextStale"]["connClaim"], "UNKNOWN/STALE")
        for stat in output["contextStale"]["stats"]:
            self.assertIn("STALE", stat["text"])
            self.assertEqual(stat["claim"], "UNKNOWN/STALE")

        self.assertIn("OK", output["recovered"]["scoreHealth"])
        self.assertIn("OK", output["recovered"]["contextHealth"])
        self.assertNotIn("STALE", output["recovered"]["connText"])
        self.assertIn("pill-green", output["recovered"]["connClass"])
        for stat in output["recovered"]["stats"]:
            self.assertNotIn("STALE", stat["text"])
            self.assertEqual(stat["claim"], "API_DERIVED")


class DashboardDomContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.html = (FRONTEND / "index.html").read_text(encoding="utf-8")
        self.app = (FRONTEND / "app.js").read_text(encoding="utf-8")
        self.css = (FRONTEND / "styles.css").read_text(encoding="utf-8")
        self.parser = _DashboardParser()
        self.parser.feed(self.html)

    def test_every_panel_has_one_allowed_visible_claim_class(self):
        allowed = {
            "RUNTIME_OBSERVED", "API_DERIVED", "STATIC_POLICY", "DIAGNOSTIC",
            "DECISION_TIME_SNAPSHOT", "UNKNOWN/STALE",
        }
        self.assertGreaterEqual(len(self.parser.panels), 10)
        for panel in self.parser.panels:
            self.assertIn(panel.get("data-claim-class"), allowed, panel.get("id"))
        for claim in allowed:
            self.assertIn(claim, self.html)

    def test_required_metric_and_health_ids_are_unique(self):
        required = {
            "score-total", "score-proof-raw", "score-independent", "score-authority-wr",
            "score-authoritative", "score-status", "score-raw-diagnostic",
            "health-context", "health-score", "health-predictions", "stat-live",
            "safe-orders", "footer-safety",
        }
        self.assertTrue(required.issubset(set(self.parser.ids)))
        duplicates = {item for item in self.parser.ids if self.parser.ids.count(item) > 1}
        self.assertEqual(duplicates, set())
        self.assertNotIn('id="score-verified"', self.html)

    def test_hardcoded_runtime_health_and_obsolete_version_are_absent(self):
        self.assertNotIn("REAL-MARKET-V1-AUD055", self.html + self.app)
        self.assertNotIn("<b>LOCKED</b>", self.html)
        self.assertNotRegex(self.html, r'id="safe-live"[^>]*>LOCKED<')
        self.assertNotRegex(self.html, r'id="safe-synth"[^>]*>OFF<')
        self.assertNotRegex(self.html, r'id="learn-poly-weight"[^>]*>OFF')
        self.assertNotIn("['Execution', 'PAPER / LIVE LOCKED']", self.app)
        self.assertNotIn("['Poly directional fusion', 'OFF BY DEFAULT']", self.app)

    def test_stale_observability_and_decision_snapshot_are_explicit(self):
        self.assertNotIn("catch (_) {}", self.app)
        self.assertIn("domainFailure", self.app)
        self.assertIn("domainSuccess", self.app)
        self.assertIn("DECISION_TIME_SNAPSHOT", self.html)
        self.assertIn("not live market", self.html.lower())
        self.assertIn("cross-symbol", self.html.lower())
        self.assertNotIn("Current authority N", self.app + self.html)

    def test_responsive_contract_and_javascript_syntax(self):
        for breakpoint in ("1280px", "1024px", "900px", "768px", "480px"):
            self.assertIn(breakpoint, self.css)
        if shutil.which("node") is None:
            self.skipTest("node is required for JavaScript syntax verification")
        for script in ("dashboard_truth.js", "app.js"):
            result = subprocess.run(
                ["node", "--check", str(FRONTEND / script)],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
