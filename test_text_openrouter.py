# Тесты веб-флага у text_openrouter.generate (платформенный веб-поиск OpenRouter).
# Сеть не дёргаем: urlopen замокан, проверяем собираемый в запрос payload.

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


def _capture(fn, monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["payload"] = json.loads(req.data.decode("utf-8"))
        captured["headers"] = dict(req.headers)
        return FakeResp({"choices": [{"message": {"content": "ответ"}}]})

    monkeypatch.setattr(tor.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    result = fn()
    return result, captured


def test_web_flag_adds_web_plugin(monkeypatch):
    result, cap = _capture(lambda: tor.generate("p", model="m:free", web=True, report_model=True), monkeypatch)
    assert result == ("ответ", "m:free")
    assert cap["payload"]["plugins"] == [{"id": "web"}]
    assert cap["payload"]["model"] == "m:free"


def test_no_web_by_default(monkeypatch):
    result, cap = _capture(lambda: tor.generate("p", model="m:free"), monkeypatch)
    assert result == "ответ"
    assert "plugins" not in cap["payload"]