#!/usr/bin/env bash
# OpenCode Go quota tracker - unified launcher for Linux/macOS
# Starts the floating desktop widget in the background.
# TUI dashboard: right-click widget menu, or run: python -X utf8 -m app.main
set -u
cd "$(dirname "$0")"

# Optional: export PYTHON_PATH to skip auto-detection
PY="${PYTHON_PATH:-}"
if [ -z "$PY" ]; then
    PY="$(command -v python3 || command -v python || true)"
fi
if [ -z "$PY" ]; then
    echo "[ERROR] python3 not found. Install Python 3.10+ first." >&2
    exit 1
fi

# tkinter is required by the widget (Linux: sudo apt install python3-tk)
if ! "$PY" -c "import tkinter" 2>/dev/null; then
    echo "[ERROR] tkinter not available." >&2
    echo "  Debian/Ubuntu: sudo apt install python3-tk" >&2
    echo "  Fedora: sudo dnf install python3-tkinter" >&2
    echo "  conda: conda install tk" >&2
    exit 1
fi

"$PY" -c "import textual, httpx, PIL" 2>/dev/null || {
    echo "Installing dependencies..."
    "$PY" -m pip install -r requirements.txt -q
}

# nohup: detach from terminal so it survives after the terminal closes
nohup "$PY" -X utf8 -m app.widget >/dev/null 2>&1 &
echo "widget launched (pid $!)"
echo "TUI dashboard: right-click widget menu, or: $PY -X utf8 -m app.main"
