# AUD-066 Source Matrix

Research-only source classification. `REALIZED_LIQUIDATION` and `ESTIMATED_LIQUIDATION_CLUSTER` are never merged semantically.

| SOURCE | DATA_CLASS | PRIMARY_OR_DERIVED | EXCHANGE_COVERAGE | BTC_5M_RELEVANCE | TIMESTAMP_SEMANTICS | UPDATE_FREQUENCY | HISTORICAL_DEPTH | POINT_IN_TIME_REPLAYABLE | FREE_ACCESS | API_KEY_REQUIRED | PAID_TIER_REQUIRED | RATE_LIMIT | TERMS_OR_LICENSE_CONSTRAINT | SCHEMA_STABILITY | FAILURE_MODES | REPRODUCIBLE | DECISION |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Bybit All Liquidation WS | REALIZED_LIQUIDATION | PRIMARY | Bybit derivatives | HIGH | `T` update ms + `ts` system generation ms; receipt time must be captured locally | 500ms pushes | live stream, no official history endpoint identified | NO for past replay without independent capture | YES | NO | NO | WS connection limits | Bybit API terms | documented | disconnects, late delivery, no historical replay | YES live / NO historical | REFERENCE_LIVE_ONLY |
| Bybit Open Interest REST | OPEN_INTEREST | PRIMARY | Bybit linear/inverse | HIGH | response `timestamp` ms | 5m minimum historical interval | to symbol launch | YES | YES | NO for public market endpoint | NO | public market limits | Bybit API terms | documented | volatility latency/delivery delay | YES | ACCEPT_AS_AUXILIARY |
| Binance futures forceOrder WS | REALIZED_LIQUIDATION | PRIMARY | Binance USD-M/COIN-M | HIGH | exchange event/order fields; local receipt needed | stream snapshots; current official developer docs | live; official public archive currently does not document liquidation CSV | NO for full past replay from Binance archive | YES live | NO for public WS | NO | WS limits | Binance API terms | documented but product docs moved | snapshot sampling, reconnect gaps, historical archive absent | YES live / NO historical | REFERENCE_LIVE_ONLY |
| Binance public-data archive | TRADES_KLINES_MARKET_DATA | PRIMARY | Binance | HIGH baseline/label | exchange timestamps | daily/monthly archives | multi-year | YES | YES | NO | NO | static download | Binance data terms | documented | missing products/periods | YES | ACCEPT_BASELINE_ONLY |
| Tardis first-day CSV samples | REALIZED_LIQUIDATION + TRADES + QUOTES + OI | DERIVED_CAPTURE_OF_PRIMARY_FEEDS | Binance futures and others | HIGH | normalized exchange `timestamp` + collector `local_timestamp`, both microseconds | tick/event | first day of each month free; Binance futures since 2019/2020+ depending type | YES | YES for first day of each month | NO | NO for sample days | bounded static files | Tardis terms; data is normalized from exchange feeds | documented normalized CSV | capture incidents, max-1/s Binance liquidation snapshot semantics, payload size, missing rows | YES for retained sample URLs | ACCEPT_FOR_ZERO_COST_OOS_RESEARCH |
| CoinGlass liquidation heatmap model1/2/3 | ESTIMATED_LIQUIDATION_CLUSTER | DERIVED | multi-exchange | MEDIUM | model output/update time, not realized event time | real time | vendor-dependent | not established at $0 | NO for API models relevant here | YES | YES (Professional/Enterprise documented for model1; paid controls broadly apply) | plan-specific | CoinGlass subscription/API terms | vendor schema | paywall, opaque leverage assumptions/model changes | NO at $0 | REJECT_UNDER_AUD066 |
| CoinAnk liquidation history/orders/maps | REALIZED_STATS and ESTIMATED_CLUSTER by endpoint | DERIVED | multi-exchange | MEDIUM/HIGH | vendor endpoint semantics | vendor | vendor | not established at $0 | NO for required endpoints | YES | VIP1/VIP3/VIP4 by endpoint | plan-specific | CoinAnk API terms | vendor | VIP dependency, model opacity | NO at $0 | REJECT_UNDER_AUD066 |
| TradingView ProjectSyndicate BTC Liquidation Heatmap | ESTIMATED_LIQUIDATION_CLUSTER / VOLUME_ZONE | DERIVED | Binance + Coinbase + Bitstamp claims | LOW-MEDIUM for BTC5m; page says best 4H/D1 | bar-time Pine context | chart recomputation | TradingView chart history | not independently replayable here without platform semantics | script view/use is free | platform account may be needed | NO explicit API | platform | TradingView House Rules; do not copy/execute third-party code | author-controlled | proprietary strength logic claim, volume zones are not realized liquidations | NO for independent AUD066 pipeline | REJECT_AS_EVIDENCE |
| BitcoinCounterFlow liquidation heatmap | ESTIMATED/ANALYTICS_DISPLAY | DERIVED | not sufficiently documented publicly | UNKNOWN | insufficient public contract | UNKNOWN | UNKNOWN | NO | website surface | UNKNOWN | product-dependent | UNKNOWN | site terms | UNKNOWN | methodology/API/history not auditable enough | NO | REJECT_AS_EVIDENCE |
| OKX liquidation-warning WS | ACCOUNT_RISK_WARNING | PRIMARY_PRIVATE | user's OKX positions | NOT market-wide realized flow | private position event/push time | event | private only | NO market-wide | requires account | YES + signing | account access | private channel limits | OKX API agreement | documented | account-specific, possible simultaneous liquidation | NO under governance | REJECT |

## Primary documentation used

- Bybit All Liquidation: https://bybit-exchange.github.io/docs/v5/websocket/public/all-liquidation
- Bybit Open Interest: https://bybit-exchange.github.io/docs/v5/market/open-interest
- Binance public data: https://github.com/binance/binance-public-data
- Tardis Binance futures: https://docs.tardis.dev/historical-data-details/binance-futures
- Tardis downloadable CSV: https://docs.tardis.dev/downloadable-csv-files/overview
- Tardis schemas: https://docs.tardis.dev/downloadable-csv-data-types
- CoinGlass model1: https://github.com/coinglass-official/coinglass-api-docs/blob/main/rest/Futures/Liquidation/liquidation-heatmap.md
- CoinAnk API index: https://docs.coinank.com/docs/api/version-1.html
- TradingView open-source listing: https://www.tradingview.com/script/BqtqW46d-BTC-Liquidation-Heatmap-Multi-Exchange/
- BitcoinCounterFlow liquidations: https://bitcoincounterflow.com/learn/liquidations/
- OKX API: https://www.okx.com/docs-v5/en/

## Zero-cost decision

Tardis first-of-month normalized captures are the only located $0 historical path that simultaneously supplies an exchange timestamp and independent receipt timestamp for realized liquidation replay. Paid heatmaps are not needed to test the realized-flow hypothesis and are excluded. Estimated-cluster value remains a separate hypothesis and is not allowed to inherit any realized-flow result.
