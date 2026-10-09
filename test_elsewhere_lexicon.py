"""Тесты словаря загадок (elsewhere_lexicon).

Главный тест здесь — `test_acts_have_no_explanation`. Он защищает ровно тот дефект,
который стоил 11 кадров из 12 на ране `37946794045`: acts, которые ОБЪЯСНЯЛИ
присутствие предмета (гнездо между изоляторами, тёплый инкубатор, иней). Как только
у предмета есть причина, загадка мертва — вопрос снят, и кадр читается как сюжет.
Причина — не украшение кадра, а то, что его убивает.

Остальные тесты закрывают:
  * дыру первой версии (недостижимые дома предметов — молчаливую потерю четверти
    словаря, выглядящую как «просто не выпало»);
  * требование «минимум три версии»: `reads` не просто написаны, а различаются;
  * форму промпта и канон;
  * боевой путь от env, потому что именно он, а не внутренняя функция, падал в бою
    (ран `37946396211`: KeyError 'surprises', 197 зелёных тестов мимо).
"""
from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import elsewhere_lexicon as E  # noqa: E402


# ════════════════════════════════════════════════════════════════════════════════
# ГЛАВНОЕ: в acts не должно быть причины
# ════════════════════════════════════════════════════════════════════════════════

def test_acts_have_no_explanation():
    """Ни одно слово-причина не допускается в acts.

    Проверено на словаре прошлой версии: 23 act'а содержали nest / moss / frost /
    rust / overgrown. Именно они превращали кадр в сюжет с причиной и убивали
    загадку. Список — в E.REASON_WORDS, дополнять его новыми находками.
    """
    offenders = []
    for ok, obj in E.OBJECTS.items():
        for i, act in enumerate(obj["acts"]):
            low = act.lower()
            hit = [w for w in E.REASON_WORDS if w in low]
            if hit:
                offenders.append(f"{ok}.acts[{i}]: {hit} → {act!r}")
    assert not offenders, (
        "acts объясняют присутствие предмета — загадка не возникнет:\n  "
        + "\n  ".join(offenders))


def test_acts_describe_state_not_history():
    """Каждый act обязан называть состояние или позу.

    Обратная сторона запрета причин: если убрать всё, что объясняет, act может
    выродиться в отписку («in a cinema hall»). Состояние — это то, что заставляет
    модель показать объект определённым образом: стоит, закрыто, наклонено, замерло.
    """
    for ok, obj in E.OBJECTS.items():
        for i, act in enumerate(obj["acts"]):
            low = act.lower()
            assert any(w in low for w in E.STATE_WORDS), (
                f"{ok}.acts[{i}]: нет ни позы, ни состояния — {act!r}")


def test_acts_are_idle_or_frozen_process():
    """Формулировка про замороженный процесс, а не про работу по назначению.

    Слова работы ('being used', 'in service', 'working') означают, что у предмета
    есть занятие, а значит и причина. Загадка требует простоя.
    """
    working = ("being used", "in service", "working normally", "on duty",
               "operating normally", "being worn", "in use", "being loaded",
               "being unloaded", "being washed", "being played")
    for ok, obj in E.OBJECTS.items():
        for i, act in enumerate(obj["acts"]):
            low = act.lower()
            hit = [w for w in working if w in low]
            assert not hit, f"{ok}.acts[{i}]: предмет ЗАНЯТ, а не в простое → {hit}: {act!r}"


# ════════════════════════════════════════════════════════════════════════════════
# «минимум три версии» — проверяемое требование
# ════════════════════════════════════════════════════════════════════════════════

def test_every_object_has_three_readings():
    """У каждого предмета минимум три прочтения — иначе загадки нет по определению."""
    for ok, obj in E.OBJECTS.items():
        assert len(obj.get("reads", [])) >= 3, (
            f"{ok}: {len(obj.get('reads', []))} прочтений, нужно минимум 3")


def test_readings_are_distinct():
    """Три прочтения должны быть разными гипотезами, а не перефразировкой.

    Если все три сводятся к «поставили и забыли», загадки нет: у зрителя одна
    версия. Проверяем попарно, что ни одно прочтение не содержит подстроку другого
    и что они не совпадают по первым словам.
    """
    for ok, obj in E.OBJECTS.items():
        reads = obj["reads"]
        for i, a in enumerate(reads):
            for b in reads[i + 1:]:
                na, nb = a.lower().strip(), b.lower().strip()
                assert na != nb, f"{ok}: прочтения {i} и {i+1} совпадают: {a!r}"
                assert na not in nb and nb not in na, (
                    f"{ok}: одно прочтение вложено в другое — это одна гипотеза: "
                    f"{a!r} / {b!r}")


def test_readings_are_about_the_object_not_the_place():
    """Прочтение должно объяснять ПРЕДМЕТ, а не локацию.

    Меняется то, как зритель читает кадр: «его поставили и забыли» — про предмет.
    «в пустом зале» — про место, и это описание сцены, а не версия.
    """
    # Только описания САМОЙ локации. «на месте зрителя» сюда НЕ входит: это
    # прочтение ПРО ПРЕДМЕТ — рохля заняла место человека, и оно самое точное
    # в пуле. Проверка отвергала именно его.
    place_words = ("пустой зал", "пустое помещение", "само место",
                   "в зале", "в комнате", "сама локация")
    for ok, obj in E.OBJECTS.items():
        for r in obj["reads"]:
            low = r.lower()
            hit = [w for w in place_words if w in low]
            assert not hit, f"{ok}: прочтение описывает место, а не предмет: {r!r}"


# ════════════════════════════════════════════════════════════════════════════════
# типизация: молчаливые потери (реальный баг первой версии)
# ════════════════════════════════════════════════════════════════════════════════

def test_every_object_home_is_reachable():
    """Ни один дом предметов не должен выпадать из ВСЕХ локаций сразу."""
    for bank in ("elsewhere", "atmosphere"):
        reachable = {E.OBJECTS[ok]["home"] for lk in E.locations_for(bank)
                     for _, ok in E.pairs_for(lk, bank)}
        unreachable = {o["home"] for o in E.OBJECTS.values()} - reachable
        assert not unreachable, f"[{bank}] недостижимые дома: {sorted(unreachable)}"


def test_excludes_names_are_real_homes():
    """Опечатка в excludes молча разрешает лишнее."""
    homes = {o["home"] for o in E.OBJECTS.values()}
    for bank in ("elsewhere", "atmosphere"):
        for k, loc in E.locations_for(bank).items():
            for h in loc["excludes"]:
                assert h in homes, f"[{bank}] {k}: исключён несуществующий дом '{h}'"


def test_home_of_object_is_never_excluded_in_pair():
    for bank in ("elsewhere", "atmosphere"):
        for lk, ok in E.all_pairs(bank):
            assert E.OBJECTS[ok]["home"] not in E.locations_for(bank)[lk]["excludes"]


def test_every_location_has_enough_pairs():
    """Мало пар — прогон исчерпает локацию за один заход."""
    for bank in ("elsewhere", "atmosphere"):
        for k in E.locations_for(bank):
            n = len(E.pairs_for(k, bank))
            assert n >= 20, f"[{bank}] {k}: всего {n} пар"


# ════════════════════════════════════════════════════════════════════════════════
# форма промпта и канон
# ════════════════════════════════════════════════════════════════════════════════

def test_prompt_separator_between_phrase_and_act():
    """Регрессия склейки: перед act обязан стоять запятая (было «keys exposed tipped»)."""
    for prompt, meta in zip(*E.prompts(40, seed=7)):
        phrase = E.OBJECTS[meta["object"]]["phrase"]
        assert phrase in prompt, f"{meta['object']}: phrase не попал в промпт"
        idx = prompt.index(phrase) + len(phrase)
        assert prompt[idx:idx + 2] == ", ", f"{meta['location']}: нет разделителя"


def test_prompt_shape_and_canon():
    """Канон в каждом промпте: объект первым, свет, запрет текста."""
    for prompt, _meta in zip(*E.prompts(30, seed=3)):
        low = prompt.lower()
        assert low.startswith("close on ")
        assert "no people" in low
        assert "no text" in low and "nothing written anywhere in frame" in low
        assert "the object large in frame" in low
        head = low.split(", the object large in frame")[0]
        for word in ("neon", "sign", "lettering", "graffiti", "logo"):
            assert word not in head, f"{word!r} просочился в описание сцены"


def test_lights_have_no_neon():
    for bank in ("elsewhere", "atmosphere"):
        for k, loc in E.locations_for(bank).items():
            assert "neon" not in loc["light"].lower(), k


def test_acts_have_no_trailing_period_and_start_lowercase():
    """Хвостовые точки ломают склейку; заглавная — читается как новое предложение."""
    for ok, obj in E.OBJECTS.items():
        assert not obj["phrase"].rstrip().endswith((".", ",", ";")), ok
        for i, act in enumerate(obj["acts"]):
            assert not act.strip().endswith("."), f"{ok}.acts[{i}] кончается точкой"
            assert act.strip()[0].islower(), f"{ok}.acts[{i}] с заглавной"


def test_acts_do_not_start_with_conjunction_glue():
    """«and its fill spilled out» склеивается с предыдущей фразой."""
    bad = re.compile(r"^(and|or|but|so|then|which|because)\b", re.I)
    for ok, obj in E.OBJECTS.items():
        for i, act in enumerate(obj["acts"]):
            assert not bad.match(act.strip()), f"{ok}.acts[{i}]: {act!r}"


def test_phrases_carry_no_state():
    """phrase = ЧТО ЭТО. Состояние живёт в acts, иначе act вырождается в пересказ.

    Живой случай: phrase «a laboratory freezer, sealed door» + act «the thermometer
    gone and the dial still set» — act повторял и не добавлял ничего.
    """
    # «full-size pool table» — ТИП стола, а не состояние; «upright piano» — тип
    # инструмента. Такие слова проверяем по границам слов, а не подстрокой,
    # иначе тест ругается на названия типов, а не на состояние.
    state_in_phrase = ("open", "closed", "sealed", "empty", "full", "gone",
                       "broken", "collapsed", "upright", "on its side")
    type_words = ("full-size", "upright piano")
    def has(text, word):
        return re.search(rf"(?<!\w){re.escape(word)}\b", text.lower()) is not None

    for ok, obj in E.OBJECTS.items():
        low = obj["phrase"].lower()
        if any(tw in low for tw in type_words):
            continue
        hit = [w for w in state_in_phrase if has(low, w)]
        assert not hit, f"{ok}: состояние {hit} должно быть в acts, не в phrase"


# ════════════════════════════════════════════════════════════════════════════════
# выборка
# ════════════════════════════════════════════════════════════════════════════════

def test_prompts_are_deterministic_for_seed():
    a, ma = E.prompts(12, seed=42)
    b, mb = E.prompts(12, seed=42)
    assert a == b and ma == mb
    c, _ = E.prompts(12, seed=43)
    assert a != c, "разные seed дали одинаковую выборку"


def test_prompts_never_repeat_within_a_run():
    prompts, meta = E.prompts(24, seed=11)
    assert len(set(prompts)) == len(prompts)
    assert len({(m["location"], m["object"], m["act"]) for m in meta}) == len(meta)


def test_run_covers_distinct_locations():
    prompts, meta = E.prompts(8, seed=5)
    assert len({m["location"] for m in meta}) == min(8, len(E.RIDDLE_LOCATIONS))


def test_run_has_no_repeated_objects():
    """Предмет в прогоне не повторяется.

    Дважды этот класс прошёл незамеченным и оба раза съел прогон целиком:
    на seed 910 из 12 задач четыре были одним и тем же cloakroom rack, а после
    первой починки — все 12 оказались в одной локации. Тест на покрытие ЛОКАЦИЙ
    в обоих случаях был зелёный: локации различались, вырождались предметы.
    Проверяем на многих seed'ах, потому что баг был seed-зависимым.
    """
    for seed in (910, 42, 7, 99, 1234, 555, 3, 88, 2024):
        prompts, meta = E.prompts(12, seed=seed)
        objects = [m["object"] for m in meta]
        assert len(set(objects)) == len(objects), (
            f"seed {seed}: предмет повторился — {objects}")
        assert len(prompts) == 12


def test_run_has_no_repeated_locations():
    """Симметрично: локации тоже не должны слипаться в прогоне."""
    for seed in (910, 42, 7, 99, 1234, 555, 3, 88, 2024):
        _prompts, meta = E.prompts(12, seed=seed)
        locs = [m["location"] for m in meta]
        assert len(set(locs)) == len(locs), f"seed {seed}: локация повторилась — {locs}"


def test_count_exceeding_available_objects_still_works():
    """Если предметов меньше, чем count, прогон должен добрать, а не зациклиться."""
    n = len(E.OBJECTS)
    prompts, meta = E.prompts(n + 3, seed=11)
    assert len(prompts) == n + 3
    triples = {(m["location"], m["object"], m["act"]) for m in meta}
    assert len(triples) == len(meta), "добранные тройки должны быть уникальны"


def test_single_location_filter():
    prompts, meta = E.prompts(6, loc="cinema_hall", seed=2)
    assert len(prompts) == 6
    assert {m["location"] for m in meta} == {"cinema_hall"}


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


def test_meta_matches_prompt_and_carries_readings():
    """meta — не декорация: act стоит в промпте, а reads приезжают в manifest."""
    prompts, meta = E.prompts(20, seed=99)
    for prompt, m in zip(prompts, meta):
        assert E.OBJECTS[m["object"]]["acts"][m["act_index"]] in prompt
        assert E.locations_for(m["bank"])[m["location"]]["where"] in prompt
        assert E.locations_for(m["bank"])[m["location"]]["light"] in prompt
        assert len(m["reads"]) >= 3


# ════════════════════════════════════════════════════════════════════════════════
# два банка
# ════════════════════════════════════════════════════════════════════════════════

def test_riddle_and_atmosphere_banks_are_disjoint():
    """Экзотика не должна просачиваться в банк загадок — иначе 12 кадров снова
    придут атмосферными, и это будет выглядеть как «словарь не сработал»."""
    riddle = set(E.RIDDLE_LOCATIONS)
    atmo = set(E.ATMOSPHERE_LOCATIONS)
    assert not riddle & atmo, f"локация в обоих банках: {sorted(riddle & atmo)}"


def test_riddle_locations_are_boring():
    """Уровень скуки объявлен для каждой локации-з��гадки: 1-3, и это осознанно
    скучно. Ровно то, что нужно: на скучном месте предмет выделяется."""
    for k, loc in E.RIDDLE_LOCATIONS.items():
        assert loc.get("quiet") in (1, 2, 3), f"{k}: нет уровня скуки"


def test_riddle_locations_exclude_their_own_kind():
    """Мягкое напоминание: локация не должна исключать сама себя по дому, который
    в ней уместен. Проверяем, что в каждой локации есть хоть одна «своя» пара."""
    for k in E.RIDDLE_LOCATIONS:
        pairs = E.pairs_for(k)
        assert pairs, f"{k}: ни одной пары — локация исключает всё"


def test_atmosphere_bank_is_not_marked_as_riddle():
    """Экзотика живёт отдельно и не должна выдаваться за находку."""
    assert E.locations_for("atmosphere") is E.ATMOSPHERE_LOCATIONS
    assert E.locations_for("elsewhere") is E.RIDDLE_LOCATIONS


def test_lexicon_volume_is_usable():
    assert len(E.OBJECTS) >= 50
    assert len(E.RIDDLE_LOCATIONS) >= 12
    triples = sum(len(E.OBJECTS[ok]["acts"]) for _lk, ok in E.all_pairs("elsewhere"))
    assert len(E.all_pairs("elsewhere")) >= 500
    assert triples >= 1500, f"всего троек {triples}"


def test_object_home_coverage_balanced():
    c = Counter(o["home"] for o in E.OBJECTS.values())
    thin = {h: n for h, n in c.items() if n <= 1}
    assert not thin, f"дома с одним предметом: {thin}"


# ════════════════════════════════════════════════════════════════════════════════
# боевой путь (ран 37946396211: 197 зелёных тестов мимо живого падения)
# ════════════════════════════════════════════════════════════════════════════════

def test_resolve_prompts_elsewhere_runs_end_to_end(monkeypatch):
    """Полный путь боя: env → resolve_prompts → промпты.

    Живой баг: после выноса словаря в resolve_prompts осталась строка, читавшая
    LOCATIONS[k]["surprises"], и КАЖДЫЙ прогон bank=elsewhere падал с KeyError.
    Локальные проверки шли через elsewhere_prompts() напрямую и были зелёными.
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
    import imagefree_pool_job as J

    monkeypatch.setenv("PROMPTS", "")
    monkeypatch.setenv("BANK", "elsewhere")
    monkeypatch.setenv("ELSEWHERE_LOCATION", "cinema_hall")
    monkeypatch.setenv("ELSEWHERE_COUNT", "4")

    prompts = J.resolve_prompts("elsewhere")
    assert len(prompts) == 4
    for p in prompts:
        assert "cinema" in p.lower()


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


def test_no_stale_surprises_key_access():
    """Ключ 'surprises' удалён вместе со старым словарём — ссылка на него в коде
    означает остаток, который упадёт в бою, а не в тестах."""
    job = (Path(__file__).resolve().parent / "imagefree_pool_job.py").read_text(
        encoding="utf-8")
    code = "\n".join(l.split("#", 1)[0] for l in job.splitlines())
    assert '"surprises"' not in code, (
        "imagefree_pool_job.py всё ещё обращается к LOCATIONS[k]['surprises']")
    assert "'surprises'" not in code


def test_job_exposes_both_banks_not_removed_names():
    """Job должен импортировать оба банка и НЕ отдавать единый LOCATIONS.

    Единый LOCATIONS был источником главной ошибки 08–09.10: экзотика и загадки
    лежали в одной куче, поэтому прогон «загадок» наполовину состоял из Луны и
    солончака — мест, где загадки не бывает по построению.
    """
    import imagefree_pool_job as J
    assert hasattr(J, "RIDDLE_LOCATIONS"), "job должен импортировать RIDDLE_LOCATIONS"
    assert hasattr(J, "ATMOSPHERE_LOCATIONS"), "job должен импортировать ATMOSPHERE_LOCATIONS"
    assert not hasattr(J, "LOCATIONS"), (
        "единый LOCATIONS больше не существует: два банка должны быть разведены")
