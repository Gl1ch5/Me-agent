#!/usr/bin/env bash
# The full promo: all four videos (RU/EN × 16:9 for YouTube, 9:16 for TikTok), in parallel.
# Output: dist/promo/full/me-<ru|en>-<16x9|9x16>.mp4 (+ .webp previews, .cues.txt, .report.json).
# Same local-stand rules as build_promo.sh. Pass --tail to finish each video with the credits card.
#
#   bash scripts/build_promo_full.sh                    # all four at once (4 jobs)
#   PROMO_JOBS=2 bash scripts/build_promo_full.sh       # two at a time (a small machine)
#   bash scripts/build_promo_full.sh ru 16x9 --tail     # just one, with the credits
#   PROMO_ONE=14 PROMO_MARKET=12 bash scripts/build_promo_full.sh   # a tighter cut
#   PROMO_NO_MUSIC=1 bash scripts/build_promo_full.sh   # picture + sound effects only
#   PROMO_SPEED=2 bash scripts/build_promo_full.sh      # a quicker render: the typing and the pauses run at 2x
set -euo pipefail
cd "$(dirname "$0")/.."

python3 -c "import playwright, PIL" 2>/dev/null || { echo "need playwright + pillow: pip install playwright pillow && playwright install chromium" >&2; exit 1; }
ffmpeg -version >/dev/null || { echo "need ffmpeg" >&2; exit 1; }
export PYTHONUNBUFFERED=1

# the samples and the score once for all four (offline or already cached: the videos build, just quieter)
python3 scripts/promo/sfx.py || echo "warning: no sound pack / score — building without them" >&2

args=(); langs=(); aspects=(); tail=()
for a in "$@"; do case "$a" in ru|en) langs+=("$a");; 16x9|9x16) aspects+=("$a");; --tail) tail=(--tail);; esac; done
[ ${#langs[@]} -eq 0 ] && langs=(ru en)
[ ${#aspects[@]} -eq 0 ] && aspects=(16x9 9x16)

if [ ${#langs[@]} -eq 2 ] && [ ${#aspects[@]} -eq 2 ]; then
  python3 scripts/promo/full.py all "${tail[@]:-}"   # one process per variant, side by side
else
  for lang in "${langs[@]}"; do for aspect in "${aspects[@]}"; do
    echo "▸ $lang $aspect"
    python3 scripts/promo/full.py "$lang" "$aspect" "${tail[@]:-}"
  done; done
fi
ls -l dist/promo/full
