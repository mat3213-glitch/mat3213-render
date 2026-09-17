#!/usr/bin/env python3
"""Serialized GitHub Actions runner for the kilo-gh fanout worker.

Контракт — зеркало glm_runner.py из фабрики (mat3213-glitch/mat3213-render):
  вход:   ЯД cloud_io/kilo_ci/in/<batch_id>.json  — массив [{id, prompt, model?, variant?, timeout?}]
  выход:  ЯД cloud_io/kilo_ci/out/<batch_id>.json — массив [{id, ok, text, model, elapsed, error}]

Режимы:
  kilo_runner.py BATCH_ID                    боевой: rclone in → batch.json → прогоны → out.json → out
  kilo_runner.py local --batch F --out F     локальный (без rclone) — ручной смоук
  kilo_runner.py --selftest                  переносимый самотест (фейковый бинарник, без сети)

Прогон задачи: `kilo run --dir <scratch> --auto [--model m] [--variant v] "<промпт>"`
(--model/--variant/--auto документированы в kilo.ai/docs/code-with-ai/platforms/cli-reference).
Файловые записи модели изолированы в scratch-каталоге ephemeral раннера.
Секреты в логи не печатаются; бинарник берётся из PATH (KILO_BIN — только для тестов).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

YD_IN = "ydrive:Content factory/cloud_io/kilo_ci/in"
YD_OUT = "ydrive:Content factory/cloud_io/kilo_ci/out"
SAFE_BATCH_ID = re.compile(r"^[A-Za-z0-9_.-]+$")

TASK_MIN_TIMEOUT = 60
TASK_MAX_TIMEOUT = 1800
TASK_DEFAULT_TIMEOUT = 600
SUBPROC_GRACE = 120

KILO_BIN = os.environ.get("KILO_BIN", "kilo")        # имя/путь бинарника; "stub" = самотест
KILO_STUB_PY = os.environ.get("KILO_STUB_PY", "")    # фейковый обработчик argv для "stub"
AUTH_FILE = Path.home() / ".local" / "share" / "kilo" / "auth.json"


def _task_timeout(value: object) -> int:
    try:
        return min(max(int(value or TASK_DEFAULT_TIMEOUT), TASK_MIN_TIMEOUT), TASK_MAX_TIMEOUT)
    except (TypeError, ValueError):
        return TASK_DEFAULT_TIMEOUT


def _kilo_prefix() -> list:
    if KILO_BIN == "stub":
        if not KILO_STUB_PY:
            raise SystemExit("KILO_BIN=stub требует KILO_STUB_PY")
        return [sys.executable, KILO_STUB_PY]
    return [KILO_BIN]


def run_task(task: dict, scratch_root: Path) -> dict:
    started = time.time()
    timeout = _task_timeout(task.get("timeout"))
    raw_id = str(task.get("id", "task"))
    task_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw_id)[:80] or "task"
    scratch = scratch_root / f"task-{task_id}"
    scratch.mkdir(parents=True, exist_ok=True)
    model = task.get("model") or ""
    variant = task.get("variant") or ""
    argv = _kilo_prefix() + ["run", "--dir", str(scratch), "--auto"]
    if model:
        argv += ["-m", model]
    if variant:
        argv += ["--variant", variant]
    argv.append(task["prompt"])
    env = dict(os.environ)
    env.setdefault("KILO_DISABLE_MOUSE", "1")
    try:
        result = subprocess.run(argv, capture_output=True, text=True,
                                timeout=timeout + SUBPROC_GRACE, cwd=str(scratch), env=env)
        text = result.stdout.strip()
        ok = result.returncode == 0 and bool(text)
        error = "" if ok else (result.stderr or f"empty output (rc={result.returncode})")[-300:]
        if not ok:
            text = ""
    except subprocess.TimeoutExpired:
        text, ok, error = "", False, f"subprocess timeout {timeout + SUBPROC_GRACE}s"
    except Exception as exc:
        text, ok, error = "", False, f"{type(exc).__name__} {str(exc)[:200]}"
    return {"id": task.get("id"), "ok": ok, "text": text,
            "model": model or "gateway-default",
            "elapsed": round(time.time() - started, 1), "error": error}


def _run_argv(argv: list, timeout: int) -> tuple[int, str]:
    try:
        result = subprocess.run(_kilo_prefix() + argv, capture_output=True, text=True,
                                timeout=timeout, env=dict(os.environ))
        return result.returncode, (result.stdout or "").strip()
    except subprocess.TimeoutExpired:
        return 124, ""
    except FileNotFoundError:
        return 127, ""


def diagnostics() -> dict:
    """Безопасная диагностика: версия, наличие кредов (bool, не значения), каталог моделей."""
    diag = {"version": "", "auth_env": bool(os.environ.get("KILO_API_KEY")),
            "auth_file": AUTH_FILE.is_file(), "models_rc": None, "models_count": None,
            "models_head": []}
    rc, out = _run_argv(["--version"], 30)
    diag["version"] = out.splitlines()[0][:80] if out else ""
    diag["version_rc"] = rc
    rc, out = _run_argv(["models"], 60)
    diag["models_rc"] = rc
    lines = [ln for ln in out.splitlines() if ln.strip()]
    diag["models_count"] = len(lines)
    diag["models_head"] = lines[:3]
    return diag


# ── Самотест ────────────────────────────────────────────────────────────────────

STUB_SOURCE = '''#!/usr/bin/env python3
import hashlib
import sys

argv = sys.argv[1:]
if not argv or argv[0] != "run":
    sys.stderr.write(f"stub: expected 'run', got {argv[:3]}")
    raise SystemExit(2)
prompt = argv[-1]
rest = argv[1:-1]
model = rest[rest.index("-m") + 1] if "-m" in rest else ""
sys.stdout.write(
    f"KILO_STUB_OK model={model or 'default'} "
    f"prompt_sha={hashlib.sha256(prompt.encode()).hexdigest()[:8]}\\n"
)
'''


def selftest() -> int:
    """Полный цикл батча с фейковым бинарником: контракт, модель, sha промпта."""
    global KILO_BIN, KILO_STUB_PY
    scratch_root = Path(tempfile.mkdtemp(prefix="kilo-runner-selftest-"))
    stub = scratch_root / "kilo-stub.py"
    stub.write_text(STUB_SOURCE, encoding="utf-8")
    stub.chmod(0o755)
    batch = [
        {"id": "s1", "prompt": "Ответь одним словом: готов."},
        {"id": "s2", "prompt": "pong", "model": "kilo/nova-2", "timeout": 120},
        {"id": "s3", "prompt": "fail-me", "skip_marker": "не влияет"},
    ]
    KILO_BIN = "stub"
    KILO_STUB_PY = str(stub)
    results = []
    for task in batch:
        results.append(run_task(task, scratch_root / "cases"))
    checks = [
        ("ключи контракта", all(
            {"id", "ok", "text", "model", "elapsed", "error"} <= set(r) for r in results)),
        ("s1 ok + sha промпта", results[0]["ok"] and "prompt_sha=" in results[0]["text"]),
        ("s2 модель echoed", results[1]["ok"] and "model=kilo/nova-2" in results[1]["text"]),
        ("s1 sha без модели", "model=default" in results[0]["text"]),
    ]
    ok = all(passed for _name, passed in checks)
    print(json.dumps(results, ensure_ascii=False, indent=2))
    print("\n--- selftest ---")
    for name, passed in checks:
        print(f"{'✅' if passed else '❌'} {name}")
    print(f"selftest: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


# ── Основной цикл ───────────────────────────────────────────────────────────────

def run_batch(tasks: list, scratch_root: Path) -> list:
    results = []
    for task in tasks:
        results.append(run_task(task, scratch_root))
    return results


def _summary(label: str, results: list) -> None:
    ok = sum(bool(x["ok"]) for x in results)
    print(f"kilo-gh {label}: {ok}/{len(results)} ok")


def main() -> None:
    args = sys.argv[1:]
    if args == ["--selftest"]:
        raise SystemExit(selftest())

    scratch_root = Path(tempfile.mkdtemp(prefix="kilo-runner-"))
    if args and args[0] == "local":
        # Локальный режим: без rclone (ручной смоук).
        batch_path, out_path = "", ""
        for flag, value in zip(args[1:], args[2:]):
            if flag == "--batch":
                batch_path = value
            elif flag == "--out":
                out_path = value
        if not batch_path or not out_path:
            raise SystemExit("usage: kilo_runner.py local --batch FILE --out FILE")
        tasks = json.loads(Path(batch_path).read_text(encoding="utf-8"))
        results = run_batch(tasks, scratch_root)
        Path(out_path).write_text(json.dumps(results, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        _summary("local", results)
        raise SystemExit(0 if all(x["ok"] for x in results) else 1)

    if len(args) != 1:
        raise SystemExit("usage: kilo_runner.py BATCH_ID | local --batch F --out F | --selftest")
    batch_id = args[0]
    if not SAFE_BATCH_ID.fullmatch(batch_id):
        raise SystemExit("invalid batch id")

    subprocess.run(["rclone", "copyto", f"{YD_IN}/{batch_id}.json", "batch.json"], check=True)
    with open("batch.json", encoding="utf-8") as stream:
        tasks = json.load(stream)
    print(f"kilo-gh: батч {batch_id}, задач {len(tasks)}", file=sys.stderr)
    print(f"kilo-gh diagnostics: {json.dumps(diagnostics(), ensure_ascii=False)}", file=sys.stderr)

    results = run_batch(tasks, scratch_root)
    with open("out.json", "w", encoding="utf-8") as stream:
        json.dump(results, stream, ensure_ascii=False, indent=2)
    subprocess.run(["rclone", "copyto", "out.json", f"{YD_OUT}/{batch_id}.json"], check=True)
    _summary(batch_id, results)


if __name__ == "__main__":
    main()
