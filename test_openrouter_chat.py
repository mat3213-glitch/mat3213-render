# Тесты движка облачного openrouter-чата (замена Kimi в @ghmimo_bot).
# Сеть, rclone и API не дергаются: только чистые функции и подмены.
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "Instrument" / "CnChat"))

import openrouter_chat as orc


def test_safe_key_matches_kimi_protocol():
    # Протокол один с kimi_chat.yml: ':', '/', ' ' → '_', по краям чисто.
    assert orc.safe_key("-1003946370426:5") == "-1003946370426_5"
    assert orc.safe_key("a/b c:d") == "a_b_c_d"


def test_pick_facts_explicit_slugs_first():
    idx = "- что-то (foo.md) — описание\n- другое (bar.md) — иное\n"
    assert orc.pick_facts("смотри [[bar]]", idx, 3) == ["bar"]


def test_pick_facts_scores_index_words():
    idx = "- воркер текста (text_worker.md) — kimi openrouter текст\n"
    assert orc.pick_facts("почини текстовый воркер kimi", idx, 3) == ["text_worker"]


def test_repo_tree_caps_and_counts(tmp_path):
    (tmp_path / "a.py").write_text("x")
    (tmp_path / "b.md").write_text("y")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    tree = orc.repo_tree(tmp_path, cap=1000)
    assert "a.py" in tree and "b.md" in tree
    tiny = orc.repo_tree(tmp_path, cap=5)
    assert "не влезло" in tiny


def test_attach_files_only_inside_root(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "ok.py").write_text("print(1)")
    outside = tmp_path.parent / "evil.py"
    outside.write_text("secret")
    msg = "почини sub/ok.py и ../../../evil.py"
    parts = orc.attach_files(tmp_path, msg)
    joined = "\n".join(parts)
    assert len(parts) == 1
    assert "sub/ok.py" in joined and "print(1)" in joined
    assert "secret" not in joined


def test_attach_files_limit(tmp_path):
    for i in range(6):
        (tmp_path / f"f{i}.py").write_text("x" * 20)
    msg = " ".join(f"f{i}.py" for i in range(6))
    assert len(orc.attach_files(tmp_path, msg)) == orc.FILE_LIMIT


def test_build_prompt_sections_and_cap(monkeypatch, tmp_path):
    memdir = tmp_path / "projmem"
    memdir.mkdir()
    (memdir / "MEMORY_CORE.md").write_text("C" * 100)
    (memdir / "PROJECT_MAP.md").write_text("M" * 100)
    (memdir / "NOW.md").write_text("N" * 100)
    facts_idx = memdir / "facts"
    facts_idx.mkdir()
    (facts_idx / "MEMORY.md").write_text("- тема (t1.md) — описание темы\n")
    (tmp_path / "t1.md").write_text("FACT-BODY")
    (tmp_path / "in_repo.py").write_text("repo file")

    monkeypatch.setattr(orc, "MEM", str(tmp_path / "yd_mem"))
    monkeypatch.setattr(orc, "_ROOT", tmp_path)
    monkeypatch.setenv("MESSAGE", "разбери in_repo.py и тему [[t1]]")
    monkeypatch.setenv("THREAD_KEY", "c:1")
    # rclone copy памяти — единственный реальный subprocess в build_prompt; гасим его
    monkeypatch.setattr(orc.subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess([], 0, stdout=b"", stderr=b""))
    # чтение памяти — из локальной заглушки вместо /tmp/projmem
    monkeypatch.setattr(orc, "mem_read",
                        lambda rel, cap=None: _cap((memdir / rel).read_text() if (memdir / rel).exists() else "", cap))
    monkeypatch.setattr(orc, "yd_read",
                        lambda remote, default="": "FACT-BODY" if remote.endswith("t1.md") else default)
    monkeypatch.setattr(orc, "thread_history", lambda key: [{"q": "prev", "a": "ans"}])

    prompt = orc.build_prompt()
    assert "C" * 100 in prompt                    # core приехал
    assert "ТЕКУЩАЯ ЗАДАЧА" in prompt
    assert "in_repo.py" in prompt                 # дерево репо
    assert "repo file" in prompt                  # авто-прицепка файла
    assert "FACT-BODY" in prompt                  # факт-файл по [[слугу]]
    assert "Задача: prev" in prompt               # история треда
    assert len(prompt) <= orc.PROMPT_CAP + 100
    assert Path("/tmp/prompt.txt").exists()


def _cap(text: str, cap) -> str:
    return text[:cap] if cap else text


def test_run_engine_ok(tmp_path, monkeypatch):
    import text_openrouter as tor

    prompt = tmp_path / "prompt.txt"
    prompt.write_text("P", encoding="utf-8")
    out = tmp_path / "out"
    monkeypatch.setattr(tor, "generate",
                        lambda *a, **k: ("ОТВЕТ-ТЕКСТ", "openrouter/nvidia/nemotron-3-super-120b-a12b:free"))
    rc = orc.run_engine(str(prompt), str(out))
    assert rc == 0
    assert (out / "clean.txt").read_text(encoding="utf-8") == "ОТВЕТ-ТЕКСТ"
    assert (out / "model.txt").read_text(encoding="utf-8") == "openrouter/nvidia/nemotron-3-super-120b-a12b:free"


def test_run_engine_old_driver_str_return(tmp_path, monkeypatch):
    # Старые/чужие драйверы возвращают str — читаем без модели, без падения
    import text_openrouter as tor

    prompt = tmp_path / "prompt.txt"
    prompt.write_text("P", encoding="utf-8")
    out = tmp_path / "out"
    monkeypatch.setattr(tor, "generate", lambda *a, **k: "ПРОСТОЙ")
    rc = orc.run_engine(str(prompt), str(out))
    assert rc == 0
    assert (out / "clean.txt").read_text(encoding="utf-8") == "ПРОСТОЙ"
    assert (out / "model.txt").read_text(encoding="utf-8") == ""


def test_run_engine_failure_writes_err(tmp_path, monkeypatch):
    import text_openrouter as tor

    prompt = tmp_path / "prompt.txt"
    prompt.write_text("P", encoding="utf-8")
    out = tmp_path / "out"

    def boom(*a, **k):
        raise RuntimeError("OpenRouter не ответил — all models failed")

    monkeypatch.setattr(tor, "generate", boom)
    rc = orc.run_engine(str(prompt), str(out))
    assert rc == 1
    assert (out / "clean.txt").read_text(encoding="utf-8") == ""
    assert "не ответил" in (out / "err.txt").read_text(encoding="utf-8")
    assert (out / "model.txt").read_text(encoding="utf-8") == ""


def test_run_engine_missing_key_is_error(tmp_path, monkeypatch):
    # text_openrouter.generate без ключа бросает RuntimeError → rc=1, а не traceback воркфлоу
    import text_openrouter as tor

    prompt = tmp_path / "prompt.txt"
    prompt.write_text("P", encoding="utf-8")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(tor, "generate",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("нет OPENROUTER_API_KEY")))
    rc = orc.run_engine(str(prompt), str(tmp_path / "out"))
    assert rc == 1


def test_run_engine_web_flag_from_env(tmp_path, monkeypatch):
    # OR_WEB=1 → веб-флаг доходит до драйвера; иначе по умолчанию выкл
    import text_openrouter as tor

    prompt = tmp_path / "prompt.txt"
    prompt.write_text("P", encoding="utf-8")

    def probe(tag):
        seen = {}
        monkeypatch.setattr(tor, "generate",
                            lambda *a, **k: (seen.update(k), ("т", "m"))[1])
        assert orc.run_engine(str(prompt), str(tmp_path / tag)) == 0
        return seen

    monkeypatch.delenv("OR_WEB", raising=False)
    assert probe("o1").get("web") is False
    monkeypatch.setenv("OR_WEB", "1")
    assert probe("o2").get("web") is True

    monkeypatch.delenv("OR_WEB", raising=False)
    assert probe("o1").get("web") is False
    monkeypatch.setenv("OR_WEB", "1")
    assert probe("o2").get("web") is True
