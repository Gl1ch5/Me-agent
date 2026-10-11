#!/usr/bin/env bash
# The full promo: one long video per language and aspect (RU/EN × 16:9 for YouTube, 9:16 for TikTok).
# Output: dist/promo/full/me-<ru|en>-<16x9|9x16>.mp4 (+ .webp previews). Same local-stand rules as build_promo.sh.
set -euo pipefail
cd "$(dirname "$0")/.."
for lang in ru en; do
  for aspect in 16x9 9x16; do
    python3 scripts/promo/full.py "$lang" "$aspect"
  done
done
ls -l dist/promo/full
