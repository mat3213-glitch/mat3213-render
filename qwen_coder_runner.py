#!/usr/bin/env python3
"""qwen_coder_runner.py — облачный драйвер воркера qwen-coder-gh (см. .github/workflows/qwen_coder_fanout.yml).

Зеркало litellm_runner.py, но гоняет qwen_chat.py на GH-раннере.
Поток: читает батч с ЯД qwen_coder_ci/in/<batch_id>.json → для каждой задачи
ПОСЛЕДОВАТЕЛЬНО вызывает qwen_chat.py (одна сессия Chromium, параллель уронит)
→ пишет массив результатов на ЯД qwen_coder_ci/out/<batch_id>.json.
"""
import json
import os
import subprocess
import sys
import time

YD_IN = "ydrive:Content factory/cloud_io/qwen_coder_ci/in"
YD_OUT = "ydrive:Content factory/cloud_io/qwen_coder_ci/out"
QWEN_CHAT = os.path.join(os.path.dirname(__file__), "qwen", "qwen_chat.py")


def main():
    bid = sys.argv[1]
    subprocess.run(["rclone", "copyto", f"{YD_IN}/{bid}.json", "batch.json"], check=True)
    tasks = json.load(open("batch.json"))

    out = []
    for t in tasks:
        model = t.get("model") or ""
        prompt = t["prompt"]
        t0 = time.time()
        args = ["python3", QWEN_CHAT, prompt, "--model", model, "--timeout", "240"]
        # per-task try: одна зависшая/упавшая задача не должна убить весь батч (иначе out.json
        # не запишется и fanout получит «таймаут» по всем). Каждый исход → строка результата.
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=330)
            text = r.stdout.strip()
            ok = r.returncode == 0 and text != ""
            err = "" if ok else (r.stderr or "")[-200:]
        except subprocess.TimeoutExpired:
            text, ok, err = "", False, "subprocess timeout 330s"
        except Exception as e:
            text, ok, err = "", False, f"{type(e).__name__} {str(e)[:160]}"
        out.append({
            "id": t.get("id"),
            "ok": ok,
            "text": text,
            "model": "qwen-coder",
            "elapsed": round(time.time() - t0, 1),
            "error": err,
        })

    json.dump(out, open("out.json", "w"), ensure_ascii=False)
    subprocess.run(["rclone", "copyto", "out.json", f"{YD_OUT}/{bid}.json"], check=True)
    ok = sum(1 for x in out if x.get("ok"))
    print(f"qwen-coder-gh: {ok}/{len(out)} ok → {YD_OUT}/{bid}.json")


if __name__ == "__main__":
    main()
