#!/usr/bin/env bash
# Supervise a multi-week paper run.
#
# Two things make a 4-week run different from a 4-hour one, and both are handled
# here rather than in the Python:
#
#   1. It WILL be interrupted — reboots, dropped links, an OOM. So the loop is
#      restarted on any non-zero exit, and the run's end is stored as an absolute
#      instant the first time this script runs. A duration would restart its clock
#      on every restart and the run would never end.
#   2. Its output is the measurement, not the fills. The journal and paper.json
#      live in the state directory; keep that directory and the run survives.
#
# Restarting is safe: the trader records last_bar_ms, so a bar already processed
# is a no-op, and the freshness guard refuses a bar older than one interval.
#
#   ./scripts/run_paper.sh              start (or resume) a 28-day run
#   DAYS=7 ./scripts/run_paper.sh       a shorter one
#   tail -f live_state/paper.log        watch it
#   python3 src/live/run.py --mode paper --report    measurements so far
set -uo pipefail

cd "$(dirname "$0")/.."
STATE_DIR="${STATE_DIR:-live_state}"
DAYS="${DAYS:-28}"
mkdir -p "$STATE_DIR"

DEADLINE_FILE="$STATE_DIR/paper_until.txt"
LOG="$STATE_DIR/paper.log"

if [ ! -s "$DEADLINE_FILE" ]; then
    python3 -c "
import datetime as dt
end = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=float('$DAYS'))
print(end.strftime('%Y-%m-%dT%H:%M:%SZ'))
" > "$DEADLINE_FILE"
    echo "new run, ending $(cat "$DEADLINE_FILE")"
else
    echo "resuming run that ends $(cat "$DEADLINE_FILE")"
fi
UNTIL="$(cat "$DEADLINE_FILE")"

while :; do
    python3 src/live/run.py --mode paper --loop --state-dir "$STATE_DIR" \
        --until "$UNTIL" 2>&1 | tee -a "$LOG"
    code=${PIPESTATUS[0]}
    [ "$code" -eq 0 ] && break            # clean exit means the deadline was reached
    echo "$(date -u +%FT%TZ) exited $code, restarting in 30s" | tee -a "$LOG"
    sleep 30
done

echo "run finished. measurements:" | tee -a "$LOG"
python3 src/live/run.py --mode paper --state-dir "$STATE_DIR" --report | tee -a "$LOG"
