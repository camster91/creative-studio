#!/usr/bin/env bash
# Creative Studio — Iterative Image Workflow
# Usage:
#   bash launch.sh direct --prompt "..." --input-image product.png
#   bash launch.sh chat --name "gfuel-shelf" --input-image product.png
#
# The user IS the creative director. This tool just executes prompts reliably.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_SCRIPT="$SCRIPT_DIR/scripts/creative_studio.py"

# Auto-detect Windows Downloads
if [ -d "/mnt/c" ]; then
    WIN_HOME="$(wslpath "$(cmd.exe /c 'echo %USERPROFILE%' 2>/dev/null | tr -d '\r\n')" 2>/dev/null)"
    if [ -n "$WIN_HOME" ]; then
        export CREATIVE_OUTPUT_DIR="$WIN_HOME/Downloads/creative-studio-outputs"
    fi
fi

export GEMINI_API_KEY="${GEMINI_API_KEY:-}"
export FIGMA_ACCESS_TOKEN="${FIGMA_ACCESS_TOKEN:-}"

# Run with uv, passing all args
exec uv run "$PYTHON_SCRIPT" "$@"
