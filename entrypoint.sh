#!/bin/sh
# Fly volume `/data`는 첫 mount 시 root:root → bot user에게 쓰기 권한 위임 후 gosu로 강등 실행.
# attempt_counts.json 쓰기에 필요 (deepfake_bot.save_attempt).
set -e

if [ -d /data ]; then
    chown -R bot:bot /data 2>/dev/null || true
fi

exec gosu bot "$@"
