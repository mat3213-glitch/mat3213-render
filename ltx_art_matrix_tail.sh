#!/usr/bin/env bash
# Хвост матрицы LTX-калибровки: коридорная пара + ретраи не-ALIVE ячеек.
# Вердикт = ТОЛЬКО последняя строка stdout (урок основного раннера).
set -u
cd /home/yaro/content_factory/github_actions_clips
CALIB_DIR=/tmp/opencode/ltx_calib
YD_DEST="Content factory/cloud_io/render_jobs/2026-08-23_ltx_art_calib"

export KAGGLE_USERNAME KAGGLE_KEY
KAGGLE_USERNAME=$(python3 -c "import json;print(json.load(open('$HOME/.kaggle/kaggle.json'))['username'])")
KAGGLE_KEY=$(python3 -c "import json;print(json.load(open('$HOME/.kaggle/kaggle.json'))['key'])")

TPL="cinematic motion, smooth animation, high quality, atmospheric movement"

cnt_prompt() { # $1 stem
  python3 - "$1" <<'PYEOF'
import sys
MOT={"door":"the heavy door slowly swings open, spilling light through the doorway",
 "corridor":"deep perspective slowly pulls the viewer down the corridor",
 "escalator":"the escalator handrails glide steadily upward",
 "train":"a train glides past the platform, lights streaking",
 "window":"curtains flutter softly by the windows",
 "interior":"dust motes drift through the room's window light"}
HINTS={"art_warm_office":["window","interior"],"art_metro_station":["escalator","train"],"art_corridor":["corridor","door"]}
s=[MOT[k] for k in HINTS[sys.argv[1]]]
print("A flat 2D poster illustration, duotone art style, bold graphic shapes. "
      + " ".join(x.capitalize()+"." for x in s)
      + " Slow cinematic push-in, subtle filmic motion.")
PYEOF
}

run_cell() { # $1 cell  $2 art  $3 prompt  $4 seed
  echo "=== $1 (seed=$4)"
  JOB_ID="2026-08-23_ltx_art_calib/$1" SCENE_IDX="$1" \
  PROMPT="$3" IMG_LOCAL="$2" DEST_FOLDER="$YD_DEST" OUT_NAME="$1.mp4" SEED="$4" \
    python3 ltx_i2v_gen.py > "$CALIB_DIR/log_$1.txt" 2>&1
  local rc=$?
  echo "rc=$rc"
  grep -h "GATE" "$CALIB_DIR/log_$1.txt" | tail -1
}

run_checked() { # $1 cell  $2 art  $3 prompt
  local out v rc
  out=$(run_cell "$1" "$2" "$3" 42)
  rc=$(printf '%s' "$out" | grep -E "^rc=" | tail -1)
  v=$(printf '%s' "$out" | grep "^GATE" | tail -1)
  echo "VERDICT $1: ${v:-FAIL} ${rc}"
  if [[ "${v:-}" != GATE*ALIVE* ]]; then
    out=$(run_cell "${1}_r43" "$2" "$3" 43)
    rc=$(printf '%s' "$out" | grep -E "^rc=" | tail -1)
    v=$(printf '%s' "$out" | grep "^GATE" | tail -1)
    echo "RETRY $1 -> ${v:-FAIL} ${rc}"
  fi
}

ART=/tmp/opencode/ltx_calib/arts/art_corridor.jpg
METRO=/tmp/opencode/ltx_calib/arts/art_metro_station.jpg
# metro_tpl уже закрыт (GATE ALIVE .934 + r43 .857) — не перегонять и не перезаписывать ЯД.
run_checked art_metro_station_cnt "$METRO" "$(cnt_prompt art_metro_station)"
run_checked corridor_tpl "$ART" "$TPL"
run_checked corridor_cnt "$ART" "$(cnt_prompt art_corridor)"

for done_cell in art_warm_office_cnt art_metro_station_tpl art_metro_station_cnt; do
  g=$(grep -h "GATE" "$CALIB_DIR/log_${done_cell}.txt" 2>/dev/null | tail -1)
  [ -z "$g" ] && { echo "SKIP $done_cell (нет лога)"; continue; }
  [[ "$g" == *ALIVE* ]] && { echo "SKIP $done_cell ($g)"; continue; }
  [ -f "$CALIB_DIR/log_${done_cell}_r43.txt" ] && { echo "SKIP $done_cell (ретрай уже был)"; continue; }
  stem="${done_cell%_cnt}"; stem="${stem%_tpl}"
  kind="${done_cell##*_}"
  p="$TPL"; [ "$kind" = "cnt" ] && p="$(cnt_prompt "$stem")"
  run_checked "$done_cell" "/tmp/opencode/ltx_calib/arts/${stem}.jpg" "$p"
done
echo "=== TAIL-DONE"
