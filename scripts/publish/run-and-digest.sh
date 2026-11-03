#!/bin/bash
# Weekly Sync stats: scrape TikTok metrics into the sofit performance log,
# then push a 3-line WhatsApp digest via headless Claude (WhatsApp MCP).
# Scheduled daily by launchd (vc.groundup.ws-stats); the scraper itself
# decides which posts are due (T+3d), so most runs are no-ops.
#
# SOFIT_PUBLISH_SCRIPTS  directory containing scrape.py (default: this dir)
# SOFIT_PYTHON           interpreter. Unset keeps ~/.ws-scraper/venv/bin/python
#                        when that binary exists (Mac launchd), else python3.
# SOFIT_DIGEST_CHANNEL   whatsapp (default) | none
#                        none still scrapes and appends scrape-history.jsonl,
#                        then prints one JSON summary on stdout and does not send.
#                        A missing claude binary skips the send (stdout note,
#                        exit status still comes from the scrape).
set -uo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
SCRAPE="${SOFIT_PUBLISH_SCRIPTS:-$SCRIPT_DIR}/scrape.py"

# $SOFIT_PYTHON when set. The Mac job does not set it: playwright lives in
# ~/.ws-scraper/venv, which is the interpreter this script used to hardcode.
# A box without that venv gets python3 on PATH.
if [ -n "${SOFIT_PYTHON:-}" ]; then
  PY="$SOFIT_PYTHON"
elif [ -x "$HOME/.ws-scraper/venv/bin/python" ]; then
  PY="$HOME/.ws-scraper/venv/bin/python"
else
  PY="python3"
fi

CHANNEL="${SOFIT_DIGEST_CHANNEL:-whatsapp}"
case "$CHANNEL" in
  whatsapp|none) ;;
  *)
    printf 'SOFIT_DIGEST: unknown SOFIT_DIGEST_CHANNEL=%s (expected whatsapp or none)\n' "$CHANNEL"
    exit 1
    ;;
esac

CLAUDE_BIN=""
if [ "$CHANNEL" = "whatsapp" ]; then
  CLAUDE_BIN=$(command -v claude || true)
  if [ -z "$CLAUDE_BIN" ] && [ -x "$HOME/.local/bin/claude" ]; then
    CLAUDE_BIN="$HOME/.local/bin/claude"
  fi
fi

LOGDIR="$HOME/.ws-scraper/logs"
mkdir -p "$LOGDIR"
STAMP=$(date +%Y%m%d-%H%M%S)

OUT=$("$PY" "$SCRAPE" 2>"$LOGDIR/$STAMP.err")
CODE=$?

# ponytail: every run leaves exactly one stamped jsonl record - success, crash,
# or garbage - so "did it run today?" is answerable from this file alone.
# Returns 3 when stdout was not a JSON object, so callers can alert on it.
record() {
  STAMP="$STAMP" REC="$1" "$PY" -c '
import os, json, sys
try:
    d = json.loads(os.environ["REC"])
    if not isinstance(d, dict): raise ValueError
    bad = False
except Exception:
    d, bad = {"status": "unparseable", "raw": os.environ["REC"][:500]}, True
d["stamp"] = os.environ["STAMP"]
print(json.dumps(d, ensure_ascii=False))
sys.exit(3 if bad else 0)' >> "$LOGDIR/scrape-history.jsonl"
}

# One JSON object on stdout for SOFIT_DIGEST_CHANNEL=none.
# $1 status override (empty = the scrape object's status), $2 exit code, $3 raw JSON.
summarize() {
  SUM_STATUS="$1" SUM_CODE="$2" REC="$3" "$PY" -c '
import os, json
raw = os.environ.get("REC") or ""
try:
    d = json.loads(raw)
    if not isinstance(d, dict):
        raise ValueError
except Exception:
    d = {}
sessions = d.get("sessions") if isinstance(d.get("sessions"), dict) else {}
dead = [name for name, ok in sessions.items() if ok is False]
status = os.environ.get("SUM_STATUS") or d.get("status") or "unknown"
if status == "session_dead" and not dead:
    dead = ["tiktok"]

def as_count(key):
    value = d.get(key, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0

out = {
    "status": status,
    "updated": as_count("updated"),
    "failed": as_count("failed"),
    "dead_sessions": dead,
    "sessions": sessions,
}
code = os.environ.get("SUM_CODE")
if code:
    out["code"] = int(code)
print(json.dumps(out, ensure_ascii=False))
'
}

alert() {
  if [ "$CHANNEL" != "whatsapp" ]; then
    return 0
  fi
  if [ -z "$CLAUDE_BIN" ]; then
    printf 'SOFIT_DIGEST: claude not found; skipping WhatsApp send: %s\n' "$1"
    return 0
  fi
  # Prompt via stdin: --allowedTools is variadic and swallows a trailing
  # positional prompt, leaving claude -p with no input at all.
  printf '%s' "Send a WhatsApp message to my own chat (message myself), exactly this text and nothing else: '$1' Use the whatsapp MCP send_message tool. Never retry a send in parallel. If it is unavailable print ALERT_FAILED and stop - never use another channel (no email, no Slack, nothing)." \
    | "$CLAUDE_BIN" -p --allowedTools "mcp__whatsapp__send_message mcp__whatsapp__search_contacts Read" \
    >> "$LOGDIR/$STAMP.digest.log" 2>&1
}

# $1 summary status, $2 raw JSON, $3 WhatsApp text
emit() {
  if [ "$CHANNEL" = "none" ]; then
    summarize "$1" "$CODE" "$2"
  else
    alert "$3"
  fi
}

# A crash or empty stdout used to append a blank line and parse to updated=0,
# so the scraper could be dead for days without a single alert.
if [ "$CODE" -ne 0 ] && [ "$CODE" -ne 2 ]; then
  rec=$(printf '{"status":"crash","code":%d,"err_log":"%s"}' "$CODE" "$LOGDIR/$STAMP.err")
  record "$rec"
  emit "crash" "$rec" "סקרייפר הסטטיסטיקות של וויקלי סינק קרס (קוד $CODE). הלוג: ~/.ws-scraper/logs/$STAMP.err"
  exit 1
fi

if [ -z "$OUT" ]; then
  rec=$(printf '{"status":"empty","code":%d,"err_log":"%s"}' "$CODE" "$LOGDIR/$STAMP.err")
  record "$rec"
  emit "empty" "$rec" "סקרייפר הסטטיסטיקות של וויקלי סינק סיים בלי פלט. הלוג: ~/.ws-scraper/logs/$STAMP.err"
  exit 1
fi

if ! record "$OUT"; then
  emit "unparseable" "$OUT" "סקרייפר הסטטיסטיקות של וויקלי סינק החזיר פלט לא תקין (לא JSON). הלוג: ~/.ws-scraper/logs/scrape-history.jsonl"
  exit 1
fi

if [ "$CODE" -eq 2 ]; then
  # Dead session: alert instead of silently returning zero rows forever.
  emit "session_dead" "$OUT" "סקרייפר הסטטיסטיקות של וויקלי סינק: הסשן של טיקטוק פג. תריץ: $PY $SCRAPE --headed ותתחבר מחדש."
  exit 0
fi

if [ "$CHANNEL" = "none" ]; then
  summarize "" "$CODE" "$OUT"
  exit 0
fi

UPDATED=$(echo "$OUT" | "$PY" -c "import sys,json;print(json.load(sys.stdin).get('updated',0))" 2>/dev/null || echo 0)
if [ "${UPDATED:-0}" -gt 0 ]; then
  if [ -z "$CLAUDE_BIN" ]; then
    printf 'SOFIT_DIGEST: claude not found; skipping WhatsApp digest\n'
  else
    printf '%s' "Here is today's Weekly Sync clip-metrics scrape result JSON: $OUT
Compose a Hebrew digest of AT MOST 3 lines: the standout TikTok clip (hook + avg watch seconds), the collapsed TikTok clip (hook + avg watch seconds), and ONE pattern sentence comparing hooks (read ~/.sofit/performance.jsonl for full context).
Ranking rules, follow them exactly:
- Use ONLY the JSON's 'best' and 'worst' fields. If 'best' is null, write that no clip stood out this week - do NOT open 'ranked' and pick the top row instead. Same for 'worst'. Null means the clips were within measurement noise, and naming a winner there is a false lesson. A digest that says 'no separation this week' is the correct output, not a failure.
- IGNORE retention% completely when comparing clips: it is avg-watch divided by duration, so it ranks clips by how short they are, not how good they are.
- Never compare a TikTok number against an Instagram one - Instagram counts a view after ~1 second and rounds watch time to whole seconds, so its numbers are not on the same scale.
Then send it as a WhatsApp message to my own chat (message myself) using the whatsapp MCP send_message tool. Never retry a send in parallel. If the WhatsApp tool is unavailable or the send fails, print DIGEST_FAILED and stop - NEVER use any other channel (no email, no Slack, nothing)." \
      | "$CLAUDE_BIN" -p --allowedTools "mcp__whatsapp__send_message mcp__whatsapp__search_contacts Read" \
      >> "$LOGDIR/$STAMP.digest.log" 2>&1
  fi
fi
exit 0
