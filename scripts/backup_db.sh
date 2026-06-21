#!/bin/bash
set -euo pipefail
SRC="/home/ubuntu/trading-bot/db/trades.db"
DST="/home/ubuntu/backups/trades_$(date +%Y%m%d).db"
cp "$SRC" "$DST"
find /home/ubuntu/backups/ -name "trades_*.db" -mtime +30 -delete
