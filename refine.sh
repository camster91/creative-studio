#!/usr/bin/env bash
# Creative Studio — Edit & Refine Workflow
#
# After generating an image, use these commands to polish it.
# The workflow is: GENERATE → REVIEW → REFINE → APPROVE
#
# Quick reference:
#   revise     --input FILE --prompt 'change this'
#   auto-fix   --input FILE --fix 'brighten background'
#   iterate    --input FILE --prompt 'make it warmer'
#   upscale    --input FILE [--resolution 4K]
#   compare    --input FILE [--compare FILE2]
#   approve    --input FILE [--name 'final-name']
#   open       --input FILE   (opens in Windows Photos)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
PYTHON_SCRIPT="$SCRIPT_DIR/scripts/creative_studio.py"

usage() {
    echo "Creative Studio — Edit & Refine"
    echo ""
    echo "  revise     --input FILE --prompt 'change this'"
    echo "  auto-fix   --input FILE --fix 'brighten background'"
    echo "  iterate    --input FILE --prompt 'make it warmer'"
    echo "  upscale    --input FILE [--resolution 4K]"
    echo "  compare    --input FILE [--compare FILE2]"
    echo "  approve    --input FILE [--name 'final-name'"
    echo "  open       --input FILE   (opens in Windows Photos)"
    echo ""
    echo "  Full workflow:"
    echo "    1. generate  --prompt 'product shot on marble' --format facebook-feed"
    echo "    2. open      --input OUTPUT.png"
    echo "    3. revise    --input OUTPUT.png --prompt 'make background darker blue'"
    echo "    4. open      --input OUTPUT-revised.png"
    echo "    5. auto-fix  --input OUTPUT-revised.png --fix 'sharpen text'"
    echo "    6. approve   --input OUTPUT-revised-fixed.png --name 'facebook-ad-v1'"
}

ACTION="${1:-}"
shift 2>/dev/null || true

if [ -z "$ACTION" ]; then
    usage
    exit 0
fi

# ── Parse args ──────────────────────────────────────────────────────

INPUT=""
COMPARE=""
FIX=""
PROMPT=""
RESOLUTION=""
NAME=""
FORMAT="facebook-feed"
OUTPUT_DIR=""

while [ $# -gt 0 ]; do
    case "$1" in
        --input) INPUT="$2"; shift 2 ;;
        --compare) COMPARE="$2"; shift 2 ;;
        --fix) FIX="$2"; shift 2 ;;
        --prompt) PROMPT="$2"; shift 2 ;;
        --resolution) RESOLUTION="$2"; shift 2 ;;
        --name) NAME="$2"; shift 2 ;;
        --format) FORMAT="$2"; shift 2 ;;
        --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
        *) shift ;;
    esac
done

# ── Helpers ──────────────────────────────────────────────────────────

open_in_photos() {
    local file="$1"
    if [ ! -f "$file" ]; then
        echo "✗ File not found: $file"
        return 1
    fi
    echo "Opening in Windows Photos..."
    if [ -d "/mnt/c" ]; then
        local winpath
        winpath=$(wslpath -w "$file" 2>/dev/null)
        if [ -n "$winpath" ]; then
            cmd.exe /c "start \"\" \"$winpath\"" 2>/dev/null || echo "Open manually: $file"
        else
            echo "Open manually: $file"
        fi
    else
        open "$file" 2>/dev/null || echo "Open manually: $file"
    fi
}

# ── Action handlers ──────────────────────────────────────────────────

case "$ACTION" in

revise|iterate)
    if [ -z "$INPUT" ]; then
        echo "✗ --input required"
        exit 1
    fi
    if [ -z "$PROMPT" ]; then
        echo "✗ --prompt required (e.g. 'make background darker blue')"
        exit 1
    fi

    echo ""
    echo "── Revising: $(basename "$INPUT")"
    echo "  Change: $PROMPT"
    echo ""

    uv run "$PYTHON_SCRIPT" revise \
        --input "$INPUT" \
        --prompt "$PROMPT" \
        --format "$FORMAT"

    echo ""
    echo "  Revision saved. To open:"
    echo "    explorer.exe ~/creative-studio-outputs/"
    echo ""
    ;;

auto-fix)
    if [ -z "$INPUT" ]; then
        echo "✗ --input required"
        exit 1
    fi
    if [ -z "$FIX" ]; then
        echo "✗ --fix required"
        echo ""
        echo "  Common fixes:"
        echo "    'brighten the background'"
        echo "    'make text sharper and more legible'"
        echo "    'improve contrast'"
        echo "    'make colors more vibrant'"
        echo "    'remove the blue cast'"
        echo "    'add soft lighting from the left'"
        echo "    'make it look more professional'"
        echo "    'increase sharpness'"
        exit 1
    fi

    echo ""
    echo "── Auto-fixing: $(basename "$INPUT")"
    echo "  Fix: $FIX"
    echo ""

    uv run "$PYTHON_SCRIPT" auto-fix \
        --input "$INPUT" \
        --fix "$FIX" \
        --filename ""

    echo ""
    echo "  Check the output. If not perfect, run another fix:"
    echo "    ./refine.sh auto-fix --input OUTPUT.png --fix 'another adjustment'"
    echo ""
    ;;

upscale)
    if [ -z "$INPUT" ]; then
        echo "✗ --input required"
        exit 1
    fi
    local res="${RESOLUTION:-4K}"
    echo ""
    echo "── Upscaling: $(basename "$INPUT") → $res"

    uv run "$PYTHON_SCRIPT" generate \
        --prompt "Upscale this image to 4K, preserve all details exactly, no changes to composition or style" \
        --format "web-hero" \
        --model "nano-banana-2" \
        --input-image "$INPUT"

    echo ""
    echo "  Upscaled version saved to outputs folder."
    echo ""
    ;;

compare)
    if [ -z "$INPUT" ]; then
        echo "✗ --input required (original)"
        exit 1
    fi
    echo ""
    echo "── Compare"
    echo "  Original:  $INPUT"
    [ -n "$COMPARE" ] && echo "  Revised:    $COMPARE"
    echo ""

    open_in_photos "$INPUT"
    if [ -n "$COMPARE" ]; then
        sleep 1
        open_in_photos "$COMPARE"
    fi
    echo ""
    ;;

approve)
    if [ -z "$INPUT" ]; then
        echo "✗ --input required"
        exit 1
    fi

    local approved_dir="$HOME/creative-studio-outputs/approved"
    mkdir -p "$approved_dir"

    local final_name="${NAME:-$(basename "$INPUT")}"
    local dest="$approved_dir/$final_name"

    cp "$INPUT" "$dest"
    echo ""
    echo "✓ Approved: $dest"
    echo ""
    echo "  All approved files:"
    ls -lh "$approved_dir/" | tail -5
    echo ""
    ;;

open)
    if [ -z "$INPUT" ]; then
        echo "✗ --input required"
        exit 1
    fi
    open_in_photos "$INPUT"
    echo ""
    ;;

*)
    echo "Unknown action: $ACTION"
    usage
    exit 1
    ;;
esac