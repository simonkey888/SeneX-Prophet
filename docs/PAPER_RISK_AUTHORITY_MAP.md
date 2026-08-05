# SENEX Paper Risk Authority Map

| Component | Classification | Evidence |
|---|---|---|
| `polymarket/paper/risk.py` | `AUTHORITATIVE_PAPER_PATH` | Imported by the current paper engine and covered by exact paper tests. |
| `senecio_polymarket/backend/portfolio/risk_kernel.py` | `ACTIVE_LEGACY` | Imported by the legacy portfolio coordinator and oracle runner, not by the current paper path. |
| `senecio_polymarket/backend/kill_switch_store.py` | `ACTIVE_LEGACY` | Persistent state used by the legacy risk kernel. |
| `absolute_kill_switch.py` | `INACTIVE_LEGACY` | Standalone historical component with no current paper-path import. |
| `risk_shadow_mirror.py` | `ACTIVE_LEGACY` | Analytical shadow component imported by `live_bridge_layer.py`; no paper execution authority. |
| `senecio_polymarket/backend/portfolio/execution_engine.py` | `LIVE_ONLY_QUARANTINED` | Legacy execution component excluded from the public-GET paper path by repository gates. |

Exactly one component owns paper risk authority. No new kill switch was created, and no legacy component was integrated or refactored by this mission.
