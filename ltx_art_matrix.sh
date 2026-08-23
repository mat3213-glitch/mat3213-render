#!/usr/bin/env bash
# Матрица калибровки LTX на артах: 3 арта x {template, content}, гейт из лога ядра,
# 1 ретрай seed=43 на WEAK/DECAYED, потолок 8 ядер.
set -u
cd "$(dirname "$0")"

CALIB_DIR=/tmp/opencode/ltx_calib
YD_DEST="Content factory/cloud_io/render_jobs/2026-08-23_ltx_art_calib"
TPL_PROMPT="cinematic motion, smooth animation, high quality, atmospheric movement"
KERNELS=0
MAX_KERNELS=8

export KAGGLE_USERNAME KAGGLE_KEY
KAGGLE_USERNAME=$(python3 -c "import json;print(json.load(open('$HOME/.kaggle/kaggle.json'))['username'])")
KAGGLE_KEY=$(python3 -c "import json;print(json.load(open('$HOME/.kaggle/kaggle.json'))['key'])")

run_cell() { # $1 cell_name  $2 art_path  $3 prompt  $4 seed
  KERNELS=$((KERNELS+1))
  echo "=== [$KERNELS/$MAX_KERNELS] $1 (seed=$4)"
  JOB_ID="2026-08-23_ltx_art_calib/$1" SCENE_IDX="$1" \
  PROMPT="$3" IMG_LOCAL="$2" DEST_FOLDER="$YD_DEST" OUT_NAME="$1.mp4" SEED="$4" \
    python3 ltx_i2v_gen.py > "$CALIB_DIR/log_$1.txt" 2>&1
  local rc=$?
  local gate
  gate=$(grep -o 'GATE verdict=[A-Z]*' "$CALIB_DIR/log_$1.txt" | tail -1 | cut -d= -f2)
  echo "--- rc=$rc gate=${gate:-NONE}"
  tail -3 "$CALIB_DIR/log_$1.txt"
  echo "$gate"
}

mapfile -t CELLS < "$CALIB_DIR/cells.txt"
for line in "${CELLS[@]}"; do
  [ -z "$line" ] && continue
  IFS='|' read -r cell art prompt_base <<< "$line"
  v=$(run_cell "${cell}" "$art" "$prompt_base" 42)
  if [ "$v" != "ALIVE" ] && [ "$KERNELS" -lt "$MAX_KERNELS" ]; then
    echo "--- ретрай ${cell}_r43 (был: ${v:-FAIL})"
    run_cell "${cell}_r43" "$art" "$prompt_base" 43
  fi
done
echo "=== ГОТОВО, ядер использовано: $KERNELS"
