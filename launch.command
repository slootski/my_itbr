#!/bin/bash
# Double-click launcher for the IBKR Dashboard.
# Kills any previous instance on port 8501, then activates the project venv
# and starts Streamlit (opens the browser).
# Resolve the real script location: this file is launched both directly and via
# a Desktop symlink, so follow the symlink chain to land in the project dir
# (a plain dirname "$0" would be the Desktop, where there is no venv/app.py).
SOURCE="${BASH_SOURCE[0]}"
while [ -h "$SOURCE" ]; do
  DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
  SOURCE="$(readlink "$SOURCE")"
  [ "${SOURCE#/}" = "$SOURCE" ] && SOURCE="$DIR/$SOURCE"
done
PROJECT_DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"

# Never let the Terminal window vanish on an error: without this, a failure
# before Streamlit starts just closes the window and there is nothing to read.
die() {
  echo ""
  echo "!!! Launch failed: $*"
  echo ""
  read -r -p "Press Enter to close..."
  exit 1
}

cd "$PROJECT_DIR" || die "cannot cd to $PROJECT_DIR"

PORT=8501
LOG_DIR="$PROJECT_DIR/logs"
LAUNCH_LOG="$LOG_DIR/launcher.log"
mkdir -p "$LOG_DIR" 2>/dev/null

echo "Project: $PROJECT_DIR"

[ -f "app.py" ] || die "app.py not found in $PROJECT_DIR"

if [ ! -x ".venv/bin/streamlit" ]; then
  echo "venv not found. Run setup first:"
  echo "  python3 -m venv .venv && PIP_USER=0 .venv/bin/pip install -r requirements.txt"
  die "missing .venv/bin/streamlit"
fi

# The venv is tied to the exact interpreter it was built from; if that Python
# was upgraded or removed (e.g. a Homebrew bump), every venv binary dies with a
# bad-interpreter error. Check once, up front, with an actionable message.
.venv/bin/python -c "import sys" >/dev/null 2>&1 \
  || die "the .venv interpreter is broken (was its Python upgraded or removed?). Rebuild with:
  rm -rf .venv && python3 -m venv .venv && PIP_USER=0 .venv/bin/pip install -r requirements.txt"

# Stop a previous instance (anything listening on the Streamlit port).
OLD_PIDS=$(lsof -ti :$PORT -sTCP:LISTEN 2>/dev/null)
if [ -n "$OLD_PIDS" ]; then
  echo "Stopping previous instance (PID: $OLD_PIDS)..."
  kill $OLD_PIDS 2>/dev/null
  # Wait up to 5s for the port to free; force-kill if still alive.
  for _ in 1 2 3 4 5; do
    sleep 1
    [ -z "$(lsof -ti :$PORT -sTCP:LISTEN 2>/dev/null)" ] && break
  done
  REMAINING=$(lsof -ti :$PORT -sTCP:LISTEN 2>/dev/null)
  if [ -n "$REMAINING" ]; then
    echo "Still running, force-killing..."
    kill -9 $REMAINING 2>/dev/null
    sleep 1
  fi
fi

# Also stop stale instances of THIS app that drifted off the port (Streamlit
# auto-increments the port when it's busy, so port-only cleanup misses them).
# Match on command line AND working directory so other projects' Streamlit
# processes are never touched.
for pid in $(pgrep -f "streamlit run app.py" 2>/dev/null); do
  if lsof -p "$pid" -a -d cwd -Fn 2>/dev/null | grep -qx "n$PWD"; then
    echo "Stopping stale instance (PID: $pid)..."
    kill "$pid" 2>/dev/null
  fi
done

# Pin the port explicitly, so Streamlit's busy-port fallback can never move the
# app to a port the user isn't looking at. If the port is still taken here, say
# so instead of starting a second copy somewhere else.
if [ -n "$(lsof -ti :$PORT -sTCP:LISTEN 2>/dev/null)" ]; then
  die "port $PORT is still in use by another process and could not be freed"
fi

echo "Starting Streamlit on http://localhost:$PORT ..."
echo "(log: $LAUNCH_LOG - keep this window open; closing it stops the server)"
echo ""

# Run in the foreground but WITHOUT exec, so a startup crash returns here and
# the window stays open with the error on screen instead of disappearing.
set -o pipefail
.venv/bin/streamlit run app.py \
  --server.port "$PORT" \
  --server.headless false 2>&1 | tee -a "$LAUNCH_LOG"
STATUS=$?

if [ "$STATUS" -ne 0 ]; then
  die "Streamlit exited with status $STATUS (see the output above)"
fi

echo ""
echo "Dashboard stopped."
read -r -p "Press Enter to close..."
