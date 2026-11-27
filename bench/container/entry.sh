#!/bin/sh
# E4 run entry point inside the task container (M5 Task 6; ruling M5-N1).
#
#   entry.sh <claude argv...>
#
# 1. Builds a container-local HOME from the arm's template and the stream's carried state
#    (copied, never linked; links dropped), so ownership checks see a local, root-owned tree.
# 2. Runs the agent in the task repository under coreutils `timeout`: the agent timeout is
#    enforced HERE, inside the container.
# 3. Then ALWAYS (timed out or not) removes the OAuth token from its environment, stops every
#    leftover process, extracts the graded patch, copies the state paths and transcripts to
#    $TSB_OUT/state-out/ and finally writes $TSB_OUT/exit.json {agent_exit, timed_out,
#    patch_exit}. exit.json is written last: without it the host treats the run as unfinished.
#
# Inputs come from the environment (passed by name by the host): TSB_TIMEOUT_S,
# TSB_BASE_COMMIT, TSB_PATCH_EXCLUDES and TSB_STATE_PATHS (newline-separated, relative to
# HOME). The TSB_* directory variables exist for the host-side tests only; in a container the
# defaults below are the mounts made by bench/backends.py.
set -u

OUT=${TSB_OUT:-/run-out}
BENCH_HOME=${TSB_HOME_DIR:-/home/bench}
TEMPLATE=${TSB_TEMPLATE:-/opt/template}
STATE_IN=${TSB_STATE_IN:-/state-in}
TESTBED=${TSB_TESTBED:-/testbed}
BIN=${TSB_BIN:-/opt/bench/bin}
PY=${TSB_PYTHON:-/opt/bench/bin/python3.12}
PATCH_PY=${TSB_PATCH_PY:-/opt/tsb/patch.py}
LOG="$OUT/entry.log"

umask 077
mkdir -p "$BENCH_HOME"
HOME=$(cd "$BENCH_HOME" && pwd -P) || exit 3
CLAUDE_CONFIG_DIR=$HOME/.claude
TOKEN_SPAREN_HOME=$HOME/.token-sparen
PATH=$BIN:$PATH
export HOME CLAUDE_CONFIG_DIR TOKEN_SPAREN_HOME PATH

mkdir -p "$CLAUDE_CONFIG_DIR"
for src in "$TEMPLATE" "$STATE_IN"; do
    if [ -d "$src" ]; then
        cp -R "$src/." "$HOME/" 2>>"$LOG"
    fi
done
find "$HOME" -type l -exec rm -f {} + 2>>"$LOG"
if [ "$(id -u)" -eq 0 ]; then
    chown -R 0:0 "$HOME" 2>>"$LOG"
fi
if [ ! -f "$HOME/mcp.json" ]; then
    printf '{"mcpServers": {}}\n' >"$HOME/mcp.json"
fi

start=$(date +%s)
(
    cd "$TESTBED" &&
        timeout --signal=TERM --kill-after=30 "$TSB_TIMEOUT_S" "$@" \
            >"$OUT/stream.jsonl" 2>"$OUT/stderr.txt" </dev/null
)
agent_exit=$?
elapsed=$(($(date +%s) - start))

# --- always runs from here on -----------------------------------------------------------
unset CLAUDE_CODE_OAUTH_TOKEN
if [ "$$" -eq 1 ]; then
    # container init: stop whatever the agent left running (tool processes, MCP servers)
    kill -KILL -1 2>/dev/null
    sleep 1
fi
timed_out=false
if [ "$agent_exit" -eq 124 ] || { [ "$agent_exit" -eq 137 ] && [ "$elapsed" -ge "$TSB_TIMEOUT_S" ]; }; then
    timed_out=true
fi

"$PY" -I "$PATCH_PY" --repo "$TESTBED" --base "$TSB_BASE_COMMIT" \
    --excludes-env TSB_PATCH_EXCLUDES --out "$OUT/patch.diff" 2>>"$LOG"
patch_exit=$?

STATE_OUT="$OUT/state-out"
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

printf '{"agent_exit": %d, "timed_out": %s, "patch_exit": %d}\n' \
    "$agent_exit" "$timed_out" "$patch_exit" >"$OUT/exit.json.tmp"
mv "$OUT/exit.json.tmp" "$OUT/exit.json"
exit 0
