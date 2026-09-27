#!/bin/bash
# Find a Python 3.14 install and make the `python` and `pip` commands in
# Terminal use it. Double-click in Finder, or run ./link_python.command.
# Safe to run again: it replaces its own links and its own block, nothing else.
#
# What it does:
#   1. Finds python3.14: Homebrew (Apple Silicon or Intel), the python.org
#      installer, pyenv, or anything on PATH.
#   2. ~/.local/bin/python -> that interpreter.
#      ~/.local/bin/pip    -> small wrapper running `python -m pip`, so pip
#      always belongs to the same Python.
#   3. Adds a marked block at the END of the shell startup file (~/.zshrc for
#      zsh, ~/.bash_profile for bash). The block puts ~/.local/bin first on
#      PATH and removes older `python` / `pip` aliases, which would otherwise
#      win over the links. A backup of the startup file is made first.

set -u

WANTED="3.14"
BIN_DIR="$HOME/.local/bin"
MARK_BEGIN="# >>> link_python (managed by link_python.command) >>>"
MARK_END="# <<< link_python <<<"
STAMP="$(date +%Y%m%d-%H%M%S)"

die() {
  echo ""
  echo "!!! $1"
  exit 1
}

# Prints "3.14" style major.minor for the interpreter in $1, or nothing.
version_of() {
  "$1" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null
}

# First candidate that exists and really is Python $WANTED. Stable paths
# (Homebrew "opt", framework "Versions/3.14") are listed before resolved ones
# so the link survives patch upgrades such as 3.14.6 -> 3.14.7.
find_python() {
  local candidates=(
    "/opt/homebrew/opt/python@$WANTED/bin/python$WANTED"
    "/usr/local/opt/python@$WANTED/bin/python$WANTED"
    "/Library/Frameworks/Python.framework/Versions/$WANTED/bin/python$WANTED"
  )
  local pyenv_dir
  for pyenv_dir in $(ls -d "$HOME/.pyenv/versions/$WANTED".* 2>/dev/null | sort -rV); do
    candidates+=("$pyenv_dir/bin/python$WANTED")
  done
  local on_path
  for on_path in "python$WANTED" python3; do
    if command -v "$on_path" >/dev/null 2>&1; then
      candidates+=("$(command -v "$on_path")")
    fi
  done
  local candidate
  for candidate in "${candidates[@]}"; do
    if [ -x "$candidate" ] && [ "$(version_of "$candidate")" = "$WANTED" ]; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

# Moves $1 aside if it exists and was not written by this script.
set_aside_foreign() {
  local path="$1"
  if [ -e "$path" ] || [ -L "$path" ]; then
    if [ -L "$path" ] || grep -q "managed by link_python.command" "$path" 2>/dev/null; then
      rm -f "$path"
    else
      mv "$path" "$path.bak-$STAMP"
      echo "Moved existing $path to $path.bak-$STAMP"
    fi
  fi
}

startup_file_for_shell() {
  case "$(basename "$1")" in
    zsh) echo "$HOME/.zshrc" ;;
    bash) echo "$HOME/.bash_profile" ;;
    *) echo "$HOME/.profile" ;;
  esac
}

PY="$(find_python)" || die "Python $WANTED not found. Install it first:
  Homebrew:   brew install python@$WANTED
  or python.org: https://www.python.org/downloads/macos/ (Python $WANTED.x installer)
Then run this script again."

echo "Found Python $WANTED: $PY ($("$PY" --version 2>&1))"

if ! "$PY" -m pip --version >/dev/null 2>&1; then
  echo "pip missing for this Python, installing it with ensurepip..."
  "$PY" -m ensurepip --upgrade >/dev/null || die "could not install pip for $PY"
fi

mkdir -p "$BIN_DIR" || die "cannot create $BIN_DIR"

set_aside_foreign "$BIN_DIR/python"
ln -s "$PY" "$BIN_DIR/python" || die "cannot link $BIN_DIR/python"

set_aside_foreign "$BIN_DIR/pip"
cat > "$BIN_DIR/pip" <<EOF
#!/bin/sh
# managed by link_python.command: pip for the same Python as \`python\`.
exec "$PY" -m pip "\$@"
EOF
chmod +x "$BIN_DIR/pip" || die "cannot make $BIN_DIR/pip executable"

RC="$(startup_file_for_shell "$SHELL")"
touch "$RC" || die "cannot write $RC"
cp -p "$RC" "$RC.bak-link_python-$STAMP"
# Drop an older copy of our block, then append a fresh one at the very end so
# it runs after any earlier `alias python=...` lines.
awk -v b="$MARK_BEGIN" -v e="$MARK_END" '
  $0 == b { skip = 1; next }
  $0 == e { skip = 0; next }
  !skip { print }
' "$RC.bak-link_python-$STAMP" > "$RC"
cat >> "$RC" <<EOF
$MARK_BEGIN
unalias python pip 2>/dev/null
export PATH="\$HOME/.local/bin:\$PATH"
$MARK_END
EOF

echo ""
echo "Linked:"
echo "  $BIN_DIR/python -> $PY"
echo "  $BIN_DIR/pip    -> $PY -m pip"
echo "Updated $RC (backup: $RC.bak-link_python-$STAMP)"
echo ""
echo "Check (as a new Terminal window will see it):"
PATH="$BIN_DIR:$PATH" python --version
PATH="$BIN_DIR:$PATH" pip --version
echo ""
echo "Done. Open a NEW Terminal window (or run: source $RC) to use python and pip."
