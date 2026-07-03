#!/usr/bin/env python3
"""litellm_runner.py — облачный драйвер воркера litellm-gh (см. Instrument/AgentQueue/fanout.py).

US-IP раннер GH Actions открывает ВСЕ free-провайдеры (Groq/Cerebras тоже, которые RU-IP режет).
Поток: читает батч с ЯД litellm_ci/in/<batch_id>.json → гоняет каждый промпт через litellm
с ФОЛБЭК-списком LITELLM_GH_MODELS (первый живой выигрывает) → пишет массив результатов
на ЯД litellm_ci/out/<batch_id>.json (формат как у fanout: {id,ok,text,model,elapsed,error}).
"""
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

MODELS = [m for m in os.environ.get("LITELLM_GH_MODELS", "").split(",") if m.strip()] or [
    "groq/llama-3.3-70b-versatile",
    "openrouter/meta-llama/llama-3.3-70b-instruct:free",
    "gemini/gemini-2.0-flash",
    "github/gpt-4o-mini",
    # доп. фолбэк-слой (каталог awesome-free-llm-apis, 2026-07-03): те же ключи
    # (OPENROUTER_API_KEY/GEMINI_API_KEY), новые free-модели той же платформы —
    # шире охват при 429 на топовых моделях, новых секретов не требуется.
    "openrouter/qwen/qwen-2.5-72b-instruct:free",
    "openrouter/deepseek/deepseek-chat:free",
    "gemini/gemini-1.5-flash",
]
YD_IN = "ydrive:Content factory/cloud_io/litellm_ci/in"
YD_OUT = "ydrive:Content factory/cloud_io/litellm_ci/out"


def call(prompt, system, timeout=120, max_tokens=1024):
    if os.environ.get("GITHUB_TOKEN") and not os.environ.get("GITHUB_API_KEY"):
        os.environ["GITHUB_API_KEY"] = os.environ["GITHUB_TOKEN"]
    import litellm
    litellm.drop_params = True
    msgs = ([{"role": "system", "content": system}] if system else []) + \
           [{"role": "user", "content": prompt}]
    last = ""
    for m in MODELS:
        t = time.time()
        try:
            r = litellm.completion(model=m, messages=msgs, timeout=timeout, max_tokens=max_tokens)
            txt = (r.choices[0].message.content or "").strip()
            if txt:
                return {"ok": True, "text": txt, "model": m,
                        "elapsed": round(time.time() - t, 1), "error": ""}
            last = f"{m}: пустой ответ"
        except Exception as e:
            last = f"{m}: {type(e).__name__} {str(e)[:140]}"
    return {"ok": False, "text": "", "model": MODELS[0], "elapsed": 0.0, "error": last}


def main():
    bid = sys.argv[1]
    subprocess.run(["rclone", "copyto", f"{YD_IN}/{bid}.json", "batch.json"], check=True)
    tasks = json.load(open("batch.json"))

    def run(t):
        r = call(t["prompt"], t.get("system"))
        r["id"] = t.get("id")
        return r

    with ThreadPoolExecutor(max_workers=4) as ex:
        out = list(ex.map(run, tasks))
    json.dump(out, open("out.json", "w"), ensure_ascii=False)
    subprocess.run(["rclone", "copyto", "out.json", f"{YD_OUT}/{bid}.json"], check=True)
    ok = sum(1 for x in out if x.get("ok"))
    print(f"litellm-gh: {ok}/{len(out)} ok → {YD_OUT}/{bid}.json")


if __name__ == "__main__":
    main()
