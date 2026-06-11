"""Phase 2: live / paper trading.

Bridges the research engine to a broker. The same indicator + signal + combine
code that backtests computes the *current* desired exposure from the latest
bars; a broker adapter then reconciles the account position to that target.

Safety: the default mode is ``dry-run`` (decide + log, place no orders). Paper
trading uses an Interactive Brokers paper account. Real-money trading is gated
behind an explicit opt-in flag.
"""
