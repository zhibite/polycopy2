#!/usr/bin/env bash
# Control the paper-trading bot on the VPS.
# Usage: scripts/paper_ctl.sh {deploy|init|start|stop|status|report|log|once}
set -euo pipefail

HOST="${POLY_HOST:?set POLY_HOST to user@your-vps}"
KEY="${POLY_KEY:-$HOME/.ssh/polycopy.pem}"
DIR="${POLY_DIR:-/root/polycopy}"
CONFIG="${POLY_CONFIG:-configs/paper_f705.toml}"
SSH=(ssh -i "$KEY" -o ConnectTimeout=20 "$HOST")
SSH_N=(ssh -n -i "$KEY" -o ConnectTimeout=20 "$HOST")

cmd="${1:-status}"
case "$cmd" in
  deploy)
    "${SSH[@]}" "mkdir -p $DIR/configs $DIR/data"
    scp -i "$KEY" -r polycopy configs scripts "${HOST}:$DIR/"
    echo "deployed to $HOST:$DIR"
    ;;
  init)
    "${SSH[@]}" "cd $DIR && python3 -m polycopy init --config $CONFIG"
    ;;
  start)
    "${SSH_N[@]}" "cd $DIR && if [ -f data/paper.pid ] && kill -0 \$(cat data/paper.pid) 2>/dev/null; then echo 'already running pid '\$(cat data/paper.pid); exit 0; fi; setsid nohup python3 -m polycopy run --config $CONFIG >> data/paper.out 2>&1 < /dev/null & echo \$! > data/paper.pid; echo started pid \$(cat data/paper.pid)"
    ;;
  stop)
    "${SSH[@]}" "cd $DIR && if [ -f data/paper.pid ]; then kill \$(cat data/paper.pid) 2>/dev/null || true; rm -f data/paper.pid; echo stopped; else echo 'no pid file'; fi"
    ;;
  status)
    "${SSH[@]}" "cd $DIR && if [ -f data/paper.pid ] && kill -0 \$(cat data/paper.pid) 2>/dev/null; then echo 'running pid '\$(cat data/paper.pid); else echo 'not running'; fi; tail -n 5 data/paper_f705.log 2>/dev/null || true"
    ;;
  once)
    "${SSH[@]}" "cd $DIR && python3 -m polycopy run --config $CONFIG --once"
    ;;
  report)
    "${SSH[@]}" "cd $DIR && python3 -m polycopy report --config $CONFIG"
    ;;
  log)
    "${SSH[@]}" "cd $DIR && tail -n 100 data/paper_f705.log"
    ;;
  *)
    echo "usage: $0 {deploy|init|start|stop|status|report|log|once}" >&2
    exit 2
    ;;
esac
