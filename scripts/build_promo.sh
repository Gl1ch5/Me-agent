#!/usr/bin/env bash
# Records the promo clips (terminal + browser) against a local stand and writes them to dist/promo/:
#   promo-1-one-file.mp4 · promo-2-continue-on-phone.mp4 · promo-3-themes-modes.mp4 · promo-4-vertical-voice.mp4 (+ .webp previews)
# Needs: python3 with fastapi, uvicorn, playwright, pyte, pillow; ffmpeg + ffprobe; DejaVu fonts;
#        a chromium for playwright (`python3 -m playwright install chromium --only-shell`) or PROMO_CHROMIUM=/path/to/chromium.
# Never touches me-agent.ru: the stand is local, the account is throwaway, the model is a mock.
set -euo pipefail
cd "$(dirname "$0")/.."

for tool in ffmpeg ffprobe python3; do
  command -v "$tool" >/dev/null || { echo "build_promo: missing $tool" >&2; exit 1; }
done
python3 -c "import fastapi, uvicorn, playwright, pyte, PIL" 2>/dev/null || {
  echo "build_promo: pip install fastapi uvicorn playwright pyte pillow" >&2; exit 1; }

python3 scripts/promo/run.py
ls -l dist/promo/*.mp4 dist/promo/*.webp
