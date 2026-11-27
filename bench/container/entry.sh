#!/bin/sh
# E4 run entry point inside the task container (M5 Task 6; ruling M5-N1; fix round 1).
#
#   entry.sh <claude argv...>
#
# 1. Builds a container-local HOME from the arm's template and the stream's carried state
#    (copied, never linked; links dropped), so ownership checks see a local, root-owned tree.
#    A failed copy aborts BEFORE the agent runs, with no exit.json (the host: infra_fail).
# 2. Runs the agent in the task repository under coreutils `timeout`: the agent timeout is
#    enforced HERE, inside the container. The OAuth token is read from a read-only file mount
#    into a shell variable and set only in the agent command's own environment: it is never
#    in this script's (PID 1's) environment nor in the container config. Processes of the
#    same uid can still read the agent's /proc/<pid>/environ or the mounted file: the subprocess
#    scrub is not a barrier against them; the dedicated, revocable token is the mitigation.
# 3. Then ALWAYS (timed out or not) stops every leftover process, extracts the graded patch
#    under its own time limit, copies the state paths and transcripts to $TSB_OUT/state-out/
#    (replacing whatever the agent put there) and finally writes $TSB_OUT/exit.json
#    {agent_exit, timed_out, patch_exit}. exit.json is written last: without it the host
#    treats the run as unfinished.
#
# Inputs come from the environment (passed by name by the host): TSB_TIMEOUT_S,
# TSB_PATCH_TIMEOUT_S, TSB_BASE_COMMIT, TSB_PATCH_EXCLUDES and TSB_STATE_PATHS
# (newline-separated, relative to HOME). The other TSB_* variables exist for the host-side
# tests only; in a container the defaults below are the mounts made by bench/backends.py.
set -u

OUT=${TSB_OUT:-/run-out}
BENCH_HOME=${TSB_HOME_DIR:-/home/bench}
TEMPLATE=${TSB_TEMPLATE:-/opt/template}
STATE_IN=${TSB_STATE_IN:-/state-in}
TESTBED=${TSB_TESTBED:-/testbed}
BIN=${TSB_BIN:-/opt/bench/bin}
PY=${TSB_PYTHON:-/opt/bench/bin/python3.12}
PATCH_PY=${TSB_PATCH_PY:-/opt/tsb/patch.py}
TOKEN_FILE=${TSB_TOKEN_FILE:-/run/tsb-secret/token}
PATCH_TIMEOUT=${TSB_PATCH_TIMEOUT_S:-60}
LOG="$OUT/entry.log"

fail() {
    printf 'entry: %s\n' "$1" >>"$LOG"
    exit 4
}

unset CLAUDE_CODE_OAUTH_TOKEN
umask 077
mkdir -p "$BENCH_HOME" || fail "cannot create HOME"
HOME=$(cd "$BENCH_HOME" && pwd -P) || fail "cannot resolve HOME"
CLAUDE_CONFIG_DIR=$HOME/.claude
TOKEN_SPAREN_HOME=$HOME/.token-sparen
PATH=$BIN:$PATH
export HOME CLAUDE_CONFIG_DIR TOKEN_SPAREN_HOME PATH

for src in "$TEMPLATE" "$STATE_IN"; do
    if [ -d "$src" ]; then
        cp -R "$src/." "$HOME/" 2>>"$LOG" || fail "copy-in from $src failed"
    fi
done
find "$HOME" -type l -exec rm -f {} + 2>>"$LOG" || fail "link purge failed"
mkdir -p "$CLAUDE_CONFIG_DIR" || fail "cannot create the config dir"
if [ "$(id -u)" -eq 0 ]; then
    chown -R 0:0 "$HOME" 2>>"$LOG" || fail "chown of HOME failed"
fi
if [ ! -f "$HOME/mcp.json" ]; then
    printf '{"mcpServers": {}}\n' >"$HOME/mcp.json" || fail "cannot write mcp.json"
fi
tok=
if [ -f "$TOKEN_FILE" ]; then
    tok=$(cat "$TOKEN_FILE") || fail "token file unreadable"
fi

start=$(date +%s)
(
    cd "$TESTBED" || exit 125
    if [ -n "$tok" ]; then
        CLAUDE_CODE_OAUTH_TOKEN=$tok timeout --signal=TERM --kill-after=30 "$TSB_TIMEOUT_S" "$@" \
            >"$OUT/stream.jsonl" 2>"$OUT/stderr.txt" </dev/null
    else
        timeout --signal=TERM --kill-after=30 "$TSB_TIMEOUT_S" "$@" \
            >"$OUT/stream.jsonl" 2>"$OUT/stderr.txt" </dev/null
    fi
)
agent_exit=$?
tok=
unset tok
elapsed=$(($(date +%s) - start))

# --- always runs from here on -----------------------------------------------------------
if [ "$$" -eq 1 ]; then
    # container init: stop whatever the agent left running (tool processes, MCP servers)
    kill -KILL -1 2>/dev/null
    sleep 1
fi
timed_out=false
if [ "$agent_exit" -eq 124 ] || { [ "$agent_exit" -eq 137 ] && [ "$elapsed" -ge "$TSB_TIMEOUT_S" ]; }; then
    timed_out=true
fi

timeout --signal=TERM --kill-after=5 "$PATCH_TIMEOUT" "$PY" -I "$PATCH_PY" --repo "$TESTBED" \
    --base "$TSB_BASE_COMMIT" --excludes-env TSB_PATCH_EXCLUDES --out "$OUT/patch.diff" 2>>"$LOG"
patch_exit=$?

STATE_OUT="$OUT/state-out"
rm -rf "$STATE_OUT"
mkdir -p "$STATE_OUT"
printf '%s\n' "${TSB_STATE_PATHS:-}" | while IFS= read -r rel; do
    case "$rel" in
        "" | /* | ..* | */..* | *"/../"*) continue ;;
    esac
    src="$HOME/$rel"
    [ -e "$src" ] || continue
    # no link anywhere between HOME and the state path (it could point anywhere)
    [ "$(readlink -f "$src")" = "$HOME/$rel" ] || continue
    mkdir -p "$STATE_OUT/$(dirname "$rel")"
    cp -R "$src" "$STATE_OUT/$rel" 2>>"$LOG"
done
find "$STATE_OUT" -type l -exec rm -f {} + 2>>"$LOG"

rm -f "$OUT/exit.json.tmp"
printf '{"agent_exit": %d, "timed_out": %s, "patch_exit": %d}\n' \
    "$agent_exit" "$timed_out" "$patch_exit" >"$OUT/exit.json.tmp"
mv -f "$OUT/exit.json.tmp" "$OUT/exit.json"
exit 0
