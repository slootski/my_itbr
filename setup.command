#!/bin/bash
# One-time setup of the IBKR Dashboard on a new Mac. Double-click it in Finder
# (first time: right-click -> Open, because macOS blocks files from another
# computer), or run: bash setup.command
# Safe to run again; it only redoes what is missing or broken.
#
# Steps:
#   1. Python 3.14: found via link_python.command, which also makes the
#      `python` and `pip` Terminal commands use it. If missing and Homebrew is
#      installed, offers `brew install python@3.14`; otherwise explains how.
#   2. .venv: created (or rebuilt when broken / wrong Python) and filled from
#      requirements.lock, the exact package versions the app is tested with.
#   3. Unblocks the project's files (macOS quarantine flag from AirDrop/download).
#   4. Desktop shortcut "Start IBKR Dashboard.command" -> launch.command.

set -u

WANTED="3.14"
SHORTCUT="$HOME/Desktop/Start IBKR Dashboard.command"

# Project folder = where this file really lives (works via a symlink too).
SOURCE="${BASH_SOURCE[0]}"
while [ -h "$SOURCE" ]; do
  DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
  SOURCE="$(readlink "$SOURCE")"
  [ "${SOURCE#/}" = "$SOURCE" ] && SOURCE="$DIR/$SOURCE"
done
PROJECT_DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"

die() {
  echo ""
  echo "!!! Setup failed: $1"
  echo ""
  read -r -p "Press Enter to close..."
  exit 1
}

step() {
  echo ""
  echo "==> $1"
}

cd "$PROJECT_DIR" || die "cannot open $PROJECT_DIR"
echo "Project: $PROJECT_DIR"

step "1/4 Python $WANTED"
# The copied scripts may carry the quarantine flag; clear it before running them.
xattr -dr com.apple.quarantine "$PROJECT_DIR" 2>/dev/null
if ! bash "$PROJECT_DIR/link_python.command"; then
  if command -v brew >/dev/null 2>&1; then
    read -r -p "Install Python $WANTED with Homebrew now? [y/N] " answer
    case "$answer" in
      y|Y|yes|YES)
        brew install "python@$WANTED" || die "brew install python@$WANTED failed"
        bash "$PROJECT_DIR/link_python.command" || die "Python $WANTED still not found after installing"
        ;;
      *) die "Python $WANTED is required. Install it, then run setup again." ;;
    esac
  else
    open "https://www.python.org/downloads/macos/" 2>/dev/null
    die "Python $WANTED is required. The download page is open in your browser:
  install the latest Python $WANTED.x (macOS 64-bit universal2 installer),
  then run setup again."
  fi
fi
PY="$HOME/.local/bin/python"
[ "$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)" = "$WANTED" ] \
  || die "$PY is not Python $WANTED"

step "2/4 App environment (.venv)"
VENV_OK=0
if [ -x ".venv/bin/python" ] \
  && [ "$(.venv/bin/python -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)" = "$WANTED" ]; then
  VENV_OK=1
fi
if [ "$VENV_OK" -eq 0 ]; then
  [ -e ".venv" ] && echo "Existing .venv is broken or uses another Python; rebuilding it."
  rm -rf .venv
  "$PY" -m venv .venv || die "could not create .venv"
fi
echo "Installing packages from requirements.lock (first time takes a few minutes)..."
PIP_USER=0 .venv/bin/pip install --disable-pip-version-check -q -r requirements.lock \
  || die "package install failed (check the internet connection, then run setup again)"
.venv/bin/python -c "import streamlit, pandas, plotly, ibflex, yfinance, dotenv" \
  || die "packages installed but the app libraries do not import"

step "3/4 Unblock project files"
xattr -dr com.apple.quarantine "$PROJECT_DIR" 2>/dev/null
chmod +x launch.command link_python.command setup.command
echo "Done."

step "4/4 Desktop shortcut"
if [ -e "$SHORTCUT" ] && [ ! -L "$SHORTCUT" ]; then
  echo "Skipped: $SHORTCUT exists and is not a shortcut made by setup."
else
  ln -sfn "$PROJECT_DIR/launch.command" "$SHORTCUT" || die "could not create $SHORTCUT"
  echo "Created: $SHORTCUT"
fi

echo ""
echo "Setup complete."
echo "Start the dashboard: double-click \"Start IBKR Dashboard\" on the Desktop."
echo "Then click \"Connect IBKR account\" and enter your own Flex token and Flex Query ID."
echo ""
read -r -p "Press Enter to close..."
