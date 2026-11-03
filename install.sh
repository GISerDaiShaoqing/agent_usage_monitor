#!/usr/bin/env bash
# daipulse_sqdai - environment check/installer for Linux/macOS (no sudo)
# Verifies python3 + tkinter, installs dependencies; prints distro commands
# when something is missing. Launch the widget afterwards with run.sh.
set -u
cd "$(dirname "$0")"

PY="${PYTHON_PATH:-}"
if [ -z "$PY" ]; then
    # 与 run.bat 同策略：跳过 Microsoft Store 占位符（WindowsApps 路径）
    for cand in python3 python python3.exe python.exe; do
        cand_p="$(command -v "$cand" 2>/dev/null || true)"
        case "$cand_p" in *WindowsApps*) continue ;; esac
        if [ -n "$cand_p" ]; then
            PY="$cand_p"
            break
        fi
    done
fi

if [ -z "$PY" ]; then
    echo "[ERROR] python3 not found. Install with one of:"
    echo "  Debian/Ubuntu : sudo apt install python3 python3-venv python3-pip python3-tk"
    echo "  Fedora        : sudo dnf install python3 python3-pip python3-tkinter"
    echo "  macOS         : brew install python@3.12"
    echo "  conda         : conda create -n dai python=3.12 tk && conda activate dai"
    case "$(uname -s)" in MINGW*|MSYS*|CYGWIN*)
        echo "  Windows       : double-click install.bat (winget installs Python); stay out of install.sh in Git Bash" ;;
    esac
    exit 1
fi
echo "[INFO] Using Python: $PY ($("$PY" --version 2>&1))"

# tkinter is required by the widget (Linux: python3-tk / python3-tkinter)
if ! "$PY" -c "import tkinter" 2>/dev/null; then
    echo "[ERROR] tkinter not available. Install with one of:"
    echo "  Debian/Ubuntu : sudo apt install python3-tk"
    echo "  Fedora        : sudo dnf install python3-tkinter"
    echo "  conda         : conda install tk"
    exit 1
fi

out=$("$PY" -m pip install -r requirements.txt 2>&1)
status=$?
if [ $status -ne 0 ]; then
    echo "$out" | tail -n 5
    if echo "$out" | grep -qi "externally-managed"; then
        echo "[HINT] PEP 668 restriction. Use a venv instead:"
        echo "  python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
    fi
    exit $status
fi

if ! "$PY" -c "import textual, httpx, PIL" 2>/dev/null; then
    echo "[ERROR] Dependency import check failed."
    exit 1
fi

echo "[OK] Environment ready. Launch the widget with run.sh."
