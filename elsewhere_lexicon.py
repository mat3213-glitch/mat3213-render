"""elsewhere_lexicon.py — словарь ЗАГАДОК «локация × предмет × состояние».

═══════════════════════════════════════════════════════════════════════════════
Что здесь и почему оно устроено именно так
═══════════════════════════════════════════════════════════════════════════════

Предыдущая версия словаря (12 локаций × 6 предметов, без действий) дала на ране
`37756755318` один точный кадр из восьми: «тележка супермаркета в лесу». На ране
`37946794045` (12 кадров по этому же словарю) — снова один: рохля в пустом
кинозале. Остальные одиннадцать yaromat отсёк как «атмосферу, не загадку».

Разбор, почему одиннадцать мимо, и он переписан в код:

**1. ДЕЙСТВИЕ-ОБЪЯСНЕНИЕ УБИВАЛО ЗАГАДКУ.** Старые `acts` объясняли аномалию:
подстанция + «гнездо между изоляторами», инкубатор + «всё ещё тёплый, стекло
запотело». Модель рисовала кадр СО СЮЖЕТОМ: обслуживание, эксперимент. Как
только у присутствия предмета есть причина, вопрос снят — а загадка и есть
отсутствие ответа. Правило теперь: `acts` описывает ПОЗУ и СОСТОЯНИЕ
(стоит / лежит / закрыто / наклонено / замерло в процессе) и НЕ содержит ничего,
что отвечает на вопрос «а почему оно тут».

Это проверяется, а не обещается: `test_acts_have_no_explanation` держит чёрный
список причин-слов (nest, moss, frost, rust, abandoned, evidence, because…).

**2. ЛОКАЦИЯ ДОЛЖНА БЫТЬ СКУЧНОЙ.** Луна, дно океана, солончак, ледник —
это «атмосферные» места, и там ЛЮБОМУ предмету выдаётся объяснение
(научная станция, эксперимент, объект рендера). Противоречия нет — нет и
загадки. Рохля в кинозале работает потому, что зал — место с очевидной бытовой
функцией, и рохля в нём не имеет НИ ОДНОГО применения.

Экзотика не удалена (жалко прогонов), а вынесена в отдельный банк
`ATMOSPHERE_LOCATIONS` / `bank=atmosphere` с честной пометкой «не загадка»,
чтобы её можно было брать под фоны, но никогда не выдавать за находку.

**3. ПРЕДМЕТ ПРИНОСИТ СВОЙ ДОМ, И ДОМ ЖЁСТКО ИСКЛЮЧАЕТ ЛОКАЦИЮ.** Это из
прошлой версии, работает и осталось: тележка — это супермаркет, рохля — это
склады и фуры. `excludes` у локации перечисляет ДОМА предметов, которые тут
у себя дома. Короткий список, а не список разрешённых: в первой версии
разрешённые списки молча выкинули четыре дома (`civic`, `medical`, `grand`,
`aquatic`), и четверть словаря была недостижима — это поймал
`test_every_object_home_is_reachable`.

**4. ЗАГАДКУ НУЖНО ПРОВЕРЯТЬ, А НЕ ОБЕЩАТЬ.** Для каждого предмета написаны
`reads` — три и более прочтения, которые кадр обязан допускать
(у рохли: ждёт погрузки / стоит вместо зрителя / её забыли). Тест
`test_every_object_has_three_readings` требует минимум три, а
`test_readings_are_distinct` — чтобы они не были перефразировкой друг друга.

Канон (MEMORY_CORE) неизменен: без лиц, без текста в кадре, без неона, свет
только честный.
"""

from __future__ import annotations

from datetime import date

# ── чёрный список причин ──────────────────────────────────────────────────────
# Слова, которые ОБЪЯСНЯЮТ присутствие предмета и потому убивают загадку.
# Проверяется тестом против acts. Это ядро всей правки: 11 из 12 кадров
# прошлого прогона были с этим дефектом.
REASON_WORDS = (
    "nest", "nests", "moss", "frost", "snow", "ice", "rust", "rusted", "stain",
    "stained", "decayed", "rotted", "overgrown", "grown over", "cobweb",
    "abandoned", "forgotten", "evidence", "trace", "traces", "mark of",
    "because", "weathered", "eaten", "eroded", "greened", "colonised",
    "colonized", "inhabited", "resident", "evidence of",
)

# ── состояние: поза или замороженный процесс ──────────────────────────────────
# Слова, которые говорят «объект пребывает», а не «объект к чему-то пришёл».
STATE_WORDS = (
    # поза
    "upright", "square", "level", "centred", "centered", "standing", "lying",
    "hanging", "leaning", "tilted", "propped", "stacked", "hung", "sitting",
    "resting", "flat", "proud", "sunk", "pushed", "set down", "turned",
    "facing", "tucked", "hooked", "mounted", "parked", "strung",
    "centre", "against", "straight", "clear",
    # состояние
    "closed", "open", "ajar", "shut", "folded", "collapsed", "half", "empty",
    "still", "balanced", "aligned", "latched", "braked", "chocked", "slack",
    "dark", "unlit", "off", "cold", "unplugged", "docked", "made up",
    "plumped", "racked", "swung",
    # замороженный процесс
    "mid", "caught", "held", "frozen", "suspended", "stopped", "waited",
    "settling", "falling", "part-open",
)


# ════════════════════════════════════════════════════════════════════════════════
# ЛОКАЦИИ-ЗАГАДКИ: скучные, с очевидной бытовой функцией
# ════════════════════════════════════════════════════════════════════════════════
# excludes — дома предметов, которые здесь У СЕБЯ ДОМА. Предмет сюда попадает,
#            только если его дом не в списке.
# quiet    — «уровень скуки»: 1 = приземлённый интерьер, 2 = улица без события,
#            3 = дикая природа. Всё это скучно. Ровно то, что нужно.
RIDDLE_LOCATIONS: dict[str, dict] = {
    "cinema_hall": {
        "where": "an empty old cinema hall after the last screening, rows of seats, bare screen",
        "light": "a few working house lights along the side wall, dim",
        "excludes": ["civic", "transit"],
        "quiet": 1,
    },
    "canteen": {
        "where": "a works canteen, long tables and benches, serving hatch closed",
        "light": "strip lights overhead, one of them flickering",
        "excludes": ["domestic", "hospitality"],
        "quiet": 1,
    },
    "cloakroom": {
        "where": "a sports centre cloakroom, numbered pegs along the wall, benches",
        "light": "one fluorescent tube working, the rest dark",
        "excludes": ["sports", "hospitality"],
        "quiet": 1,
    },
    "stairwell": {
        "where": "a concrete apartment stairwell, metal railings, mailboxes",
        "light": "a bulb on each landing, one landing dark",
        "excludes": ["domestic"],
        "quiet": 1,
    },
    "laundry": {
        "where": "a coin laundry, rows of machines, plastic folding tables",
        "light": "even fluorescent light, nothing dramatic about it",
        "excludes": ["domestic", "sanitary"],
        "quiet": 1,
    },
    "carpark": {
        "where": "an underground car park, numbered bays, painted arrows on the floor",
        "light": "sodium strip lights, low and even",
        "excludes": ["transit"],
        "quiet": 1,
    },
    "office_night": {
        "where": "an open-plan office at night, desks in rows, monitors dark",
        "light": "a few desk lamps left on, the rest of the floor dark",
        "excludes": ["civic"],
        "quiet": 1,
    },
    "gym_floor": {
        "where": "a gym floor, machines in rows, mirrors along one wall",
        "light": "flat overhead panel light, no drama",
        "excludes": ["sports", "medical"],
        "quiet": 1,
    },
    "waiting_room": {
        "where": "a station waiting room, plastic seats bolted in rows, a closed window",
        "light": "cold light through the window and one wall lamp",
        "excludes": ["transit", "civic"],
        "quiet": 1,
    },
    "classroom": {
        "where": "an empty classroom, desks in rows, a green board at the front",
        "light": "north-facing windows, flat daylight, nothing harsh",
        "excludes": ["civic", "institutional"],
        "quiet": 1,
    },
    "workshop": {
        "where": "a small workshop, bench along one wall, tools on a board",
        "light": "one bare bulb over the bench, the rest dim",
        "excludes": ["industrial"],
        "quiet": 1,
    },
    "kitchen_flat": {
        "where": "a plain kitchen, worktop, sink, a window onto a wall",
        "light": "daylight from the window, one under-cupboard lamp off",
        "excludes": ["domestic", "hospitality"],
        "quiet": 1,
    },
    "corridor_flat": {
        "where": "a plain residential corridor, doors along one side, no windows",
        "light": "one ceiling fitting, the rest of the corridor dark",
        "excludes": ["domestic"],
        "quiet": 1,
    },
    "basement": {
        "where": "a low basement, pipes along the ceiling, a concrete floor",
        "light": "one bare bulb on a flex, everything else dark",
        "excludes": ["industrial", "sanitary"],
        "quiet": 1,
    },
    "bathhouse": {
        "where": "an old bathhouse, a row of cubicles, tiled floor, high windows",
        "light": "grey daylight through the high windows, nothing warm",
        "excludes": ["sanitary", "sports"],
        "quiet": 1,
    },
    "farm_shed": {
        "where": "a farm shed, bare boards, a workbench, stacked pallets",
        "light": "open door letting in flat daylight, one bulb off",
        "excludes": ["agri", "industrial", "rural"],
        "quiet": 2,
    },
    "loading_bay": {
        "where": "a loading bay, roller door, painted floor markings, one pallet",
        "light": "strip light above the door, the bay itself dim",
        "excludes": ["industrial", "retail"],
        "quiet": 2,
    },
    "garage": {
        "where": "a domestic garage, concrete floor, workbench, roller door shut",
        "light": "one bulb on a pull cord, the rest of the space dark",
        "excludes": ["domestic", "industrial"],
        "quiet": 2,
    },
    "wastelot": {
        "where": "an empty fenced wastelot, flat ground, weeds at the edges",
        "light": "flat overcast daylight, no shadows, no direction",
        "excludes": ["rural", "industrial"],
        "quiet": 2,
    },
    "parking_roof": {
        "where": "an open parking deck, painted bays, low concrete wall",
        "light": "flat daylight, no sun angle, no shadows to speak of",
        "excludes": ["transit", "civic"],
        "quiet": 2,
    },
    "station_platform": {
        "where": "a station platform, yellow line, canopy overhead, empty",
        "light": "platform strip lights, evenly spaced, nothing special",
        "excludes": ["transit", "civic"],
        "quiet": 2,
    },
    "forest_path": {
        "where": "a forest path, straight and level, going on between the trunks",
        "light": "flat daylight through the canopy, no shafts, no fog",
        "excludes": ["rural"],
        "quiet": 3,
    },
    "field": {
        "where": "a flat open field, short grass, nothing on the horizon",
        "light": "even daylight under high cloud",
        "excludes": ["agri", "rural"],
        "quiet": 3,
    },
    "beach_flat": {
        "where": "a flat empty beach, wet sand, sea out of frame",
        "light": "flat overcast light, the horizon barely visible",
        "excludes": ["rural"],
        "quiet": 3,
    },
    "riverbank": {
        "where": "a flat riverbank, still water, grass cut short",
        "light": "even daylight, no sun angle",
        "excludes": ["rural"],
        "quiet": 3,
    },
}


# ════════════════════════════════════════════════════════════════════════════════
# АТМОСФЕРА — не загадка. Отдельный банк, честная пометка.
# ════════════════════════════════════════════════════════════════════════════════
# Оставлены, потому что как фоны/подложки они годятся, но выдавать их за
# находку нельзя: там любому предмету находится объяснение.
ATMOSPHERE_LOCATIONS: dict[str, dict] = {
    "moon": {
        "where": "on the surface of the Moon, in grey regolith, with black sky",
        "light": "one sun at a low angle, hard black shadows",
        "excludes": ["rural"],
    },
    "orbit": {
        "where": "inside an orbital station corridor, handrails and equipment",
        "light": "cold instrument light and the sun through a round window",
        "excludes": ["transit"],
    },
    "underwater": {
        "where": "on the sea floor, several metres down, in blue-green murk",
        "light": "one shaft of sunlight from far above, the rest dark",
        "excludes": ["aquatic"],
    },
    "saltflat": {
        "where": "on a cracked white salt flat at noon, no landmarks",
        "light": "brutal overhead sun, almost no shadow",
        "excludes": ["rural", "agri"],
    },
    "glacier": {
        "where": "inside an ice cave, blue translucent walls",
        "light": "light diffusing through the ice, no hard shadows",
        "excludes": ["aquatic"],
    },
    "quarry": {
        "where": "in an abandoned quarry pit, terraced stone steps",
        "light": "flat daylight off pale stone",
        "excludes": ["industrial"],
    },
    "mine": {
        "where": "deep in a mine shaft, timber supports, rails",
        "light": "one lamp on a wire, everything else black",
        "excludes": ["industrial"],
    },
    "swamp": {
        "where": "in a flooded cypress swamp, still black water",
        "light": "green-grey light through mist",
        "excludes": ["aquatic"],
    },
    "desert": {
        "where": "on sand dunes at night, nothing for kilometres",
        "light": "moonlight, very low contrast",
        "excludes": ["rural", "agri"],
    },
    "tundra": {
        "where": "on open tundra under low overcast, no trees",
        "light": "flat white light, no shadows, no direction",
        "excludes": ["rural"],
    },
    "subwaytunnel": {
        "where": "in a disused subway tunnel, curved concrete, cable brackets",
        "light": "one caged bulb, black between them",
        "excludes": ["transit", "institutional"],
    },
    "subway_surface": {
        "where": "on the ground above a disused subway line, just kerb and paving",
        "light": "flat daylight, the street cropped out of frame",
        "excludes": ["transit"],
    },
}


# ════════════════════════════════════════════════════════════════════════════════
# ПРЕДМЕТЫ
# ════════════════════════════════════════════════════════════════════════════════
# home    — класс дома; предмет попадает в локацию, только если home не в excludes
# phrase  — ЧТО ЭТО. Без состояния: состояние живёт в acts, иначе act вырождается
#           в пересказ (был реальный класс дефекта, ловится тестом)
# acts    — ПОЗА или ЗАМОРОЖЕННЫЙ ПРОЦЕСС. Никаких причин. Именно пустое место
#           для причины порождает три версии
# reads   — три и более прочтений, которые кадр обязан допускать. Это проверяемое
#           требование к загадке, а не самоотчёт
OBJECTS: dict[str, dict] = {
    "palletjack": {
        "home": "industrial", "carries": "склады, фуры, разметка пола",
        "phrase": "a hand pallet truck, forks and handle",
        "acts": [
            "standing square in the middle of the floor, forks down, handle upright",
            "standing in the aisle facing the seats, handle tilted back",
            "standing against the wall at an angle, forks pointing at nothing",
            "standing alone, handle raised, exactly where it was set down",
        ],
        "reads": [
            "ждёт, когда привезут груз",
            "стоит на месте зрителя — кто-то оставил вместо себя",
            "её забыли и не вернули на склад",
            "стоит здесь намеренно, как часть постановки",
        ],
    },
    "trolley": {
        "home": "retail", "carries": "супермаркет: асфальт, полки, тележки в тележках",
        "phrase": "a shopping trolley, wire basket, four castor wheels",
        "acts": [
            "standing upright on the path, basket empty, handle straight",
            "sitting in the middle of the floor with the wheels still free",
            "turned to face a wall, basket level, full of nothing",
            "standing still on the grass, all four wheels on the ground",
        ],
        "reads": [
            "привезли и забыли, когда выгружали",
            "считают, что это часть пейзажа",
            "ждёт, пока вернётся тот, кто её прикатил",
            "подставка, чтобы достать до чего-то",
        ],
    },
    "fridge": {
        "home": "domestic", "carries": "кухня, свет из щели, еда",
        "phrase": "a refrigerator, door, shelves",
        "acts": [
            "standing in the corner, door open, light on inside",
            "pushed against the wall, door ajar, nothing visible on the shelves",
            "standing in the middle of the floor, shut, humming or not",
            "set down level, doors closed, standing where it stands",
        ],
        "reads": [
            "принесли и поставили, сами не уверены зачем",
            "работает как холодильник — просто место выбрано странное",
            "оставлен после переезда, никто не вывез",
            "кто-то проверяет, работает ли он вообще здесь",
        ],
    },
    "bathtub": {
        "home": "sanitary", "carries": "ванная, слив, кафель",
        "phrase": "a bathtub on its own feet, taps in place",
        "acts": [
            "standing free in the middle of the floor, taps overhanging nothing",
            "set down level, empty, dry, upright",
            "turned on its side and resting on the rim",
            "standing where it stands, the taps facing a wall",
        ],
        "reads": [
            "забыли при выезде, тяжёлая, не вывезли",
            "поставили как перегородку или столешницу",
            "ждёт, пока придёт сантехник и скажет, куда его",
            "вмонтировали в пол, и это часть замысла",
        ],
    },
    "stove": {
        "home": "domestic", "carries": "кухня, огонь, чугун",
        "phrase": "a cast-iron cooking stove, doors ajar, no flue",
        "acts": [
            "standing square on the floor, doors open, nothing inside",
            "pushed to the wall, cold, level",
            "standing on its own in the middle of the floor",
            "set down at an angle, not level, not against anything",
        ],
        "reads": [
            "притащили, чтобы греть, и бросили",
            "считают, что тут можно готовить",
            "остался после переезда, в зал не влез",
            "экспонат или реквизит",
        ],
    },
    "bunkbed": {
        "home": "institutional", "carries": "казарма, койка, режим",
        "phrase": "a metal bunk bed frame, two mattresses, ladder",
        "acts": [
            "standing level and made up, both mattresses square on",
            "pushed against the wall, squared up to it",
            "standing alone in the middle of the floor, ladder facing out",
            "set down with the mattresses on, nothing under it",
        ],
        "reads": [
            "поставили, чтобы кто-то спал",
            "мебель из другого помещения, переехала сюда",
            "запасной вариант на случай, если придут гости",
            "часть выставки или съёмки",
        ],
    },
    "lockers": {
        "home": "institutional", "carries": "раздевалка, номерки, коридор",
        "phrase": "a bank of steel lockers, doors",
        "acts": [
            "standing straight against the wall, all doors shut, in a row",
            "standing alone in the middle of the floor, doors closed",
            "one door open, the rest shut, squared up level",
            "pushed under the workbench, doors facing out",
        ],
        "reads": [
            "поставили, чтобы вещи не валялись",
            "остались от прежнего хозяина помещения",
            "временно, пока не привезут нормальные шкафчики",
            "экспонат, показывающий, как раньше",
        ],
    },
    "trolleys_shelf": {
        "home": "retail", "carries": "витрина, ценники, запас",
        "phrase": "a steel retail shelf unit, three tiers",
        "acts": [
            "standing empty in the middle of the floor, square to the room",
            "pushed against the wall, shelves level, empty",
            "standing where it stands, nothing on any tier",
            "set down facing a wall, back to the room",
        ],
        "reads": [
            "забыли при переезде",
            "считают, что в нём что-то будет стоять",
            "остался от прежнего помещения",
            "мебель под будущую экспозицию",
        ],
    },
    "bedframe": {
        "home": "domestic", "carries": "спальня, подушка, простыня",
        "phrase": "a metal bed frame and mattress, made up",
        "acts": [
            "made up and square, standing level on the floor",
            "pushed against the wall, made up, nothing else on it",
            "standing in the middle of the floor, pillows square",
            "set down facing a wall, the side facing us",
        ],
        "reads": [
            "поставили, чтобы спать",
            "переехала из другой комнаты или квартиры",
            "стоит, пока не приедет нормальная кровать",
            "поставлена как экспонат",
        ],
    },
    "armchair": {
        "home": "domestic", "carries": "гостиная, кресло, камин",
        "phrase": "a padded armchair, seat and arms",
        "acts": [
            "standing square in the middle of the floor, facing nothing",
            "set against the wall at a slight angle, facing the room",
            "alone in the centre, cushions plumped",
            "pushed to the corner, turned to face the wall",
        ],
        "reads": [
            "считают, что тут кто-то будет сидеть",
            "переехала из другой комнаты",
            "вынесена, чтобы освободить место",
            "стоит как часть инсталляции",
        ],
    },
    "piano": {
        "home": "musical", "carries": "концертный зал, рояль, клавиши",
        "phrase": "a piano, keys and pedals",
        "acts": [
            "standing closed in the middle of the floor, square to the room",
            "pushed against the wall, bench tucked under, lid shut",
            "standing alone, fallboard open, keys uncovered",
            "set down level, lid closed, in the middle of the room",
        ],
        "reads": [
            "поставили, потому что есть место и руки",
            "остался от прежнего владельца",
            "стоит как реквизит или экспонат",
            "привезли, но никто не играет",
        ],
    },
    "vending": {
        "home": "retail", "carries": "монеты, витрина, «продай что-нибудь»",
        "phrase": "a coin-operated vending machine, glass front",
        "acts": [
            "standing against the wall, upright, shut, glass front dark",
            "standing alone in the middle of the room, level, empty",
            "pushed into the corner, facing out, unlit",
            "set down square, doors closed, contents not visible",
        ],
        "reads": [
            "поставили, чтобы что-то продавать",
            "осталась от прежнего арендатора",
            "стоит как объект, никто не вкладывает монеты",
            "привезли и забыли подключить",
        ],
    },
    "jacuzzi": {
        "home": "sanitary", "carries": "ванна, форсунки, крышка",
        "phrase": "a jacuzzi tub, controls on the rim",
        "acts": [
            "standing dry in the middle of the floor, controls facing out",
            "pushed against the wall, level, not filled",
            "set down square, lid closed, nothing running",
            "standing alone in the room, jets off, dry and empty",
        ],
        "reads": [
            "притащили, чтобы пользоваться",
            "наполнить нечем — стоит как символ",
            "остался после ремонта",
            "поставлен как экспонат",
        ],
    },
    "sauna": {
        "home": "sanitary", "carries": "баня, жар, камни",
        "phrase": "a barrel sauna stove with stones",
        "acts": [
            "standing bare in the middle of the room, stones piled, cold",
            "set down square, no chimney, not lit",
            "alone in the corner, door open, nothing heating",
            "standing level with the door facing out, not connected to anything",
        ],
        "reads": [
            "поставили, чтобы топить",
            "стоит без дымохода — не работает, но стоит",
            "остался от прежнего хозяина",
            "выставлен как объект",
        ],
    },
    "pooltable": {
        "home": "sports", "carries": "бильярд, сукно, кий",
        "phrase": "a full-size pool table, six pockets",
        "acts": [
            "standing level in the middle of the room, balls racked, cloth flat",
            "pushed against the wall, racked, nowhere to swing a cue",
            "alone and squared up, cue ball on the spot",
            "set down in the middle, cloth flat, no balls on it at all",
        ],
        "reads": [
            "поставили, чтобы играть",
            "стоит, но бить некому и негде",
            "переехала из бара или клуба",
            "часть экспозиции",
        ],
    },
    "treadmill": {
        "home": "sports", "carries": "бег, дорожка, консоль",
        "phrase": "a treadmill with console and handrails",
        "acts": [
            "standing still, belt square and stopped, console dark",
            "pushed against the wall, folded level, not running",
            "alone in the middle of the room, belt flat and motionless",
            "set down facing a wall, console turned to the wall",
        ],
        "reads": [
            "поставили, чтобы бегать",
            "стоит как часть оформления",
            "переехал из другой комнаты",
            "выставлен как экспонат",
        ],
    },
    "lockers_cage": {
        "home": "institutional", "carries": "склад, клетка, режим хранения",
        "phrase": "a steel storage cage on castors, mesh sides",
        "acts": [
            "standing level in the middle of the room, doors shut, empty",
            "pushed against the wall, mesh door closed, nothing inside",
            "alone, square to the room, castors free",
            "set down in the corner, door latched, nothing stored",
        ],
        "reads": [
            "поставили, чтобы хранить",
            "стоит пустая — забыли наполнить",
            "переехала со склада",
            "нужна как ограждение",
        ],
    },
    "safe": {
        "home": "institutional", "carries": "сейф, ценности, код",
        "phrase": "a floor safe with a dial and a handle",
        "acts": [
            "standing against the wall, dial shut, handle level",
            "sitting alone in the middle of the floor, door closed",
            "set down square, dial at zero, nothing behind the door",
            "in the corner, closed, dial turned slightly from zero",
        ],
        "reads": [
            "поставили, чтобы хранить деньги",
            "переехал с прежнего места",
            "стоит, потому что выглядит надёжным",
            "реквизит или экспонат",
        ],
    },
    "piano_upright_small": {
        "home": "musical", "carries": "школа, класс, репетиция",
        "phrase": "a small piano, single lid",
        "acts": [
            "standing closed against the wall, stool pushed under",
            "alone in the middle of the room, lid closed, level",
            "set down square, bench beside it, no sheet music",
            "pushed to the corner, closed, lid flat",
        ],
        "reads": [
            "поставили, чтобы играть",
            "остался после прежних владельцев",
            "ждёт, пока придёт ученик",
            "экспонат",
        ],
    },
    "shower_stall": {
        "home": "sanitary", "carries": "душевая кабина, поддон, слив",
        "phrase": "a shower tray with a riser and no doors",
        "acts": [
            "standing level on the floor, riser upright, tray dry",
            "set down in the corner, square, nothing connected",
            "alone in the middle of the room, tray empty, riser up",
            "pushed against the wall, tray level, no cubicle around it",
        ],
        "reads": [
            "поставили, чтобы мыться",
            "стоит без воды",
            "остался после ремонта",
            "нужен как перегородка",
        ],
    },
    "clinic_bed": {
        "home": "medical", "carries": "койка, больной, палата",
        "phrase": "a clinic examination bed, paper roll",
        "acts": [
            "standing level in the middle of the room, paper roll on",
            "pushed against the wall, headrest up, made up flat",
            "alone, squared to the room, nothing on it",
            "set down square, wheels braked, paper not touched",
        ],
        "reads": [
            "поставили, чтобы класть больного",
            "переехала из другой палаты или кабинета",
            "стоит как символ медицины",
            "нужна как мебель, а не как койка",
        ],
    },
    "xray": {
        "home": "medical", "carries": "снимок, диагноз, кабинет",
        "phrase": "a wall X-ray viewing light box",
        "acts": [
            "mounted on the wall, switched off, blank and level",
            "standing against the wall, faces empty, not lit",
            "alone in the room, hooked to the wall, dark",
            "set down flat on a stand, off, no film in it",
        ],
        "reads": [
            "повесили, чтобы смотреть снимки",
            "стоит, но снимков нет",
            "остался от прежнего кабинета",
            "нужен как светящаяся панель",
        ],
    },
    "dentalchair": {
        "home": "medical", "carries": "стоматолог, кресло, приём",
        "phrase": "a dental chair with a headrest and footrest",
        "acts": [
            "standing level in the middle of the room, raised, footrest down",
            "set down against the wall, chair up, not connected",
            "alone, squared up, everything folded away",
            "pushed to the corner, lowered flat, no light overhead",
        ],
        "reads": [
            "поставили, чтобы принимать",
            "переехала из другой клиники",
            "стоит как кресло, если под ним стул",
            "экспонат",
        ],
    },
    "microscope": {
        "home": "medical", "carries": "лаборатория, линза, препарат",
        "phrase": "a bench microscope on a stand",
        "acts": [
            "standing level on the table, stage empty, lamp off",
            "set down in the middle of the table, objective down, nothing under it",
            "standing alone, stage empty, power light out",
            "pushed to the edge of the bench, level, cold",
        ],
        "reads": [
            "поставили, чтобы смотреть",
            "стоит без препаратов",
            "переехал из другой лаборатории",
            "нужен как есть",
        ],
    },
    "centrifuge_small": {
        "home": "medical", "carries": "пробирки, обороты, лаборатория",
        "phrase": "a small bench centrifuge, lid and bowl",
        "acts": [
            "standing level on the bench, lid closed, not running",
            "set down square, lid up, bowl empty",
            "alone in the middle of the bench, stopped, silent",
            "pushed to the back of the bench, closed, unplugged",
        ],
        "reads": [
            "поставили, чтобы работать",
            "стоит без пробирок",
            "переехала из другой лаборатории",
            "нужна как ёмкость",
        ],
    },
    "stove_gas": {
        "home": "hospitality", "carries": "кухня, конфорки, посуда",
        "phrase": "a commercial gas range with four burners",
        "acts": [
            "standing level in the middle of the room, burners cold, knobs at zero",
            "pushed against the wall, no gas line attached",
            "alone, squared up, not lit, knobs in a row",
            "set down at an angle, not against anything",
        ],
        "reads": [
            "поставили, чтобы готовить",
            "стоит без газа",
            "переехала с другой кухни",
            "нужна как стойка",
        ],
    },
    "till": {
        "home": "retail", "carries": "касса, чек, смена",
        "phrase": "a mechanical cash register with keys",
        "acts": [
            "standing level on the counter, keys up, drawer shut",
            "set down in the middle of the counter, drawer closed",
            "alone, square to the room, nothing beside it",
            "pushed to the back of the counter, facing the wall",
        ],
        "reads": [
            "поставили, чтобы считать деньги",
            "осталась от прежнего магазина",
            "стоит как касса без смены",
            "нужна как ящик",
        ],
    },
    "gym_bench": {
        "home": "sports", "carries": "жим, скамья, железо",
        "phrase": "a weight bench with a bar and plates",
        "acts": [
            "standing level in the middle of the floor, bar racked, plates on",
            "pushed against the wall, bar off, nothing loaded",
            "alone, squared to the room, bar not on it",
            "set down facing a wall, weights unloaded",
        ],
        "reads": [
            "поставили, чтобы качаться",
            "стоит, но поднимать некому",
            "переехал из другой части зала",
            "нужен как верстак",
        ],
    },
    "lockers_steel": {
        "home": "institutional", "carries": "армия, форма, номера",
        "phrase": "a single tall steel locker with a louvred door",
        "acts": [
            "standing square in the middle of the room, door shut, level",
            "pushed against the wall, door closed, keyhole empty",
            "alone, facing the room, nothing on top of it",
            "set down in the corner, door ajar, hinges visible",
        ],
        "reads": [
            "поставили, чтобы вещи были под рукой",
            "остался от прежнего хозяина",
            "нужен как шкаф",
            "ждёт следующего хозяина",
        ],
    },
    "bicycle_shop": {
        "home": "sports", "carries": "велосипед, цепь, дорога",
        "phrase": "a bicycle on a repair stand",
        "acts": [
            "clamped in the stand at working height, wheels off the ground",
            "standing upright on the stand, chain on the big cog",
            "alone in the middle of the room, held level in the clamp",
            "hung on the stand, front wheel turned, still",
        ],
        "reads": [
            "поставили, чтобы чинить",
            "стоит, но чинить некому",
            "переехал из другой мастерской",
            "нужен как стойка",
        ],
    },
    "easel": {
        "home": "grand", "carries": "мастерская, холст, свет",
        "phrase": "a wooden easel, three legs and a ledge",
        "acts": [
            "standing open and empty in the middle of the room, square to nothing",
            "set down against the wall, folded flat, no canvas on it",
            "alone, facing a wall, legs spread, no board",
            "standing at working height with nothing on the ledge",
        ],
        "reads": [
            "поставили, чтобы рисовать",
            "стоит пустой, рисовать некому",
            "переехал из мастерской",
            "нужен как стойка",
        ],
    },
    "harpsichord": {
        "home": "grand", "carries": "барокко, клавиши, зал",
        "phrase": "a harpsichord on a stand, lid and keys",
        "acts": [
            "standing open in the middle of the room, keys uncovered, no bench",
            "set down against the wall, lid open, strings visible",
            "alone, squared to the room, lid up and empty",
            "pushed to the corner, open, taking up the whole wall",
        ],
        "reads": [
            "поставили, чтобы играть",
            "остался от прежнего зала",
            "нужен как стол",
            "выставлен как экспонат",
        ],
    },
    "dummy_box": {
        "home": "sports", "carries": "удар, тренировка, форма",
        "phrase": "a training punching dummy on a chain",
        "acts": [
            "hanging still on its chain at working height, level",
            "hanging in the middle of the room, not swinging, chain straight",
            "alone, hanging from a bracket, exactly centred",
            "pushed to the wall, hanging from a hook, still",
        ],
        "reads": [
            "повесили, чтобы бить",
            "висит, бить некому",
            "переехал из другой секции",
            "нужен как подвес",
        ],
    },
    "cash_booth": {
        "home": "civic", "carries": "касса, смена, очередь",
        "phrase": "a glass-fronted cash booth",
        "acts": [
            "standing level and closed in the middle of the room, shutter down",
            "set down against the wall, window shut, queue side facing the room",
            "alone in the middle of the room, unlit, glass intact",
        ],
        "reads": [
            "поставили, чтобы принимать оплату",
            "остался от прежнего помещения",
            "стоит как кабина",
        ],
    },
    "pet_carrier": {
        "home": "aviculture", "carries": "перевозка, клетка, дорога",
        "phrase": "a pet carrier, wire front, door",
        "acts": [
            "standing level and shut in the middle of the floor",
            "set down against the wall, door closed, latch on",
            "alone, square to the room, empty",
        ],
        "reads": [
            "принесли и забыли",
            "стоит, ждёт, когда придёт хозяин",
            "нужна как переноска",
        ],
    },
    "birdcage_idle": {
        "home": "aviculture", "carries": "клетка, корм, вода",
        "phrase": "a domed wire birdcage on its hook",
        "acts": [
            "hanging still and empty on its hook, door open",
            "hanging level from a bracket, nothing inside, cup in place",
            "alone, hanging in the middle of the room, door open",
        ],
        "reads": [
            "повесили, но птицу не привезли",
            "ждёт, пока вернётся хозяин",
            "повесили как есть",
        ],
    },
    "aquarium_idle": {
        "home": "aquatic", "carries": "аквариум, вода, стекло",
        "phrase": "an aquarium tank on its stand",
        "acts": [
            "standing level and switched off, water still, gravel level",
            "set down against the wall, off, lid closed",
            "alone in the middle of the room, lit from within, empty",
        ],
        "reads": [
            "поставили, чтобы завести рыб",
            "стоит пустая, рыб нет",
            "нужна как ёмкость",
        ],
    },
    "tractor_small": {
        "home": "agri", "carries": "поле, трактор, земля",
        "phrase": "a small garden tractor with a mower deck",
        "acts": [
            "standing level in the middle of the floor, deck down, engine off",
            "set down facing a wall, seat square, key absent",
            "alone, parked exactly in the centre, wheels straight",
        ],
        "reads": [
            "загнали, чтобы косить",
            "стоит, косить негде",
            "переехал с улицы",
        ],
    },
    "mower": {
        "home": "agri", "carries": "газон, срез, сезон",
        "phrase": "a walk-behind lawnmower, handle up",
        "acts": [
            "standing level on the floor, handle up, deck clean",
            "set down facing a wall, handle folded, unplugged",
            "alone in the middle of the room, wheels straight, off",
        ],
        "reads": [
            "поставили на зиму",
            "остался после продажи дома",
            "нужен как техника",
        ],
    },
    "hammock": {
        "home": "rural", "carries": "отдых, тень, верёвка",
        "phrase": "a rope hammock on a wooden stand",
        "acts": [
            "hung slack and empty between the two stands, level",
            "hanging in the middle of the room, not slung with weight",
            "alone, strung between two points, nothing in it",
        ],
        "reads": [
            "повесили, чтобы лежать",
            "висит пустая, ложиться некому",
            "нужна как декорация",
        ],
    },
    "wheelbarrow": {
        "home": "rural", "carries": "сад, земля, тара",
        "phrase": "a wheelbarrow on its legs and handles",
        "acts": [
            "standing level and tipped back on its legs, tray empty",
            "set down facing a wall, handles square, nothing in the tray",
            "alone in the middle of the floor, tray down, upright",
        ],
        "reads": [
            "завезли в помещение, чтобы хранить",
            "стоит, возить некому и некуда",
            "переехал с улицы",
        ],
    },
    "beehive_box": {
        "home": "agri", "carries": "пасека, улей, мёд",
        "phrase": "a stack of bee boxes on a stand",
        "acts": [
            "standing level and closed on its stand, lid square",
            "set down in the corner, boxes stacked straight, entrance shut",
            "alone in the middle of the room, boxes level, lid on",
        ],
        "reads": [
            "поставили, чтобы завёл пчёл",
            "стоит, пчёл нет",
            "нужен как ящик",
        ],
    },
    "sheeppen_gate": {
        "home": "agri", "carries": "загон, калитка, овцы",
        "phrase": "a metal livestock gate panel",
        "acts": [
            "leaning square against the wall, gate shut",
            "standing free in the middle of the floor, hung on its hinges",
            "set down flat on the ground, closed, square to the wall",
        ],
        "reads": [
            "затащили, чтобы закрыть проём",
            "стоит, овец нет",
            "нужна как перегородка",
        ],
    },
    "milking_stool_idle": {
        "home": "agri", "carries": "коровник, дойка, утро",
        "phrase": "a three-legged milking stool",
        "acts": [
            "standing square on the floor, seat up, level",
            "set down against the wall, stool square, nothing around",
            "alone in the middle of the room, all three legs down",
        ],
        "reads": [
            "поставили, чтобы доить",
            "стоит, коров нет",
            "нужен как табурет",
        ],
    },
    "pallet_plain": {
        "home": "industrial", "carries": "тара, склад, штабель",
        "phrase": "a wooden pallet, three blocks",
        "acts": [
            "lying flat on the floor, boards square, nothing on it",
            "standing alone in the middle of the room, flat and level",
            "stacked three high and square, nothing on top",
        ],
        "reads": [
            "затащили, чтобы что-то поставить",
            "стоит пустая, груза нет",
            "нужна как верстак",
        ],
    },
    "crate_stack": {
        "home": "retail", "carries": "ящик, грузчик, погрузка",
        "phrase": "a stack of plastic crates",
        "acts": [
            "stacked square and empty, nothing in any of them",
            "standing alone in the middle of the room, lids off",
            "set down against the wall, stacked inside one another, level",
        ],
        "reads": [
            "затащили, чтобы наполнить",
            "стоят пустыми",
            "нужны как тара",
        ],
    },
    "coat_rack": {
        "home": "hospitality", "carries": "гардероб, номерок, вешалка",
        "phrase": "a freestanding coat rack with hooks",
        "acts": [
            "standing empty in the middle of the floor, hooks level",
            "set down against the wall, no coats on any hook",
            "alone in the corner, all hooks empty, square",
        ],
        "reads": [
            "поставили, чтобы вешать",
            "стоит пустая, вешать нечего",
            "нужна как вешалка",
        ],
    },
    "luggage_cart": {
        "home": "hospitality", "carries": "гостиница, чемодан, поезд",
        "phrase": "a hotel luggage trolley with a brass rail",
        "acts": [
            "standing empty and level in the middle of the floor, rail bare",
            "set down against the wall, nothing hung on the rail",
            "alone in the corner, castors square, empty",
        ],
        "reads": [
            "поставили, чтобы возить чемоданы",
            "стоит пустая, гостей нет",
            "нужна как тележка",
        ],
    },
    "cloak_counter": {
        "home": "hospitality", "carries": "гардероб, номерок, стойка",
        "phrase": "a cloakroom counter with a rack behind",
        "acts": [
            "standing level and closed, rack empty behind it",
            "set down against the wall, counter square, no tickets",
        ],
        "reads": [
            "поставили, чтобы принимать вещи",
            "стоит, вещей нет",
            "нужна как стойка",
        ],
    },
    "cloak_tier": {
        "home": "hospitality", "carries": "гараж, номерки, вешалка",
        "phrase": "a tiered cloakroom rack with brass hooks",
        "acts": [
            "standing level with every hook empty, tiers square",
            "set down against the wall, hooks in a row, nothing hung",
        ],
        "reads": [
            "поставили, чтобы вешать",
            "стоит пустая",
            "нужна как стойка",
        ],
    },
    "lab_bench": {
        "home": "medical", "carries": "лаборатория, стекло, реактивы",
        "phrase": "a laboratory bench with a sink and taps",
        "acts": [
            "standing level and empty along the wall, taps overhanging a drain",
            "set down in the middle of the room, square, nothing on the top",
            "alone, bench top clear, no glassware on it",
        ],
        "reads": [
            "поставили, чтобы работать",
            "стоит пустая",
            "нужна как верстак",
        ],
    },
    "incubator_idle": {
        "home": "aviculture", "carries": "вывод, тепло, яйца",
        "phrase": "an egg incubator, glass lid, dial",
        "acts": [
            "standing level and closed, dial at zero, nothing inside",
            "set down on a table, lid shut, power light off",
            "alone in the middle of the room, plugged in, unlit",
        ],
        "reads": [
            "поставили, чтобы выводить",
            "стоит, яиц нет",
            "нужна как ящик",
        ],
    },
    "diving_tank": {
        "home": "aquatic", "carries": "дыхание, глубина, вода",
        "phrase": "a diving tank on a stand, valve on top",
        "acts": [
            "standing level and shut, valve closed, gauge at zero",
            "set down against the wall, capped, not connected",
            "alone in the middle of the room, gauge visible, unpressurised",
        ],
        "reads": [
            "поставили, чтобы нырять",
            "стоит, нечем дышать под водой",
            "нужна как баллон",
        ],
    },
    "votingbooth": {
        "home": "civic", "carries": "выборы, кабина, очередь",
        "phrase": "a folding voting booth, curtain",
        "acts": [
            "standing open and empty, curtain pushed back, square to the room",
            "set down against the wall, folded flat, curtain tied",
            "alone in the middle of the room, curtain closed, nothing inside",
        ],
        "reads": [
            "поставили, чтобы голосовать",
            "стоит, голосующих нет",
            "нужна как перегородка",
        ],
    },
    "passportbooth": {
        "home": "civic", "carries": "паспорт, очередь, стекло",
        "phrase": "a glass passport photo booth",
        "acts": [
            "standing level and closed, curtain down, stool inside unseen",
            "set down against the wall, shut, light off",
            "alone in the middle of the room, curtained, unlit",
        ],
        "reads": [
            "поставили, чтобы снимать",
            "стоит, снимать некому",
            "нужна как кабина",
        ],
    },
    "postbox_wall": {
        "home": "civic", "carries": "почта, адрес, слот",
        "phrase": "a post box on a short leg",
        "acts": [
            "standing level and empty, slot shut, on its leg",
            "set down against the wall, flap shut, nothing posted",
            "alone in the middle of the room, empty, upright",
        ],
        "reads": [
            "поставили, чтобы принимать почту",
            "стоит пустая, почты нет",
            "нужна как ящик",
        ],
    },
    "scales_freight": {
        "home": "industrial", "carries": "вес, приёмка, груз",
        "phrase": "a platform weighbridge, checker plate",
        "acts": [
            "sitting flush and empty in the floor, plate level, nothing on it",
            "set down in the middle of the room, plate flat, dial at zero",
            "alone, standing level, platform clear",
        ],
        "reads": [
            "поставили, чтобы взвешивать",
            "стоит, взвешивать нечего",
            "нужна как пол",
        ],
    },
    "turnstile": {
        "home": "transit", "carries": "проход, билет, поток",
        "phrase": "a station turnstile, three arms",
        "acts": [
            "standing level and locked, arms across, no queue side",
            "set down in the middle of the room, arms half turned and stopped",
            "alone against the wall, arms still, unpowered",
        ],
        "reads": [
            "поставили, чтобы пропускать",
            "стоит, пассажиров нет",
            "нужна как преграда",
        ],
    },
    "departureboard": {
        "home": "transit", "carries": "рейс, время, табло",
        "phrase": "a split-flap departure board on brackets",
        "acts": [
            "hanging level and dark, all flaps in one row, no letters",
            "mounted on the wall, switched off, flaps square",
            "alone in the middle of the room, dark, hanging still",
        ],
        "reads": [
            "повесили, чтобы показывать рейсы",
            "висит, рейсов нет",
            "нужно как панель",
        ],
    },
    "luggage_scanner": {
        "home": "transit", "carries": "досмотр, лента, очередь",
        "phrase": "a baggage scanner with a conveyor mouth",
        "acts": [
            "standing level and off, belt still, curtain down",
            "set down against the wall, unplugged, mouth dark",
            "alone in the middle of the room, belt loop visible, silent",
        ],
        "reads": [
            "поставили, чтобы досматривать",
            "стоит, ленты не работают",
            "нужен как тумба",
        ],
    },
    "newsstand": {
        "home": "retail", "carries": "газета, киоск, смена",
        "phrase": "a folding newsstand, shutters and counter",
        "acts": [
            "standing level and closed, shutters down, shelves empty",
            "set down against the wall, folded flat, nothing inside",
            "alone in the middle of the room, closed, no counter out",
        ],
        "reads": [
            "поставили, чтобы продавать",
            "стоит закрытая, продавать некому",
            "нужен как шкаф",
        ],
    },
    "pet_shop_tank": {
        "home": "aviculture", "carries": "аквариум, вода, продажа",
        "phrase": "a row of small tanks on a shop counter",
        "acts": [
            "standing level in a row, all lids shut, water still",
            "set down against the wall, filtered, nothing visible in any",
            "alone on the counter, pumps off, glass clear",
        ],
        "reads": [
            "поставили, чтобы продавать",
            "стоят пустыми",
            "нужны как вёдра",
        ],
    },
    "ticket_booth": {
        "home": "transit", "carries": "билет, касса, окно",
        "phrase": "a ticket booth with a glass window",
        "acts": [
            "standing level and shuttered, window closed, no queue",
            "set down against the wall, glass intact, empty inside",
            "alone in the middle of the room, window down, unlit",
        ],
        "reads": [
            "поставили, чтобы продавать билеты",
            "стоит, пассажиров нет",
            "нужна как будка",
        ],
    },
    "photo_booth_small": {
        "home": "hospitality", "carries": "снимок, свет, кабина",
        "phrase": "a photo booth with a curtain and a seat",
        "acts": [
            "standing level and curtained, seat empty, unlit",
            "set down against the wall, curtain drawn, nobody inside",
            "alone in the middle of the room, curtain open, seat unused",
        ],
        "reads": [
            "поставили, чтобы сниматься",
            "стоит, сниматься некому",
            "нужна как кабина",
        ],
    },
    "stage_piano": {
        "home": "musical", "carries": "репетиция, сцена, настройка",
        "phrase": "a piano on a low platform",
        "acts": [
            "standing closed on the platform, bench pushed in, level",
            "set down at the back of the platform, square, lid shut",
            "alone on the platform, stool tucked under, not tuned or not",
        ],
        "reads": [
            "поставили, чтобы играть",
            "остался после репетиции",
            "нужен как стол",
        ],
    },
    "spotlight_stand": {
        "home": "grand", "carries": "сцена, свет, гастроли",
        "phrase": "a theatre followspot on a tall stand",
        "acts": [
            "standing level and switched off, lens facing forward",
            "set down at the back of the room, yoke loose, unlit",
            "alone in the middle of the floor, aimed at nothing",
        ],
        "reads": [
            "поставили, чтобы светить",
            "стоит, освещать нечего",
            "нужен как стойка",
        ],
    },
    "prop_table": {
        "home": "grand", "carries": "постановка, реквизит, стол",
        "phrase": "a long prop table with a plain top",
        "acts": [
            "standing level and empty in the middle of the room, bare top",
            "set down against the wall, square, nothing on it",
            "alone, table bare, legs square to the floor",
        ],
        "reads": [
            "поставили, чтобы раскладывать",
            "стоит пустая",
            "нужен как стол",
        ],
    },
    "rope_barrier": {
        "home": "civic", "carries": "очередь, граница, порядок",
        "phrase": "a queue barrier post with a rope",
        "acts": [
            "standing alone in the middle of the room, rope hanging slack",
            "set down at one end of a row that does not continue, rope slack",
            "alone against the wall, no queue in front of it",
        ],
        "reads": [
            "поставили, чтобы вставать в очередь",
            "стоит, очреди нет",
            "нужен как ограждение",
        ],
    },
    "stencil_table": {
        "home": "civic", "carries": "разметка, линия, покраска",
        "phrase": "a metal stencil table on legs",
        "acts": [
            "standing level and empty on the floor, no stencils on the rack",
            "set down against the wall, square, rack bare",
            "alone in the middle of the room, nothing on top",
        ],
        "reads": [
            "затащили, чтобы размечать",
            "стоит, размечать нечего",
            "нужен как стол",
        ],
    },
    "bus_shelter_frame": {
        "home": "transit", "carries": "ожидание, дождь, расписание",
        "phrase": "a bus shelter frame with a glass back",
        "acts": [
            "standing empty and level, bench clear, no timetable",
            "set down against the wall, glass intact, seat empty",
            "alone in the middle of the room, waiting, nobody on the bench",
        ],
        "reads": [
            "поставили, чтобы ждать автобус",
            "стоит, автобуса нет",
            "нужна как будка",
        ],
    },
    "school_locker_bank": {
        "home": "institutional", "carries": "школа, форма, класс",
        "phrase": "a row of school lockers, doors and vents",
        "acts": [
            "standing straight along the wall, all doors shut, in a row",
            "set down alone in the middle of the room, doors closed, square",
            "one door open, the rest shut, level with the floor",
        ],
        "reads": [
            "поставили, чтобы вещи были в классе",
            "остались от прежнего класса",
            "нужны как шкафчики",
        ],
    },
    "map_cabinet": {
        "home": "institutional", "carries": "карта, архив, ящики",
        "phrase": "a flat map cabinet with many drawers",
        "acts": [
            "standing level and shut, all drawers in, nothing pulled",
            "set down against the wall, square, one drawer half out",
            "alone in the middle of the room, drawers closed, empty",
        ],
        "reads": [
            "поставили, чтобы хранить карты",
            "стоит, карт нет",
            "нужен как тумба",
        ],
    },
    "microscope_room": {
        "home": "institutional", "carries": "урок, препарат, доска",
        "phrase": "a classroom microscope on a trolley",
        "acts": [
            "standing level on the trolley, stage empty, lamp off",
            "set down against the wall, focused nowhere, unlit",
            "alone in the middle of the room, trolley still",
        ],
        "reads": [
            "поставили, чтобы показывать",
            "стоит, показывать некому",
            "нужен как прибор",
        ],
    },
    "gym_locker_room": {
        "home": "sports", "carries": "раздевалка, полотенце, номер",
        "phrase": "a gym locker room, benches and hooks",
        "acts": [
            "standing empty along the wall, hooks bare, benches clear",
            "set down alone in the middle of the room, all shut",
            "one locker open, the rest shut, nobody's things anywhere",
        ],
        "reads": [
            "посетителей нет, но шкафчики стоят",
            "нужны, чтобы переодеваться",
            "остались от прежнего зала",
        ],
    },
    "squat_rack": {
        "home": "sports", "carries": "присед, штанга, вес",
        "phrase": "a squat rack with a barbell racked",
        "acts": [
            "standing level with the bar racked and plates loaded, square to the room",
            "set down against the wall, bar on, nobody to lift it",
            "alone in the middle of the room, loaded, waiting",
        ],
        "reads": [
            "поставили, чтобы качаться",
            "стоит, поднимать некому",
            "нужен как стойка",
        ],
    },
    "changing_cubicle": {
        "home": "sports", "carries": "раздевалка, кабинка, номер",
        "phrase": "a changing cubicle with a curtain",
        "acts": [
            "standing level and curtained, nobody inside, hook bare",
            "set down against the wall, curtain open, seat empty",
            "alone in the middle of the room, curtained, unused",
        ],
        "reads": [
            "поставили, чтобы переодеваться",
            "стоит, раздеваться некому",
            "нужна как перегородка",
        ],
    },
    "workshop_lathe": {
        "home": "industrial", "carries": "станок, стружка, мастер",
        "phrase": "a metal lathe on its stand",
        "acts": [
            "standing level and switched off, chuck empty, bed clear",
            "set down against the wall, not plugged in, toolrest centred",
            "alone in the middle of the room, off, nothing turning",
        ],
        "reads": [
            "поставили, чтобы точить",
            "стоит, токарь не придёт",
            "нужен как верстак",
        ],
    },
    "air_compressor": {
        "home": "industrial", "carries": "сжатый воздух, гараж, шланг",
        "phrase": "a compressor tank on wheels",
        "acts": [
            "standing level and switched off, gauge at zero, hose coiled",
            "set down against the wall, off, wheel chocked",
            "alone in the middle of the room, silent, gauge down",
        ],
        "reads": [
            "поставили, чтобы дуть",
            "стоит, воздух никому не нужен",
            "нужен как баллон",
        ],
    },
    "pallet_scale": {
        "home": "industrial", "carries": "вес, погрузка, приём",
        "phrase": "a platform scale with a dial",
        "acts": [
            "standing level on the floor, platform clear, dial at zero",
            "set down against the wall, unwrapped, dial at zero",
            "alone in the middle of the room, empty, waiting",
        ],
        "reads": [
            "поставили, чтобы взвешивать",
            "стоит, груза нет",
            "нужна как пол",
        ],
    },
    "forklift_pallet": {
        "home": "industrial", "carries": "склад, вилы, штабель",
        "phrase": "a pallet with a forklift's forks still under it",
        "acts": [
            "lying flat with forks still slid under, pallet level on the floor",
            "standing with forks under and not withdrawn, empty",
            "alone in the middle of the room, forks in, nothing lifted",
        ],
        "reads": [
            "забыли вынуть вилы",
            "ждут, когда подвезут груз",
            "стоит как подставка",
        ],
    },
}


# ════════════════════════════════════════════════════════════════════════════════
# сборка промпта
# ════════════════════════════════════════════════════════════════════════════════

TAIL = ("the object large in frame and plainly out of place here, no people, "
        "no text, no lettering, no numbers, no signs, no labels, no arrows, "
        "no stickers, no posters, no graffiti, no logos, nothing written anywhere "
        "in frame")


def _norm(text: str) -> str:
    return " ".join(str(text).split()).strip().lower()


def locations_for(bank: str = "elsewhere") -> dict[str, dict]:
    return ATMOSPHERE_LOCATIONS if bank == "atmosphere" else RIDDLE_LOCATIONS


def pairs_for(loc_key: str, bank: str = "elsewhere") -> list[tuple[str, str]]:
    locs = locations_for(bank)
    if loc_key not in locs:
        raise ValueError(f"неизвестная локация '{loc_key}'")
    banned = set(locs[loc_key]["excludes"])
    return [(loc_key, ok) for ok, ov in OBJECTS.items() if ov["home"] not in banned]


def all_pairs(bank: str = "elsewhere") -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for lk in locations_for(bank):
        out.extend(pairs_for(lk, bank))
    return out


def _candidates(keys, bank, base):
    """Детерминированный круг «локация → пара → действие» без повторов.

    Порядок строится заранее и потом просто режется по count. Так локации
    чередуются гарантированно, а не «пока не кончится». Две прежние попытки
    сломались одинаково: первая тянула один предмет в разные локации (на seed 910
    cloakroom rack повторялся 4 раза из 12), вторая — при исчерпании незанятых
    предметов у локации продолжала крутить её же (12 промптов из одной локации).
    Обе выглядели как «прогон отработал», и обе тратили 12 задач впустую.
    """
    out = []
    cursors = {}
    for i, k in enumerate(keys):
        pairs = pairs_for(k, bank)
        cursors[k] = (base + i * 7) % len(pairs) if pairs else 0
    for step in range(len(keys) * 8):
        for k in keys:
            pairs = pairs_for(k, bank)
            if not pairs:
                continue
            lk, ok = pairs[cursors[k] % len(pairs)]
            cursors[k] += 1
            obj = OBJECTS[ok]
            acts = obj["acts"]
            out.append({
                "location": lk, "object": ok,
                "act": acts[step % len(acts)], "act_index": step % len(acts),
                "reads": obj["reads"], "bank": bank,
            })
    return out


def prompts(count: int = 8, loc: str = "", seed: int | None = None,
            bank: str = "elsewhere") -> tuple[list[str], list[dict]]:
    """Тройки «локация × предмет × состояние» для прогона.

    Обход: круг по локациям (иначе 8 задач съедают одну локацию целиком), внутри
    локации — круг по парам, разрешённым её `excludes`, внутри пары — круг по
    `acts`. Предмет в прогоне не повторяется. `seed` фиксирует выборку: одинаковый
    seed даёт одинаковый прогон — это нужно для повторной приёмки после правки слов.
    """
    if count <= 0:
        return [], []
    locs = locations_for(bank)
    keys = list(locs)
    named = (loc or "").strip()
    if named and named in locs:
        keys = [named]
    elif named:
        raise ValueError(f"неизвестная локация '{named}': доступны {sorted(locs)}")

    base = date.today().toordinal() if seed is None else int(seed)
    keys = keys[base % len(keys):] + keys[:base % len(keys)]

    picked = _pick_diverse(_candidates(keys, bank, base), count)
    prompts = []
    for m in picked:
        obj = OBJECTS[m["object"]]
        loc = locs[m["location"]]
        prompts.append(
            f"close on {obj['phrase']}, {m['act']}, {loc['where']}, "
            f"{loc['light']}, {TAIL}")
    return prompts, picked


def _pick_diverse(cands, count):
    """Первые `count` троек без повторов предметов, локации чередуются.

    Два прохода: сперва строго без повторов предметов, затем — если предметов не
    хватило — добираем оставшиеся из уже использованных. Повтор по ПРЕМЕТУ не
    допускается, пока есть незанятые; локации при этом идут по кругу.
    """
    picked, used_objects, seen_triples = [], set(), set()
    for m in cands:
        if len(picked) >= count:
            return picked
        triple = (m["location"], m["object"], m["act"])
        if m["object"] in used_objects or triple in seen_triples:
            continue
        picked.append(m)
        used_objects.add(m["object"])
        seen_triples.add(triple)
    if len(picked) < count:
        for m in cands:                      # добор: предметы повторяются, тройки — нет
            if len(picked) >= count:
                break
            triple = (m["location"], m["object"], m["act"])
            if triple in seen_triples:
                continue
            picked.append(m)
            seen_triples.add(triple)
    return picked[:count]
