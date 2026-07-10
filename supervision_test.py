#!/usr/bin/env python3
"""
supervision_test.py — смоук-тест roboflow/supervision на НАШИХ vision-задачах (B-adopt).

Цель: проверить, даёт ли ОБУЧЕННЫЙ детектор (YOLOv8 персоны/объекты + mediapipe лица),
обёрнутый в supervision, более надёжный класс, чем наш VLM-судья (plastic_gate/
final_qc). supervision — детерминированная CV-библиотека (боксы/маски/аннотация),
LLM внутри НЕТ.

Наши задачи, куда это ложится:
  • no-close-up-faces гейт: детектор лиц + доля площади бокса → «лицо крупным
    планом» (фигура/силуэт — можно, крупное ЛИЦО — брак; [[feedback_no_faces_in_clips]]);
  • обогащение тегов пула детерминированными person/object-боксами (вместо
    только VLM-описания).

Тест НЕ вшивает supervision в прод — гоняет на сэмпле кадров пула с ЯД, кладёт
аннотированные кадры + JSON-вердикт в сессионную ЯД-папку для ревью глазами.
"""
import os
import sys
import json
import subprocess
import tempfile
from pathlib import Path

import cv2
import numpy as np
import mediapipe as mp
import supervision as sv
from ultralytics import YOLO

YD = "ydrive:Content factory"
CATALOG = "cloud_io/ai_pool_catalog.jsonl"
OUT_YD = "cloud_io/preview/2026-07-10_supervision_creative/01_supervision_test"
WORK = Path(tempfile.mkdtemp(prefix="sv_"))
FACE_AREA_CLOSEUP = 0.08   # доля кадра под лицом → «крупный план лица» (эвристика)
N_SAMPLE = 10


def log(m): print(m, flush=True)


def rc(*a):
    return subprocess.run(["rclone", *a], capture_output=True, text=True)


def yd_cat(rel):
    r = rc("cat", f"{YD}/{rel}")
    if r.returncode != 0:
        raise RuntimeError(f"rclone cat {rel}: {r.stderr}")
    return r.stdout


def yd_pull(rel, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    r = rc("copyto", f"{YD}/{rel}", str(dest))
    return r.returncode == 0


def yd_push(src, rel):
    rc("copyto", str(src), f"{YD}/{rel}")


def sample_catalog() -> list[dict]:
    """Курируем сэмпл: сперва клипы с фигурой/силуэтом (стресс для детектора лиц)
    и close-планы, добиваем разнообразием по движку."""
    rows = [json.loads(l) for l in yd_cat(CATALOG).splitlines() if l.strip()]
    def is_fig(r):
        blob = " ".join(map(str, r.get("tags", []) + [r.get("hero_candidate", "")]))
        return any(k in blob for k in ("фигур", "силуэт", "человек", "люд"))
    figs = [r for r in rows if is_fig(r)]
    close = [r for r in rows if r.get("scale") == "close" and r not in figs]
    rest = [r for r in rows if r not in figs and r not in close]
    picked, seen = [], set()
    for bucket in (figs, close, rest):
        for r in bucket:
            if len(picked) >= N_SAMPLE:
                break
            if r["id"] not in seen:
                picked.append(r); seen.add(r["id"])
    return picked[:N_SAMPLE]


def first_frame(media: Path) -> Path:
    """PNG/JPG — как есть; видео — первый значимый кадр (t=1s) через ffmpeg."""
    if media.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
        return media
    out = media.with_suffix(".frame.png")
    subprocess.run(["ffmpeg", "-y", "-ss", "1", "-i", str(media), "-frames:v", "1",
                    str(out)], capture_output=True)
    return out if out.exists() else media


def detect_faces_mp(bgr) -> sv.Detections:
    """mediapipe FaceDetection (model_selection=1 = full-range) → sv.Detections.
    Обученный детектор, не зависит от cv2-каскадов (ultralytics ставит headless-opencv)."""
    H, W = bgr.shape[:2]
    rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    with mp.solutions.face_detection.FaceDetection(
            model_selection=1, min_detection_confidence=0.5) as fd:
        res = fd.process(rgb)
    boxes = []
    if res.detections:
        for d in res.detections:
            bb = d.location_data.relative_bounding_box
            x1 = max(0.0, bb.xmin * W); y1 = max(0.0, bb.ymin * H)
            x2 = min(float(W), (bb.xmin + bb.width) * W)
            y2 = min(float(H), (bb.ymin + bb.height) * H)
            if x2 > x1 and y2 > y1:
                boxes.append([x1, y1, x2, y2])
    if not boxes:
        return sv.Detections.empty()
    return sv.Detections(xyxy=np.array(boxes, dtype=float),
                         confidence=np.ones(len(boxes)),
                         class_id=np.zeros(len(boxes), dtype=int))


def annotate(bgr, dets: sv.Detections, labels, color):
    """Универсально по версиям supervision: BoxAnnotator | BoundingBoxAnnotator."""
    pal = sv.ColorPalette.from_hex([color])
    BoxCls = getattr(sv, "BoxAnnotator", None) or getattr(sv, "BoundingBoxAnnotator")
    box = BoxCls(color=pal)
    img = box.annotate(scene=bgr.copy(), detections=dets)
    try:
        lab = sv.LabelAnnotator(color=pal, text_scale=0.4)
        img = lab.annotate(scene=img, detections=dets, labels=labels)
    except Exception as e:
        log(f"  (label annotator пропущен: {e})")
    return img


def main() -> int:
    log("== supervision smoke-test ==")
    log(f"supervision {sv.__version__}, opencv {cv2.__version__}")
    model = YOLO("yolov8n.pt")          # COCO 80 классов, авто-докачка ~6MB
    sample = sample_catalog()
    log(f"сэмпл: {len(sample)} медиа")

    report = []
    for i, r in enumerate(sample):
        rel = r["path"]
        media = WORK / Path(rel).name
        if not yd_pull(rel, media):
            log(f"[{i}] {rel}: не скачался — пропуск"); continue
        frame_path = first_frame(media)
        bgr = cv2.imread(str(frame_path))
        if bgr is None:
            log(f"[{i}] {rel}: кадр не читается — пропуск"); continue
        H, W = bgr.shape[:2]
        area = float(H * W)

        # 1) YOLO — персоны/объекты
        res = model(bgr, verbose=False)[0]
        det = sv.Detections.from_ultralytics(res)
        names = res.names
        yolo_labels = [f"{names[c]} {p:.2f}" for c, p in zip(det.class_id, det.confidence)]
        persons = int(sum(1 for c in det.class_id if names[int(c)] == "person"))
        objects = sorted({names[int(c)] for c in det.class_id})

        # 2) mediapipe — лица + доля площади крупнейшего
        faces = detect_faces_mp(bgr)
        max_face_frac = 0.0
        if len(faces) > 0:
            fa = (faces.xyxy[:, 2] - faces.xyxy[:, 0]) * (faces.xyxy[:, 3] - faces.xyxy[:, 1])
            max_face_frac = float(fa.max() / area)
        closeup_face = max_face_frac >= FACE_AREA_CLOSEUP

        # аннотация: объекты зелёным, лица красным
        img = annotate(bgr, det, yolo_labels, "#00FF66")
        img = annotate(img, faces, [f"face {max_face_frac:.0%}"] * len(faces), "#FF0033")
        ann_name = f"{i:02d}_{Path(rel).stem}.jpg"
        ann_path = WORK / ann_name
        cv2.imwrite(str(ann_path), img)
        yd_push(ann_path, f"{OUT_YD}/{ann_name}")

        verdict = {
            "id": r["id"], "path": rel, "tags": r.get("tags"),
            "scale": r.get("scale"),
            "yolo_objects": objects, "persons": persons,
            "faces": int(len(faces)), "max_face_frac": round(max_face_frac, 3),
            "closeup_face_FLAG": closeup_face,
            "annotated": ann_name,
        }
        report.append(verdict)
        log(f"[{i}] {r['id']}: persons={persons} objects={objects} "
            f"faces={len(faces)} face_frac={max_face_frac:.0%} "
            f"{'⚠CLOSEUP' if closeup_face else ''}")

    # сводка
    rep_path = WORK / "report.json"
    rep_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    yd_push(rep_path, f"{OUT_YD}/report.json")

    md = ["# supervision smoke-test — вердикты\n",
          f"Сэмпл {len(report)} кадров пула. Детектор: YOLOv8n (COCO, персоны/объекты) "
          f"+ mediapipe (лица). supervision = аннотация/боксы, детерминированно, без LLM.\n",
          "| # | id | scale | persons | objects | faces | face% | closeup? |",
          "|---|---|---|---|---|---|---|---|"]
    for i, v in enumerate(report):
        md.append(f"| {i} | {v['id']} | {v['scale']} | {v['persons']} | "
                  f"{', '.join(v['yolo_objects']) or '—'} | {v['faces']} | "
                  f"{v['max_face_frac']:.0%} | {'⚠ДА' if v['closeup_face_FLAG'] else 'нет'} |")
    md += ["\n## Как читать",
           "- **persons/objects** — обученный детектор против VLM-догадок (для тегов пула).",
           f"- **closeup?** — лицо занимает ≥{FACE_AREA_CLOSEUP:.0%} кадра → кандидат в брак "
           "по правилу «без лиц крупным планом» (фигура/силуэт — ок).",
           "- Смотри аннотированные .jpg рядом: зелёные боксы=объекты, красные=лица."]
    md_path = WORK / "SUMMARY.md"
    md_path.write_text("\n".join(md), encoding="utf-8")
    yd_push(md_path, f"{OUT_YD}/SUMMARY.md")

    log(f"\nГотово. Вердикты+аннотации → ЯД {OUT_YD}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
