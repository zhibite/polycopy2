#!/usr/bin/env bash
# Control the on-chain paper bot on the VPS (main only; data-api and legs retired).
# Usage: scripts/onchain_ctl.sh {deploy|start|stop|restart|status|report|verify|log|monitor-start|monitor-stop|monitor-status|bucket}
set -euo pipefail

HOST="${POLY_HOST:?set POLY_HOST to user@your-vps}"
KEY="${POLY_KEY:-$HOME/.ssh/polycopy.pem}"
DIR="${POLY_DIR:-/root/polycopy-onchain}"
CONFIG="${POLY_CONFIG:-/root/polycopy/configs/paper_f705.toml}"
DB="${POLY_DB:-/root/polycopy/data/paper_f705.db}"
PYTHON="${POLY_PYTHON:-/root/polycopy/.venv/bin/python}"
LIVE="${POLY_LIVE:-0}"
if [ "$LIVE" = "1" ]; then
  DB_ARGS="--mode live"
  LOG_ARGS=""
  [ -n "${POLY_DB:-}" ] && DB_ARGS="$DB_ARGS --db $POLY_DB"
  [ -n "${POLY_LOG:-}" ] && LOG_ARGS="--log $POLY_LOG"
else
  DB_ARGS="--db $DB"
  LOG_ARGS="--log /root/polycopy/data/paper_f705.log"
fi
SSH=(ssh -i "$KEY" -o ConnectTimeout=20 "$HOST")
SSH_N=(ssh -n -i "$KEY" -o ConnectTimeout=20 "$HOST")

cmd="${1:-status}"
case "$cmd" in
  deploy)
    tar czf - --exclude='__pycache__' --exclude='.pytest_cache' \
      polycopy configs requirements.txt requirements-live.txt tests docs \
      scripts/compare_sources.py scripts/exposure_monitor.py \
      scripts/bucket_report.py \
      | "${SSH[@]}" "mkdir -p $DIR && tar xzf - -C $DIR"
    echo "deployed to $HOST:$DIR"
    ;;
  start)
    "${SSH_N[@]}" "cd $DIR; \
      if [ -f /root/polycopy/.env.live ]; then set -a; . /root/polycopy/.env.live; set +a; fi; \
      setsid nohup $PYTHON -m polycopy onchain run \
      --config $CONFIG $DB_ARGS $LOG_ARGS \
      --trace $DIR/data/onchain_trace.jsonl --lookback 0 \
      >> data/onchain.out 2>&1 < /dev/null & \
      echo \$! > data/onchain.pid; sleep 1; echo started pid \$(cat data/onchain.pid)"
    ;;
  stop)
    "${SSH[@]}" "cd $DIR && if [ -f data/onchain.pid ]; then \
      kill \$(cat data/onchain.pid) 2>/dev/null || true; \
      rm -f data/onchain.pid; echo stopped; else echo 'no pid file'; fi"
    ;;
  restart)
    "$0" stop
    sleep 1
    "$0" start
    ;;
  status)
    "${SSH[@]}" "cd $DIR && if [ -f data/onchain.pid ] && \
      kill -0 \$(cat data/onchain.pid) 2>/dev/null; then \
      echo 'running pid '\$(cat data/onchain.pid); else echo 'not running'; fi; \
      tail -n 8 /root/polycopy/data/paper_f705.log 2>/dev/null || true"
    ;;
  report)
    "${SSH[@]}" "cd $DIR && $PYTHON -m polycopy report --config $CONFIG --db $DB"
    ;;
  compare)
    echo "compare is retired: the data-api paper bot was removed; use bucket instead" >&2
    exit 2
    ;;
  verify)
    shift || true
    "${SSH[@]}" "cd $DIR && $PYTHON -m polycopy onchain verify --config $CONFIG \
      --count 50 --json-out data/verify_f705_50.json ${*:-}"
    ;;
  log)
    "${SSH[@]}" "cd $DIR && tail -n 120 /root/polycopy/data/paper_f705.log"
    ;;
  monitor-start)
    "${SSH_N[@]}" "cd $DIR; if [ -f data/exposure.pid ] && \
      kill -0 \$(cat data/exposure.pid) 2>/dev/null; then \
      echo 'monitor already running pid '\$(cat data/exposure.pid); exit 0; fi; \
      setsid nohup $PYTHON scripts/exposure_monitor.py \
      --db onchain=$DB \
      --out $DIR/data/exposure.jsonl --interval 60 \
      >> data/exposure.out 2>&1 < /dev/null & \
      echo \$! > data/exposure.pid; sleep 1; \
      echo started monitor pid \$(cat data/exposure.pid)"
    ;;
  monitor-stop)
    "${SSH[@]}" "cd $DIR && if [ -f data/exposure.pid ]; then \
      kill \$(cat data/exposure.pid) 2>/dev/null || true; \
      rm -f data/exposure.pid; echo monitor stopped; \
      else echo 'no monitor pid file'; fi"
    ;;
  monitor-status)
    "${SSH[@]}" "cd $DIR && if [ -f data/exposure.pid ] && \
      kill -0 \$(cat data/exposure.pid) 2>/dev/null; then \
      echo 'monitor running pid '\$(cat data/exposure.pid); else \
      echo 'monitor not running'; fi; \
      wc -l data/exposure.jsonl 2>/dev/null || true; \
      tail -n 4 data/exposure.out 2>/dev/null || true"
    ;;
  bucket)
    "${SSH[@]}" "cd $DIR && $PYTHON scripts/bucket_report.py \
      --db onchain=$DB \
      --exposure $DIR/data/exposure.jsonl \
      --json-out data/bucket_report.json"
    ;;
  legs-start|legs-stop|legs-status|legs-report)
    echo "multi-leg retired; only the main on-chain leg remains" >&2
    exit 2
    ;;
  *)
    echo "usage: $0 {deploy|start|stop|restart|status|report|verify|log|monitor-start|monitor-stop|monitor-status|bucket}" >&2
    exit 2
    ;;
esac
