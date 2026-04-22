#!/bin/bash
# Wrapper for mom to use creative-studio skill
# Usage: mom-wrapper.sh generate "prompt" [input-image]

set -e

COMMAND=$1
PROMPT="$2"
INPUT_IMAGE="$3"
OUTPUT_DIR="$HOME/mom/data/outputs"
mkdir -p "$OUTPUT_DIR"

cd ~/.pi/agent/skills/creative-studio

if [ "$COMMAND" = "generate" ]; then
    if [ -n "$INPUT_IMAGE" ] && [ -f "$INPUT_IMAGE" ]; then
        uv run creative_studio.py direct --prompt "$PROMPT" --input-image "$INPUT_IMAGE" --model gemini-3.1-flash-image-preview &> /tmp/creative-studio.log
    else
        uv run creative_studio.py direct --prompt "$PROMPT" --model gemini-3.1-flash-image-preview &> /tmp/creative-studio.log
    fi
    
    # Find the most recent output
    LATEST=$(ls -t ~/Downloads/creative-studio-outputs/*/*/*.png 2>/dev/null | head -1)
    if [ -n "$LATEST" ]; then
        cp "$LATEST" "$OUTPUT_DIR/generated-$(date +%s).png"
        echo "IMAGE: $OUTPUT_DIR/generated-$(date +%s).png"
    else
        echo "No image generated. Check /tmp/creative-studio.log"
    fi
elif [ "$COMMAND" = "variations" ]; then
    # Similar for variations...
    echo "Variations mode - TODO"
else
    echo "Usage: mom-wrapper.sh generate \"prompt\" [input-image]"
    exit 1
fi
