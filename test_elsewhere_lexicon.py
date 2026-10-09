"""Тесты словаря неожиданных сочетаний (elsewhere_lexicon).

Что тут защищается, по факту двух живых багов, а не абстрактных инвариантов:

1. ДЕСТРУКТУРНЫЙ ПРОПУСК. Первая версия словаря объявляла для локации список
   `allows` — список разрешённых домов предметов. Списки были написаны руками в
   20 локациях, и четыре дома (`civic`, `medical`, `grand`, `aquatic`) не попали
   НИ В ОДИН из них. Четверть словаря была недостижима, при этом пул продолжал
   генерироваться и выглядел健康 — отсутствие неотличимо от «не выпало».
   Теперь разрешение считается из короткого `excludes`, и тест ниже падает, если
   какой-то дом недостижим хоть где-то.

2. СКЛЕЙКА БЕЗ ЗАПЯТОЙ. Промпт собирался как `close on {phrase} {act}`, где и
   phrase, и act заканчиваются без знака препинания: «…keys exposed tipped forward
   onto its front legs». Модель читала это как один слипшийся поток. Сейчас
   `phrase, act`, и тест ловит возврат к склейке.
"""
from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import elsewhere_lexicon as E  # noqa: E402


def test_every_object_home_is_reachable():
    """Ни один дом предметов не должен выпадать из ВСЕХ локаций сразу."""
    reachable = {E.OBJECTS[ok]["home"] for k in E.LOCATIONS for _, ok in E.pairs_for(k)}
    unreachable = set(o["home"] for o in E.OBJECTS.values()) - reachable
    assert not unreachable, f"недостижимые дома предметов: {sorted(unreachable)}"


def test_every_location_has_enough_pairs():
    """Локация с парой десятков пар выедает себя за один прогон — это не запас."""
    for k in E.LOCATIONS:
        n = len(E.pairs_for(k))
        assert n >= 40, f"{k}: всего {n} пар, прогон исчерпает локацию за один заход"


def test_excludes_names_are_real_homes():
    """Опечатка в excludes молча разрешает лишнее — ловим по списку домов."""
    homes = {o["home"] for o in E.OBJECTS.values()}
    for k, loc in E.LOCATIONS.items():
        for h in loc["excludes"]:
            assert h in homes, f"{k}: исключён несуществующий дом '{h}'"


def test_home_of_object_is_never_excluded_in_pair():
    """Инвариант самого фильтра: в выданной паре дом не в excludes."""
    for lk, ok in E.all_pairs():
        assert E.OBJECTS[ok]["home"] not in E.LOCATIONS[lk]["excludes"]


def test_prompt_separator_between_phrase_and_act():
    """Защита от регрессии склейки: перед act обязан стоять запятая."""
    for prompt, (lk, ok, _act, _ai) in zip(
        E.prompts(40, seed=7)[0], E.prompts(40, seed=7)[1]
    ):
        phrase = E.OBJECTS[ok]["phrase"]
        assert phrase in prompt, f"{ok}: phrase не попал в промпт"
        idx = prompt.index(phrase) + len(phrase)
        assert prompt[idx:idx + 2] == ", ", f"{lk}/{ok}: нет разделителя после phrase"


def test_prompt_shape_and_canon():
    """Канон в каждом промпте: объект первым, свет, запрет текста и неона."""
    forbidden = ("neon", "sign", "lettering")
    for prompt, _meta in zip(E.prompts(30, seed=3)[0], E.prompts(30, seed=3)[1]):
        low = prompt.lower()
        assert low.startswith("close on ")
        assert "no people" in low
        assert "no text" in low and "nothing written anywhere in frame" in low
        assert "the object large in frame" in low
        # «no signs» в хвосте — это запрет, а не присутствие вывески
        head = low.split(", the object large in frame")[0]
        for word in forbidden:
            assert word not in head, f"{word!r} просочился в описание сцены"
        # нигде не обещан неон
        assert "neon" not in low or "no neon" in low or "no signs" in low


def test_prompts_are_deterministic_for_seed():
    a, ma = E.prompts(12, seed=42)
    b, mb = E.prompts(12, seed=42)
    assert a == b and ma == mb
    c, _ = E.prompts(12, seed=43)
    assert a != c, "разные seed дали одинаковую выборку"


def test_prompts_never_repeat_within_a_run():
    """Два одинаковых промпта в одном прогоне — потраченная генерация."""
    prompts, meta = E.prompts(24, seed=11)
    assert len(set(prompts)) == len(prompts)
    assert len({(l, o, a) for l, o, a, _ in meta}) == len(meta)


def test_run_covers_distinct_locations():
    """Иначе 8 задач съедают одну локацию (дефект старого round-robin)."""
    prompts, meta = E.prompts(8, seed=5)
    assert len({l for l, _o, _a, _i in meta}) == 8


def test_single_location_filter():
    prompts, meta = E.prompts(6, loc="forest", seed=2)
    assert len(prompts) == 6
    assert {l for l, _o, _a, _i in meta} == {"forest"}


def test_unknown_location_raises():
    try:
        E.prompts(4, loc="atlantis")
    except ValueError as exc:
        assert "неизвестная локация" in str(exc)
    else:
        raise AssertionError("неизвестная локация должна падать, а не молчать")


def test_count_zero_and_negative():
    assert E.prompts(0) == ([], [])
    assert E.prompts(-5) == ([], [])


def test_meta_matches_prompt_content():
    """meta — не декорация: act должен реально стоять в промпте."""
    prompts, meta = E.prompts(20, seed=99)
    for prompt, (lk, ok, act_norm, ai) in zip(prompts, meta):
        assert E.OBJECTS[ok]["acts"][ai].strip().lower().startswith(act_norm[:40])
        assert E.LOCATIONS[lk]["where"] in prompt
        assert E.LOCATIONS[lk]["light"] in prompt


def test_phrases_have_no_trailing_punctuation():
    """Хвост склейки: если phrase кончается точкой, запятая-следом ломает кадр."""
    for ok, obj in E.OBJECTS.items():
        assert not obj["phrase"].rstrip().endswith((".", ",", ";")), ok
        for i, act in enumerate(obj["acts"]):
            assert not act.strip().endswith("."), f"{ok}.acts[{i}] кончается точкой"
            assert act.strip()[0].islower(), f"{ok}.acts[{i}] должен начинаться со строчной"


def test_acts_do_not_start_with_conjunction_glue():
    """Хвосты вида 'and its fill spilled out' склеиваются с предыдущей фразой.

    `with` исключён намеренно: «close on a shelf, with one shelf of identical
    tins» — корректная конструкция, запятая перед ним уже стоит. Клеят только
    союзы-континуисты, которые продолжают предыдущую мысль, а не начинают новую.
    """
    bad = re.compile(r"^(and|or|but|so|then|while|which|because)\b", re.I)
    for ok, obj in E.OBJECTS.items():
        for i, act in enumerate(obj["acts"]):
            assert not bad.match(act.strip()), f"{ok}.acts[{i}]: {act!r}"


def test_every_object_has_min_three_acts():
    """Один action = одна идея на 1768 пар; прогон на 8 берёт 8 разных."""
    for ok, obj in E.OBJECTS.items():
        assert len(obj["acts"]) >= 3, f"{ok}: только {len(obj['acts'])} действий"


def test_lights_have_no_neon():
    """MEMORY_CORE: неон запрещён. Свет — только честный."""
    for k, loc in E.LOCATIONS.items():
        assert "neon" not in loc["light"].lower(), k


def test_acts_add_information_beyond_phrase():
    """Act обязан говорить то, чего в phrase ещё нет.

    Промпт склеивается как «close on {phrase}, {act}». Если act пересказывает
    phrase («with a raised divider bar, with the divider bar raised»), модель
    получает удвоение вместо нового слоя, а слот действия — потрачен впустую.
    Требуем не полного непересечения (повтор существительного нормален:
    «полная ванна, наполненная стоячей водой»), а заметной новизны: act не должен
    целиком состоять из слов phrase. Порог 3, а не 4: act'ы по определению
    короткие, и три новых слова («up», «fastened», «inside») — это ровно тот слой
    состояния, ради которого ось действий и вводилась. Цифра проверялась
    перебором всего словаря: при 4 падали 14 корректных act'ов.
    """
    content = lambda t: {w for w in re.findall(r"[a-z']+", t.lower())
                         if len(w) > 2 and w not in {"the", "and", "with", "its", "one", "two", "still", "from", "into", "that", "this"}}
    for ok, obj in E.OBJECTS.items():
        pw = content(obj["phrase"])
        for i, act in enumerate(obj["acts"]):
            novel = content(act) - pw
            assert len(novel) >= 3, (
                f"{ok}.acts[{i}]: почти весь текст уже есть в phrase.\n"
                f"    phrase={obj['phrase']}\n"
                f"    act={act}")


def test_no_phrase_contradicts_its_act():
    """Грубый ловец прямых противоречий состояния внутри одного промпта.

    Реальный случай из живой правки: phrase у drumkit говорил «hardware
    collapsed», а act — «upright, assembled and played». Оба утверждения не
    могут быть верны, и модель разрешает противоречие как угодно.
    Отрицание в любой части снимает проверку: «mattress gone» + «the mattress
    rotted to nothing» — это не противоречие, а усиление.
    """
    pairs_of_state = [
        ("collapsed", ("upright", "assembled", "erect", "standing")),
        ("open", ("closed", "sealed", "shut")),
        ("empty", ("full",)),
        ("full", ("empty",)),
        ("intact", ("broken", "rotted", "cracked", "collapsed", "gone")),
        ("new", ("old", "worn", "rusted")),
        ("upright", ("on its side", "face-down", "tipping", "tipped")),
        # «split along the back» + «whole and closed» — из живого dry-run 910.
        # Оба утверждения не могут быть верны, а тест молчал: «split» просто
        # не было в списке, и словарь пополнялся мимо защиты.
        ("split", ("whole", "closed", "intact")),
    ]
    # «gone» в пару НЕ входит: у shower_stall phrase «doors gone», а act «the
    # tiles intact» — это разные части предмета, не противоречие. Слово «gone»
    # без объекта слишком широкое, чтобы быть признаком состояния.

    def has_phrase(text, word):
        # Именно границы слова. Наивный `word in text` ловил «collapsed» внутри
        # «encrusted»-подобных слов и, что хуже, матчил single-char подстроки —
        # из-за чего тест сначала ругался на невинные act'ы.
        return re.search(rf"\b{re.escape(word)}\b", text.lower()) is not None

    # «upright piano» и «a full drum kit» — часть ТИПА, а не состояние кадра:
    # «an upright piano» не противоречит «tipped forward», а «full kit» —
    # «in an empty room». Тут речь о разных объектах в одной фразе.
    type_words = ("upright piano", "grand piano", "full drum kit")

    for ok, obj in E.OBJECTS.items():
        phrase = obj["phrase"]
        for i, act in enumerate(obj["acts"]):
            if " not " in act or "n't " in act:
                continue
            for pw, aws in pairs_of_state:
                if any(t in phrase.lower() for t in type_words):
                    continue
                if not any(has_phrase(phrase, w) for w in pw.split()):
                    continue
                hit = [w for w in aws if has_phrase(act, w)]
                assert not hit, (
                    f"{ok}.acts[{i}]: phrase утверждает {pw!r}, act утверждает {hit}.\n"
                    f"    phrase={phrase}\n"
                    f"    act={act}")

def test_resolve_prompts_elsewhere_runs_end_to_end(monkeypatch):
    """Полный путь боя: env → resolve_prompts("elsewhere") → промпты.

    Живой баг (ран 37946396211): после выноса словаря в elsewhere_lexicon.py в
    `resolve_prompts` осталась старая строка печати статистики, которая
    обращалась к LOCATIONS[k]["surprises"]. Локальные проверки шли через
    elsewhere_prompts() напрямую и были зелёными, а боевой путь через
    resolve_prompts падал на КАЖДЫЙ прогон с KeyError: 'surprises'.

    Тест идёт ровно тем путём, каким идёт воркфлоу, поэтому такой остаток
    ловится до рана, а не на нём.
    """
    import imagefree_pool_job as J

    monkeypatch.setenv("PROMPTS", "")
    monkeypatch.setenv("BANK", "elsewhere")
    monkeypatch.setenv("ELSEWHERE_LOCATION", "")
    monkeypatch.setenv("ELSEWHERE_COUNT", "6")
    monkeypatch.setenv("ELSEWHERE_SEED", "910")

    prompts = J.resolve_prompts("elsewhere")
    assert len(prompts) == 6
    for p in prompts:
        assert p.startswith("close on ")
        assert "the object large in frame" in p


def test_resolve_prompts_elsewhere_single_location(monkeypatch):
    """Тот же путь с фильтром локации — иначе опечатка в переменной не видна."""
    import imagefree_pool_job as J

    monkeypatch.setenv("PROMPTS", "")
    monkeypatch.setenv("BANK", "elsewhere")
    monkeypatch.setenv("ELSEWHERE_LOCATION", "forest")
    monkeypatch.setenv("ELSEWHERE_COUNT", "4")

    prompts = J.resolve_prompts("elsewhere")
    assert len(prompts) == 4
    for p in prompts:
        assert "dark coniferous forest" in p


def test_resolve_prompts_rejects_bad_location(monkeypatch):
    """Опечатка в ELSEWHERE_LOCATION обязана падать, а не молча брать все локации."""
    import imagefree_pool_job as J

    monkeypatch.setenv("PROMPTS", "")
    monkeypatch.setenv("BANK", "elsewhere")
    monkeypatch.setenv("ELSEWHERE_LOCATION", "atlantis")
    monkeypatch.setenv("ELSEWHERE_COUNT", "4")

    try:
        J.resolve_prompts("elsewhere")
    except SystemExit as exc:
        assert "atlantis" in str(exc)
    else:
        raise AssertionError("неизвестная локация должна останавливать прогон")


def test_no_stale_surprises_key_access(monkeypatch):
    """Ключа 'surprises' в новой схеме нет — любая ссылка на него в job-скрипте
    означает остаток старого кода, который упадёт в бою, а не в тестах."""
    job = (Path(__file__).resolve().parent / "imagefree_pool_job.py").read_text(
        encoding="utf-8")
    # Комментарии пропускаем: в докстринге над этим тестом ключ упоминается
    # намеренно, чтобы зафиксировать, что именно его и ловим.
    code = "\n".join(l.split("#", 1)[0] for l in job.splitlines())
    assert '"surprises"' not in code, (
        "imagefree_pool_job.py всё ещё обращается к LOCATIONS[k]['surprises'] — "
        "ключ удалён вместе со старым словарём")
    assert "'surprises'" not in code


def test_lexicon_volume_is_usable():
    """Объём словаря: прогон должен упираться в MAX_TASKS, а не в словарь."""
    assert len(E.OBJECTS) >= 60
    assert len(E.LOCATIONS) >= 12
    # Тройка = (локация, предмет, действие) и разрешена только если дом предмета
    # не в excludes локации. Считаем по парам, а не суммой acts: сумма acts —
    # это словарь предметов, а не пространство прогонов.
    triples = sum(len(E.OBJECTS[ok]["acts"]) for _lk, ok in E.all_pairs())
    assert len(E.all_pairs()) >= 500
    assert triples >= 3000, f"всего разрешённых троек {triples}"


def test_object_home_coverage_balanced():
    """Словарь не должен состоять из одних стульев: дома с 1-2 предметами — сигнал."""
    c = Counter(o["home"] for o in E.OBJECTS.values())
    thin = {h: n for h, n in c.items() if n <= 2}
    assert not thin, f"слишком тонкие дома: {thin}"
