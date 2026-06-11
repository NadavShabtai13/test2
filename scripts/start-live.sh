#!/usr/bin/env bash
# Pre-market / live trading launcher for SPY @ 1h.
#
# US RTH opens 09:30 America/New_York ≈ 16:30 Israel (summer IDT).
# Run `preflight` ~5 min before the open; then `dry-run` or `paper`.
#
# Usage:
#   ./scripts/start-live.sh preflight
#   ./scripts/start-live.sh dry-run          # log only, background
#   ./scripts/start-live.sh paper            # IBKR paper (TWS/Gateway on host)

set -euo pipefail
cd "$(dirname "$0")/.."

RUN_ID="${RUN_ID:-3}"
MODE="${1:-preflight}"

# Top-3 pool, ranked by robustness (min IS/OOS Sharpe). Strict OOS sample size.
AUTO_SEL=(
  --run-id "$RUN_ID"
  --strategies 3
  --order-by score
  --min-oos-trades 25
  --min-trades 50
  --min-win-rate 0.78
)

case "$MODE" in
  preflight)
    echo "==> DB check"
    docker compose run --rm app check-db
    echo "==> Optimize status (run #${RUN_ID})"
    docker compose run --rm app status
    echo "==> Auto-select top 3 (promote to live slot)"
    docker compose run --rm app auto-select "${AUTO_SEL[@]}"
    echo "==> One-shot live signal"
    docker compose run --rm app live-signal
    echo ""
    echo "Ready. Before 16:30 Israel run:"
    echo "  ./scripts/start-live.sh dry-run"
    echo "  ./scripts/start-live.sh paper   # needs IBKR TWS paper on port 7497"
    ;;
  dry-run)
    docker compose build app -q
    docker rm -f sptrader-live 2>/dev/null || true
    docker compose run -d --name sptrader-live app \
      live-run --auto --reselect-on-flat --mode dry-run \
      --poll-seconds 600 --combine priority \
      "${AUTO_SEL[@]}"
    echo "sptrader-live started (dry-run). Logs: docker logs -f sptrader-live"
    ;;
  paper)
    docker compose build app -q
    docker rm -f sptrader-live 2>/dev/null || true
    docker compose run -d --name sptrader-live app \
      live-run --auto --reselect-on-flat --mode paper \
      --poll-seconds 600 --combine priority \
      "${AUTO_SEL[@]}"
    echo "sptrader-live started (paper). Logs: docker logs -f sptrader-live"
    ;;
  stop)
    docker stop sptrader-live 2>/dev/null || true
    docker rm sptrader-live 2>/dev/null || true
    echo "sptrader-live stopped."
    ;;
  *)
    echo "Unknown mode: $MODE (preflight | dry-run | paper | stop)"
    exit 1
    ;;
esac
