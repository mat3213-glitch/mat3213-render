# Тесты веб-флага и валидации url-навыка у text_openrouter.generate.
# Сеть не дёргаем: urlopen замокан, проверяем собираемый в запрос payload
# и поведение при отказе web-поля API.

import json
from unittest import mock

import text_openrouter as tor


class FakeResp:
    def __init__(self, data):
        self._data = data

    def read(self, *a):
        return json.dumps(self._data).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _install(monkeypatch, func):
    monkeypatch.setattr(tor.urllib.request, "urlopen", func)
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")


def _capture(fn, monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["calls"] = captured.get("calls", [])
        captured["calls"].append(json.loads(req.data.decode("utf-8")))
        captured["payload"] = captured["calls"][-1]
        captured["headers"] = dict(req.headers)
        return FakeResp({"choices": [{"message": {"content": "ответ"}}]})

    _install(monkeypatch, fake_urlopen)
    result = fn()
    return result, captured


def test_web_flag_adds_web_plugin(monkeypatch):
    result, cap = _capture(
        lambda: tor.generate("p", model="m:free", web=True, report_model=True), monkeypatch)
    assert result == ("ответ", "m:free")
    assert cap["payload"]["plugins"] == [{"id": "web"}]
    assert cap["payload"]["model"] == "m:free"


def test_no_web_by_default(monkeypatch):
    result, cap = _capture(lambda: tor.generate("p", model="m:free"), monkeypatch)
    assert result == "ответ"
    assert "plugins" not in cap["payload"]


def _capture_400_then_ok(fn, monkeypatch):
    captured = {"calls": []}

    def fake_urlopen(req, timeout=None):
        captured["calls"].append(json.loads(req.data.decode("utf-8")))
        if len(captured["calls"]) == 1:
            raise tor.urllib.error.HTTPError("u", 400, "Bad Request", None, None)
        return FakeResp({"choices": [{"message": {"content": "ответ"}}]})

    monkeypatch.setattr(tor.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    result = fn()
    return result, captured


def test_web_rejected_400_retries_same_model_without_web(monkeypatch):
    result, cap = _capture_400_then_ok(
        lambda: tor.generate("p", model="m:free", web=True, report_model=True), monkeypatch)
    # первый вызов — с web, повтор — без него, та же модель
    assert cap["calls"][0]["plugins"] == [{"id": "web"}]
    assert "plugins" not in cap["calls"][1]
    assert [c["model"] for c in cap["calls"]] == ["m:free", "m:free"]
    assert result == ("ответ", "m:free")


def test_web_rejected_400_then_model_down_falls_to_next(monkeypatch):
    captured = {"calls": []}

    def fake_urlopen(req, timeout=None):
        captured["calls"].append(json.loads(req.data.decode("utf-8")))
        if len(captured["calls"]) == 1:
            raise tor.urllib.error.HTTPError("u", 400, "Bad Request", None, None)
        raise tor.urllib.error.URLError("down")

    monkeypatch.setattr(tor.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    try:
        tor.generate("p", model=None, web=True, report_model=True)
    except RuntimeError as exc:
        assert "URLError" in str(exc)
    else:
        raise AssertionError("generate должен бросить RuntimeError")


def test_429_does_not_trigger_no_web_retry(monkeypatch):
    captured = {"calls": []}

    def fake_urlopen(req, timeout=None):
        captured["calls"].append(json.loads(req.data.decode("utf-8")))
        raise tor.urllib.error.HTTPError("u", 429, "Too Many Requests", None, None)

    monkeypatch.setattr(tor.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    try:
        tor.generate("p", model="m:free", web=True)
    except RuntimeError:
        pass
    else:
        raise AssertionError("generate должен бросить RuntimeError")
    # 429 — не поле, а квота: у цепочки было ровно одно представление на модель
    assert len(captured["calls"]) == 1
    assert captured["calls"][0]["plugins"] == [{"id": "web"}]


def test_402_metered_on_web_retries_same_model_without_web(monkeypatch):
    captured = {"calls": []}

    def fake_urlopen(req, timeout=None):
        captured["calls"].append(json.loads(req.data.decode("utf-8")))
        if len(captured["calls"]) == 1:
            raise tor.urllib.error.HTTPError("u", 402, "Payment Required", None, None)
        return FakeResp({"choices": [{"message": {"content": "ответ"}}]})

    monkeypatch.setattr(tor.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    result, cap = _capture(lambda: tor.generate("p", model="m:free", web=True, report_model=True), monkeypatch)
    assert cap["calls"][0]["plugins"] == [{"id": "web"}]
    assert "plugins" not in cap["calls"][1]
    assert [c["model"] for c in cap["calls"]] == ["m:free", "m:free"]
    assert result == ("ответ", "m:free")
