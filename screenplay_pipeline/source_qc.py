#!/usr/bin/env python3
"""
source_qc.py — детерминированный Source-QC пре-гейт (supervision, БЕЗ LLM).

Слой L4 архитектуры v4: дешёвый детерминированный фильтр ПЕРЕД дорогим VLM
(plastic_gate_core). Ловит то, в чём обученный детектор объективнее догадок VLM:
  • ЛИЦО КРУПНЫМ ПЛАНОМ — брак ([[feedback_no_faces_in_clips]]); фигура/силуэт — ок;
  • person/object-теги — объективное обогащение пула.

Двухфакторный детект лица (урок теста 2026-07-10: YOLOv8-face ложнит на круглых
объектах — часы приняты за лицо): лицо засчитывается, только если conf≥FACE_CONF
И бокс НЕ поглощён круглым COCO-классом (clock/vase/sports ball/donut/frisbee).

API: judge_source(path) -> dict. Модели грузятся лениво и кэшируются в модуле
(батч не переинициализирует). Fail-open по загрузке моделей: если детектор не
поднялся — verdict ok=True с флагом qc_skipped (не блокируем пайплайн из-за среды).
"""
import subprocess
from pathlib import Path

import numpy as np

FACE_CONF = 0.5
FACE_AREA_CLOSEUP = 0.08          # доля кадра под лицом → «крупный план»
ROUND_COCO = {"clock", "vase", "sports ball", "donut", "frisbee", "orange", "apple"}
IOU_SUPPRESS = 0.55               # если лицо так перекрыто круглым объектом → ложняк

_yolo = None
_face = None
_loaded = False


def _load():
    global _yolo, _face, _loaded
    if _loaded:
        return
    _loaded = True
    try:
        from ultralytics import YOLO
        _yolo = YOLO("yolov8n.pt")
    except Exception as e:
        print(f"[source_qc] YOLO COCO не загрузился: {e}")
        _yolo = None
    try:
        from ultralytics import YOLO
        from huggingface_hub import hf_hub_download
        p = hf_hub_download(repo_id="arnabdhar/YOLOv8-Face-Detection", filename="model.pt")
        _face = YOLO(p)
    except Exception as e:
        print(f"[source_qc] YOLOv8-face не загрузился: {e}")
        _face = None


def _first_frame(path: str) -> "np.ndarray|None":
    import cv2
    p = Path(path)
    if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
        return cv2.imread(str(p))
    out = p.with_suffix(".sqc.png")
    subprocess.run(["ffmpeg", "-y", "-ss", "1", "-i", str(p), "-frames:v", "1", str(out)],
                   capture_output=True)
    img = cv2.imread(str(out)) if out.exists() else None
    if out.exists():
        out.unlink(missing_ok=True)
    return img


def _iou_contain(face_box, obj_box) -> float:
    """Доля площади ЛИЦА, покрытая объектом (containment, не симметричный IoU)."""
    ax1, ay1, ax2, ay2 = face_box
    bx1, by1, bx2, by2 = obj_box
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    face_area = max(1e-6, (ax2 - ax1) * (ay2 - ay1))
    return inter / face_area


def judge_source(path: str) -> dict:
    """Детерминированный вердикт источника. ok=False только при лице крупным планом."""
    _load()
    verdict = {"ok": True, "reject_reason": None, "persons": 0, "objects": [],
               "faces_kept": 0, "max_face_frac": 0.0, "closeup_face": False,
               "qc_skipped": False}
    if _yolo is None and _face is None:
        verdict["qc_skipped"] = True
        return verdict

    frame = _first_frame(path)
    if frame is None:
        verdict["qc_skipped"] = True
        verdict["reject_reason"] = "кадр не прочитан"
        return verdict
    H, W = frame.shape[:2]
    area = float(H * W)

    round_boxes = []
    if _yolo is not None:
        import supervision as sv
        res = _yolo(frame, verbose=False)[0]
        det = sv.Detections.from_ultralytics(res)
        names = res.names
        objs = [names[int(c)] for c in det.class_id]
        verdict["objects"] = sorted(set(objs))
        verdict["persons"] = sum(1 for o in objs if o == "person")
        for box, c in zip(det.xyxy, det.class_id):
            if names[int(c)] in ROUND_COCO:
                round_boxes.append(box)

    if _face is not None:
        import supervision as sv
        fres = _face(frame, verbose=False)[0]
        fdet = sv.Detections.from_ultralytics(fres)
        kept = []
        for box, conf in zip(fdet.xyxy, fdet.confidence):
            if conf < FACE_CONF:
                continue
            # двухфактор: подавить лицо, поглощённое круглым объектом (ложняк на часах/вазе)
            if any(_iou_contain(box, rb) >= IOU_SUPPRESS for rb in round_boxes):
                continue
            kept.append(box)
        verdict["faces_kept"] = len(kept)
        if kept:
            fa = [((b[2] - b[0]) * (b[3] - b[1])) / area for b in kept]
            verdict["max_face_frac"] = round(float(max(fa)), 3)
            verdict["closeup_face"] = max(fa) >= FACE_AREA_CLOSEUP

    if verdict["closeup_face"]:
        verdict["ok"] = False
        verdict["reject_reason"] = (
            f"лицо крупным планом ({verdict['max_face_frac']:.0%} кадра ≥ "
            f"{FACE_AREA_CLOSEUP:.0%}) — правило «без лиц крупным планом»")
    return verdict


if __name__ == "__main__":
    import sys, json
    print(json.dumps(judge_source(sys.argv[1]), ensure_ascii=False, indent=2))
