#!/usr/bin/env bash
# fake_agent.sh — a configurable fake CLI coding agent for roundtable tests.
#
# Behaviour is driven by env vars:
#   FAKE_AGENT_NAME   name used in output/session ids       (default: "agent")
#   FAKE_AGENT_LOG    file to append every invocation to    (optional)
#   FAKE_COUNTER_FILE file holding the session counter      (optional)
#   FAKE_WRITE_MODE   "file"  -> write the answer MD to the ANSWER FILE path
#                               extracted from the prompt
#                     "stdout"-> print the answer MD to stdout (default)
#   FAKE_SLEEP        seconds to sleep before responding    (default: 0)
#   FAKE_FAIL         if set, exit 1 after printing output
#   FAKE_NO_SESSION   if set, do not print a session id line
#   FAKE_COMMIT_FILE  if set, create this file (relative to the cwd) with a
#                     token and git-commit everything — simulates an agent
#                     making changes during a write phase
#
# Every invocation prints (to stdout):
#   session id: fake-<name>-<counter>     <- matches session_regex 'session id:\s*(\S+)'
# plus a markdown answer containing a unique token FAKE-TOKEN-<name>-<counter>.
set -u

NAME="${FAKE_AGENT_NAME:-agent}"
LOG="${FAKE_AGENT_LOG:-}"
WRITE_MODE="${FAKE_WRITE_MODE:-stdout}"

# Optional leading "--name <n>" argument overrides FAKE_AGENT_NAME.
if [ "${1:-}" = "--name" ]; then
    NAME="$2"
    shift 2
fi

# Counter: exact file via FAKE_COUNTER_FILE, or per-name file inside
# FAKE_COUNTER_DIR.
COUNTER_FILE="${FAKE_COUNTER_FILE:-}"
if [ -z "$COUNTER_FILE" ] && [ -n "${FAKE_COUNTER_DIR:-}" ]; then
    mkdir -p "$FAKE_COUNTER_DIR"
    COUNTER_FILE="$FAKE_COUNTER_DIR/counter-$NAME"
fi

# --- session counter ---------------------------------------------------------
if [ -n "$COUNTER_FILE" ]; then
    if [ -f "$COUNTER_FILE" ]; then
        read -r N < "$COUNTER_FILE" || N=0
    else
        N=0
    fi
    N=$((N + 1))
    printf '%s\n' "$N" > "$COUNTER_FILE"
else
    N=1
fi

# --- prompt: from args, or stdin if no args ----------------------------------
if [ "$#" -gt 0 ]; then
    PROMPT="$*"
else
    PROMPT="$(cat)"
fi

# --- log the invocation for test assertions ----------------------------------
# mkdir is atomic on POSIX and works where flock is unavailable (macOS); the
# spinlock keeps parallel agents' log blocks contiguous.
if [ -n "$LOG" ]; then
    LOCKDIR="${LOG}.lockdir"
    while ! mkdir "$LOCKDIR" 2>/dev/null; do sleep 0.01 2>/dev/null || :; done
    printf '=== invocation: %s #%s ===\nargv: %s\nprompt-begin\n%s\nprompt-end\n' \
        "$NAME" "$N" "$*" "$PROMPT" >> "$LOG"
    rmdir "$LOCKDIR"
fi

# --- simulated work -----------------------------------------------------------
if [ "${FAKE_SLEEP:-0}" != "0" ]; then
    sleep "${FAKE_SLEEP}"
fi

if [ -n "${FAKE_COMMIT_FILE:-}" ]; then
    printf 'RELAY-WRITE-TOKEN-%s-%s\n' "$NAME" "$N" > "$FAKE_COMMIT_FILE"
    git add -A >/dev/null 2>&1 \
        && git -c user.name="Fake Agent" -c user.email="fake@example.com" \
               commit -q -m "fake write phase by ${NAME}" >/dev/null 2>&1 \
        || true
fi

if [ -z "${FAKE_NO_SESSION:-}" ]; then
    echo "session id: fake-${NAME}-${N}"
fi

ANSWER="# Fake ${NAME} answer (invocation ${N})

Token: FAKE-TOKEN-${NAME}-${N}

This is the fake agent ${NAME}'s comprehensive markdown response for this round.
It references prompt bytes: $(printf '%s' "$PROMPT" | wc -c | tr -d ' ') chars received.
"

if [ "$WRITE_MODE" = "file" ]; then
    # Extract "ANSWER FILE: <path>" from the prompt and write there.
    ANSWER_PATH="$(printf '%s\n' "$PROMPT" | sed -n 's/^ANSWER FILE: //p' | head -n 1)"
    if [ -n "$ANSWER_PATH" ]; then
        mkdir -p "$(dirname "$ANSWER_PATH")"
        printf '%s\n' "$ANSWER" > "$ANSWER_PATH"
    else
        printf '%s\n' "$ANSWER"
    fi
else
    printf '%s\n' "$ANSWER"
fi

# FAKE_FAIL: "1" fails every invocation; any other value fails only when it
# equals this invocation's agent name.
if [ -n "${FAKE_FAIL:-}" ] && { [ "$FAKE_FAIL" = "1" ] || [ "$FAKE_FAIL" = "$NAME" ]; }; then
    echo "fake agent ${NAME} failing as requested" >&2
    exit 1
fi
exit 0
