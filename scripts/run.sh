#!/bin/bash
# One benchmark run on this server, started by cron (see schedule.txt).
#   run.sh <server> <full|light>
# full:  speed (cold c=1, warm c=1, load c=5 and c=10), fingerprint, targets
# light: speed (cold c=1, warm c=1), fingerprint, targets
#
# Servers take turns (schedule.txt) and never run at the same time: Floppydata's
# traffic is read from one account balance, GoLogin's real tests share one
# profile, and the providers' parallel-session limits are per account.
set -u
cd ~/cbbench
S=$1
MODE=$2
RUN=$(date -u +%Y%m%d-%H%M)
ALL=floppydata,gologin,steel,browserbase,kernel,anchor,hyperbrowser,browseruse,notte,browserless,lightpanda
NOGL=floppydata,steel,browserbase,kernel,anchor,hyperbrowser,browseruse,notte,browserless,lightpanda
WARM=steel,gologin,browserbase,kernel,hyperbrowser,browseruse
# plans that allow 10 sessions at once; Kernel and Anchor stop at 5
C10=floppydata,gologin,steel,browserbase,hyperbrowser,browseruse,notte,browserless,lightpanda
mkdir -p logs
exec >>"logs/run_${S}_${RUN}.log" 2>&1

step() {  # step <max minutes> <cbbench args...>
    local limit=$1
    shift
    echo "-- $(date -u '+%F %T') $*"
    timeout "${limit}m" .venv/bin/python -u cbbench.py "$@" --server "$S" || echo "-- step exited with $?"
}

echo "== run $RUN on $S, $MODE"
# GoLogin makes a new profile for every cold session (daily cap), so it gets
# fewer cold sessions than the others
# step limits add up to 3h40m at most, inside the 4-hour turn of each server
step 35 lifecycle --providers "$NOGL" --n 30 --run-id "$RUN-c1"
step 10 lifecycle --providers gologin --n 10 --run-id "$RUN-c1gl"
step 25 lifecycle --providers "$WARM" --start warm --n 20 --run-id "$RUN-warm"
if [ "$MODE" = full ]; then
    step 15 lifecycle --providers "$ALL" --n 10 --concurrency 5 --warmup 0 --run-id "$RUN-c5"
    step 15 lifecycle --providers "$C10" --n 20 --concurrency 10 --warmup 0 --run-id "$RUN-c10"
fi
step 25 fingerprint --providers "$ALL" --n 5 --run-id "$RUN"
step 80 targets --run-id "$RUN"
echo "== run $RUN done $(date -u '+%F %T')"
