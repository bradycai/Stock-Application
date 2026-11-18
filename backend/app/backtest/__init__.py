"""Deterministic backtest and replay engine (offline; never touches the broker or Claude).

Pure modules: data (point-in-time view), fills (fill/cost model), strategies, engine
(portfolio simulator, sizes entries through risk.evaluate), metrics, splits.
`run` is the CLI: `python -m app.backtest.run --symbols AAPL,MSFT --start ... --end ...`.
"""
