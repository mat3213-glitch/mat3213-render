"""elsewhere_lexicon.py — словарь неожиданных сочетаний «локация × предмет × действие».

Зачем отдельный модуль. Пул `bank=elsewhere` сначала держал пары «локация → неожиданное»
(dict LOCATIONS прямо в imagefree_pool_job.py, 12 локаций × 6 предметов). На живом прогоне
2026-10-08 (ран 37756755318, 8/8 в пул) точное попадание было ровно одно — «тележка
супермаркета в лесу», раскручивать остальные было нечем. Разбор, почему именно она:

  1. ПРЕДМЕТ НОСИТ СВОЙ ДОМ ВНУТРИ СЕБЯ. Тележка — это не «железный предмет», это
     супермаркет: асфальт, ценники, тележка-в-тележке, холодный свет. Перенесённая в
     лес, она приносит туда целый мир, которого там нет. Стул или пылесос такой силы
     не имеют — они часть любой комнаты.
  2. ДОМ ПРЕДМЕТА ЯВНО ИСКЛЮЧЁТ ЛОКАЦИЮ. «Лес» — не «просто на улице», а место, где
     асфальта нет в принципе. Чем жёстче исключение, тем сильнее кадр.
  3. ДЕЙСТВИЕ ПОКАЗЫВАЕТ ВРЕМЯ. «Уходящая в хвою», «ещё гудит», «из щели растёт» —
     это не статичный предмет, а процесс, у которого есть прошлое. Статичный предмет
     читается как натюрморт, процесс читается как вопрос.

Отсюда три оси и типы, а не ручной кросс-продукт:

  * локация несёт `realm` — что это за место;
  * предмет несёт `home` (свой дом) и `carries` (что он приносит с собой);
  * действие — `acts` у предмета, написаны руками и всегда физически подходят ему.

Пара разрешена только если `home` предмета в списке `allows` локации. Это ровно тот
типызированный фильтр, который даёт осмысленность ручных пар, но не требует их писать
по одной: «лес» запрещает retail/urban/transit/industrial/civic и разрешает всё
остальное, поэтому тележка туда попадает, а скрипка — нет (не несёт чужого дома).

Почему не полный кросс-продукт (прямой запрет на полный кросс-продукт стоит в
imagefree_pool_job.py, и он прав): случайная пара даёт и неожиданное, и нелепое.
Случайная «вафельница на дне океана» — не метафора, а коллаж. Тип отсекает мусор,
а написанные вручную `acts` и `carries` оставляют смысл.

Канон (MEMORY_CORE): без лиц, без текста в кадре (значит — никаких книжных корешков
навскидку, ценников, вывесок, номеров), без неона, свет только честный. Предметы с
неизбежным текстом исключены или описаны так, чтобы текста не читалось.
"""

from __future__ import annotations

from datetime import date

# ── локации ──────────────────────────────────────────────────────────────────
# realm    — класс места (справочно, для отчётов)
# excludes — ДОМА предметов, которые тут у себя дома (тележка на тротуаре, трактор
#            в поле) или физически нелепы (аквариум под водой). Всё, чего в
#            excludes нет, разрешено: в локацию попадает предмет из чужого мира.
# where    — физическое описание места
# light    — честный свет, без неона
#
# Почему excludes, а не «список разрешённых» (так было в первой версии, и это была
# дыра): список allows в 20 локациях молча не содержал четырёх домов — civic, medical,
# grand, aquatic, — то есть четверть словаря была недостижима, и это никто не видел,
# потому что отсутствие выглядит как «просто не выпало». Исключения короткие, их
# видно глазами, а пропуск дома в excludes даёт лишнюю пару, а не мёртвый класс.
LOCATIONS: dict[str, dict] = {
    "forest": {
        "realm": "wild",
        "where": "deep in a dark coniferous forest, trunks packed close, ground fog",
        "light": "thin shafts of daylight through the canopy, dim everywhere else",
        "excludes": ["wild", "agri", "rural"],
    },
    "underwater": {
        "realm": "sea",
        "where": "on the sea floor, several metres down, in blue-green murk",
        "light": "a single shaft of sunlight from far above, everything else dark",
        "excludes": ["aquatic", "wild"],
    },
    "moon": {
        "realm": "airless",
        "where": "on the surface of the Moon, in grey regolith, with black sky",
        "light": "one sun at a low angle, hard black shadows, no atmosphere to soften them",
        "excludes": [],
    },
    "orbit": {
        "realm": "space",
        "where": "inside an orbital station corridor, equipment and handrails around",
        "light": "cold instrument light mixed with the sun through a round window",
        "excludes": [],
    },
    "city": {
        "realm": "urban",
        "where": "on a city pavement at night between tall buildings",
        "light": "sodium streetlight from above and cold window light from the facades",
        "excludes": ["civic", "retail", "transit", "institutional"],
    },
    "desert": {
        "realm": "arid",
        "where": "on sand dunes at night, nothing else for kilometres",
        "light": "moonlight, very low contrast, sand holding the light softly",
        "excludes": ["wild", "agri", "rural"],
    },
    "glacier": {
        "realm": "ice",
        "where": "inside an ice cave, blue translucent walls all around",
        "light": "light diffusing through the ice, no hard shadows anywhere",
        "excludes": ["aquatic", "wild"],
    },
    "mine": {
        "realm": "subterranean",
        "where": "deep in a mine shaft, timber supports, rails running away",
        "light": "one lamp on a wire, swinging, everything else black",
        "excludes": ["industrial"],
    },
    "palace": {
        "realm": "grand",
        "where": "in a palace hall, marble floor, tall windows, a chandelier above",
        "light": "cold daylight through tall windows, chandelier unlit",
        "excludes": ["grand", "domestic", "institutional"],
    },
    "rooftop": {
        "realm": "high",
        "where": "on the flat roof of a high tower, city far below",
        "light": "sky light and city glow from below, wind visible in things",
        "excludes": ["grand", "civic"],
    },
    "cinema": {
        "realm": "public",
        "where": "inside an old cinema hall, rows of seats, boarded screen",
        "light": "one work light at the back, dust in the air",
        "excludes": [],
    },
    "cabin": {
        "realm": "domestic",
        "where": "inside a wooden cabin in the woods, stove and small windows",
        "light": "warm lamp light and daylight through one small window",
        "excludes": ["domestic", "wild"],
    },
    "saltflat": {
        "realm": "arid",
        "where": "on a cracked white salt flat at noon, heat haze, no landmarks",
        "light": "brutal overhead sun, almost no shadow, bleached everything",
        "excludes": ["wild", "agri"],
    },
    "quarry": {
        "realm": "extractive",
        "where": "in an abandoned quarry pit, terraced stone steps, no water",
        "light": "flat daylight bouncing off pale stone, few shadows",
        "excludes": ["industrial"],
    },
    "swamp": {
        "realm": "wetland",
        "where": "in a flooded cypress swamp, knees of still black water, moss trunks",
        "light": "green-grey light through mist, no direct sun, reflections everywhere",
        "excludes": ["wild", "aquatic", "agri"],
    },
    "tundra": {
        "realm": "wild",
        "where": "on open tundra under low overcast sky, no trees at all, flat to the horizon",
        "light": "flat white overcast light, no shadows, no direction",
        "excludes": ["wild", "agri", "aquatic"],
    },
    "subwaytunnel": {
        "realm": "subterranean",
        "where": "in a disused subway tunnel, curved concrete, cable brackets on the walls",
        "light": "one caged bulb every twenty metres, everything between them black",
        "excludes": ["transit", "institutional", "civic"],
    },
    "attic": {
        "realm": "domestic",
        "where": "in a dusty attic under the roof, beams and stored furniture",
        "light": "one dusty skylight, hard beams of light through the dust",
        "excludes": ["domestic"],
    },
    "greenhouse": {
        "realm": "cultivated",
        "where": "inside a derelict glasshouse, empty growing beds, broken panes",
        "light": "hard daylight through the glass roof, bar shadows across the floor",
        "excludes": ["agri", "wild"],
    },
    "roofrunoff": {
        "realm": "high",
        "where": "on the flat back roof of a tenement block, tar and gravel, low parapet",
        "light": "flat city daylight, no sun angle, everything the same brightness",
        "excludes": ["transit", "civic", "grand"],
    },
}

# ── предметы ─────────────────────────────────────────────────────────────────
# home     — класс дома предмета; предмет попадает в локацию только если home в allows
# carries  — что предмет приносит с собой в кадр (документация замысла, в промпт не идёт)
# phrase   — как предмет выглядит; объект ПЕРВЫЙ и крупный (так выживает на живых прогонах)
# acts     — что с ним происходит; подобраны так, чтобы читались в любой локации
OBJECTS: dict[str, dict] = {
    # ── retail: самый сильный дом, предметы-носители чужого мира ──
    "trolley": {
        "home": "retail", "carries": "супермаркет целиком: асфальт, полки, флуорный свет",
        "phrase": "a shopping trolley, wire basket, four castor wheels",
        "acts": [
            "sinking wheel-deep into soft ground, still upright, leaning forward",
            "full of standing rainwater, the wire basket reflecting the sky above it",
            "piled high with wet moss, a colony of it slowly taking over",
            "standing on its last wheel, the frame twisted, one handle snapped off",
        ],
    },
    "shelf": {
        "home": "retail", "carries": "упаковка, ценники, ряды одинаковых банок",
        "phrase": "a steel retail shelf unit, three tiers, mostly empty",
        "acts": [
            "half swallowed by whatever is growing through it, bolts still holding",
            "standing on end, wiped clean in one strip where something slid down it",
            "sagging in the middle, its whole weight slowly going into the ground",
            "with one shelf of identical tins still lined up, perfectly straight",
        ],
    },
    "checkout": {
        "home": "retail", "carries": "касса, разделитель покупок, лента с ценниками",
        "phrase": "a checkout counter, rubber belt, divider bar",
        "acts": [
            "with the belt still turning slowly over nothing, dust lifting off it",
            "half sunk into the floor, the conveyor rollers exposed underneath",
            "the divider bar up, and nothing on the belt either side of it",
            "growing a line of moss exactly along the belt seam",
        ],
    },
    "freezer": {
        "home": "retail", "carries": "холод, запотевшие стенки, упаковки",
        "phrase": "a chest freezer with a sliding glass lid",
        "acts": [
            "frosted solid on the inside, the lid unable to close on its own load",
            "sitting open, its cold still pouring out into the air around it",
            "with a steady column of water running down its side and into the ground",
            "lid cracked open, a perfect square of green growing out of the seam",
        ],
    },
    "barcode": {
        "home": "retail", "carries": "кассовая зона: жёсткий верхний свет, плёнка",
        "phrase": "a chrome checkout desk lamp on a weighted base",
        "acts": [
            "still burning, throwing one hard cone of light and nothing else lit",
            "bent almost double, the base half buried, the shade pointed at the ground",
            "its cord trailing away into the ground and gone, still lit",
            "with a ring of hard shadow exactly where its light lands",
        ],
    },
    "basket": {
        "home": "retail", "carries": "ручная тележка покупателя",
        "phrase": "a stack of plastic shopping baskets, nested",
        "acts": [
            "going soft and fusing together, one shape instead of a stack",
            "sunk in a drift, only the top one still showing its shape",
            "with the nearest one stretched and deformed by something long-term",
            "spilled out across the ground, all of them face-down",
        ],
    },
    "clothingrack": {
        "home": "retail", "carries": "примерочная, зеркало, очередь",
        "phrase": "a rolling clothing rack with a rail and castors",
        "acts": [
            "collapsed sideways, hangers splayed like ribs",
            "still full of empty hangers, swinging a little",
            "the rail bent into a shallow curve by weight it never had",
            "with every hanger turned to face the same wrong way",
        ],
    },
    "crate": {
        "home": "retail", "carries": "ящик, грузчик, погрузочная зона",
        "phrase": "a wooden produce crate, slatted, hand-holed",
        "acts": [
            "full of black water, floating level with the rim",
            "half buried, only the hand-holes and the top rail showing",
            "swelling shut, the slats pressed tight as if still wet",
            "with a ring of green along the waterline where it has been floating",
        ],
    },
    "vending": {
        "home": "retail", "carries": "продажа, монеты, неоновая витрина",
        "phrase": "a coin-operated machine with a glass front, contents long gone",
        "acts": [
            "growing through its own glass, roots pressed against the inside",
            "with one row of bottles still standing, the rest empty",
            "leaning far forward, held up by nothing visible",
            "its coin slot packed solid with something growing in it",
        ],
    },

    # ── domestic: ванна, плита, диван — тоже носители дома ──
    "bathtub": {
        "home": "domestic", "carries": "ванная комната, сантехника, умывальник",
        "phrase": "a full-size bathtub on its own feet, taps still attached",
        "acts": [
            "holding water to the brim, the surface completely still",
            "filled to the top with whatever grows here, blades through the taps",
            "dry and cracked through, a long line of it stained down the inside",
            "slowly filling, the water line still visible and rising",
        ],
    },
    "stove": {
        "home": "domestic", "carries": "кухня, огонь, посуда",
        "phrase": "a cast-iron cooking stove with its doors ajar",
        "acts": [
            "with its firebox still lit, the only warm light for a kilometre",
            "burning through from inside, the doors hanging open on their hinges",
            "with one burner missing, the empty hole full of nest",
            "chimney pipe gone, smoke going straight up through the ceiling hole",
        ],
    },
    "fridge": {
        "home": "domestic", "carries": "холодильник, кухня, свет из щели",
        "phrase": "a refrigerator, door, shelves inside",
        "acts": [
            "still humming, its interior light spilling out across the floor",
            "door ajar, packed solid inside with a green that pushed the shelves",
            "gone over on its side, door hanging open upward",
            "with frost built up around the door seal in one solid ring",
        ],
    },
    "sofa": {
        "home": "domestic", "carries": "гостиная, подушки, отCTV",
        "phrase": "a three-seat sofa with its back cushions still plump",
        "acts": [
            "sunk to the floor until only the arms are above ground",
            "with the cushions still holding the dents where nobody has sat in years",
            "torn open along the seam, its filling spilling out and spreading",
            "growing straight up through the middle of the seat",
        ],
    },
    "bed": {
        "home": "domestic", "carries": "спальня, подушка, простыня",
        "phrase": "a metal hospital-style bed frame, head and foot rails",
        "acts": [
            "the bare frame intact, the mattress rotted to nothing in the middle",
            "standing up on its head end, legs in the air",
            "with a blanket still folded at the foot, untouched",
            "half collapsed, one side folded down to the ground",
        ],
    },
    "kitchencounter": {
        "home": "domestic", "carries": "мойка, плита, посуда",
        "phrase": "a kitchen counter unit with a sink and tiled splashback",
        "acts": [
            "taps still running, the water falling into a drum beneath",
            "the sink full to the brim and not overflowing, perfectly level",
            "with the whole unit pushed flush into the ground, only the taps showing",
            "sink gone solid with something flowering inside it",
        ],
    },
    "wardrobe": {
        "home": "domestic", "carries": "спальня, зеркало, одежда",
        "phrase": "a wardrobe with one door off its hinges",
        "acts": [
            "doors both gone, its contents hung neatly on the rail inside",
            "leaning, held by one hinge, throwing a hard shadow behind it",
            "with the mirror intact and reflecting nothing that is there",
            "its back panel gone, standing as two sides and a roof",
        ],
    },
    "stovepipe": {
        "home": "domestic", "carries": "дымоход, отопление",
        "phrase": "a freestanding cast-iron stove with a flue pipe",
        "acts": [
            "still drawing, smoke pulling straight out of the flue and up",
            "the flue pipe removed, only the collar left flush in the ground",
            "with a bird's nest built in the flue mouth, still in use",
            "rusted through at the base, one leg hanging",
        ],
    },
    "washbasin": {
        "home": "sanitary", "carries": "санузел, кафель, пробка",
        "phrase": "a pedestal washbasin, taps in place",
        "acts": [
            "brim full and still, one drip leaving rings on the surface",
            "the pedestal buried, only the bowl showing above ground",
            "chipped through on one side, the crack grown right through",
            "with one tap still slowly running into the bowl",
        ],
    },
    "toilet": {
        "home": "sanitary", "carries": "санузел, кафель, смыв",
        "phrase": "a floor-standing toilet, cistern, bowl",
        "acts": [
            "dry and full to the seat, its contents long gone to stone",
            "still full to the brim, its lid propped open with a stone",
            "turned to face a wall it was never installed against",
            "with its pipe pulled up and out, the floor open under it",
        ],
    },

    # ── transit: сиденья, которые требуют своего транспорта ──
    "plane_seat": {
        "home": "transit", "carries": "борт самолёта, иллюминатор, вытяжка над головой",
        "phrase": "a single aircraft seat bolted to the floor, seatbelt hanging",
        "acts": [
            "seatbelt still fastened across an empty cushion",
            "swept clean of dust while everything around it is not",
            "tipped back to full recline and left there",
            "with its headrest slowly sinking into the ground under it",
        ],
    },
    "bus_seat": {
        "home": "transit", "carries": "городской автобус, поручень, кондуктор",
        "phrase": "a bus seat on a tube, moquette pattern, grab rail behind",
        "acts": [
            "the grab rail above it bent down to hand height",
            "facing backwards, as if the row had been turned",
            "with a whole row of them, all turned to face the door",
            "the moquette worn through to the foam in the shape of one hand",
        ],
    },
    "stanchion": {
        "home": "transit", "carries": "вагон, поручни, билеты",
        "phrase": "a stainless handrail stanchion with a hanging strap",
        "acts": [
            "still polished bright at the grip, everything else around it weathered",
            "with its strap torn off and only the ring left",
            "bent hard to one side and held there",
            "one of a row of them, all bent the same way",
        ],
    },
    "junctionbox": {
        "home": "transit", "carries": "трамвайная линия, рельс, контакт",
        "phrase": "a tram pantograph and its base frame, complete",
        "acts": [
            "still reaching up, holding the angle it held in service",
            "the contact strip worn to bare metal in one bright band",
            "lying on its side, one spring free, uncoiled",
            "with insulators cracked and gone one by one along the frame",
        ],
    },
    "trafficlight": {
        "home": "civic", "carries": "перекрёсток, поток машин, город",
        "phrase": "a three-lamp traffic signal head hanging from a bracket",
        "acts": [
            "hanging dead level, every lens dark, hoods full of rain",
            "one lens still faintly lit, the bracket cracked above it",
            "turned to face nothing, its back plate toward the open road",
            "swung on its bracket so the whole head is upside down",
        ],
    },
    "barrier": {
        "home": "civic", "carries": "парковка, будка, шлагбаум",
        "phrase": "a red and white parking barrier arm, counterweight end",
        "acts": [
            "still upright in its housing, exactly as it was left",
            "the arm snapped off and lying where it fell, far from the housing",
            "raised and stuck there, a leaf caught under it and gone to brown",
            "with the counterweight box split open and its sand spilled out",
        ],
    },
    "bollard": {
        "home": "civic", "carries": "улица, тротуар, ограничение",
        "phrase": "a cast-iron bollard, domed top, chained to a ring",
        "acts": [
            "still chained to its ring, the chain lying slack on the ground",
            "leaning, its base sheared off clean at ground level",
            "the chain gone and the ring empty, one link welded shut",
            "with the paint worn through on one side only, from always facing the same way",
        ],
    },
    "station_clock": {
        "home": "civic", "carries": "вокзал, расписание, платформа",
        "phrase": "a station platform clock on a bracket, two dials",
        "acts": [
            "the minute and hour hands agree, and the second hand does not",
            "the glass gone, the mechanism open to the weather",
            "still ticking audibly in the empty space around it",
            "turned ninety degrees, face toward the wall behind it",
        ],
    },

    # ── institutional: то, что требует стен, коридоров, людей ──
    "lockers": {
        "home": "institutional", "carries": "раздевалка, номерки, коридор",
        "phrase": "a bank of steel lockers, one door swung wide",
        "acts": [
            "one door wide open and the inside completely bare",
            "all doors open, every interior rusted to the same colour",
            "the row tilted, its feet dug into the ground to stay standing",
            "with the vents stuffed with something growing through from behind",
        ],
    },
    "vaultdoor": {
        "home": "institutional", "carries": "банк, сейф, толщина стали",
        "phrase": "a bank vault door, wheel handle, on a short frame",
        "acts": [
            "the wheel half turned, as if stopped by something solid behind it",
            "swung fully open on a wall that is only dirt",
            "still sealed, its dial showing a combination set long ago",
            "sunk into the ground to the frame, only the wheel above the line",
        ],
    },
    "exam_table": {
        "home": "medical", "carries": "больница, кабинет, лампа",
        "phrase": "an examination table, paper roll, adjustable headrest",
        "acts": [
            "paper roll gone, the bare table weathered from above",
            "headrest raised to the top and locked there",
            "the paper roll still on its spindle, part used, part one long sheet",
            "pushed through the floor at an angle, one leg up",
        ],
    },
    "ivstand": {
        "home": "medical", "carries": "палата, капельница, койка рядом",
        "phrase": "a wheeled IV stand with an empty hook and no drip chamber",
        "acts": [
            "wheels caked in one substance, castors no longer turning",
            "the pole bent into a slow curve near the top",
            "standing dead upright with the hook still taped shut",
            "its tube tied neatly in a loop, the way it was left",
        ],
    },
    "libraryshelf": {
        "home": "institutional", "carries": "читальный зал, тишина, каталог",
        "phrase": "a double-sided library shelf unit, all spines turned inward",
        "acts": [
            "every spine turned in, the shelf correct and completely unreadable",
            "holding one shelf of books felled upright, no gaps at all",
            "leaning forward, its base slowly going into the ground",
            "with the shelf runners exposed and nothing resting on them",
        ],
    },
    "courtroom": {
        "home": "institutional", "carries": "зал суда, трибуна, протокол",
        "phrase": "a judge's bench, high front, worn wood, brass rail",
        "acts": [
            "the brass rail polished bright only along its top edge",
            "sunk into the ground, only the writing surface still level",
            "tipped over forward, the high front facing the ground",
            "with two chairs behind it, both pushed in and square",
        ],
    },

    # ── hospitality ──
    "tableset": {
        "home": "hospitality", "carries": "ресторан, официант, счёт",
        "phrase": "a dining table set for two, laid properly, two chairs",
        "acts": [
            "still laid exactly as it was, glasses untouched, nothing used",
            "one chair pushed back and one pushed in, as if just stood up",
            "the cloth on it gone to the weather, the lay-out still on the wood",
            "set for far more than the chairs that survived",
        ],
    },
    "bell": {
        "home": "hospitality", "carries": "стойка, ресепшн, ожидание",
        "phrase": "a brass reception counter bell on a round base",
        "acts": [
            "the ring still polished, the brass gone dull everywhere else",
            "domed over on one side by a single layer of growth",
            "still on its base, the counter it belonged to long gone",
            "sunk to the rim, the base embedded and the dome proud of the ground",
        ],
    },
    "cloakroom": {
        "home": "hospitality", "carries": "гардероб, номерок, очередь",
        "phrase": "a tiered cloakroom rack, brass hooks, numbered",
        "acts": [
            "one hook empty, its tag still swinging on the string",
            "the whole tier collapsed down into a single flat plane",
            "hooks returned one by one, all of them level, none pulled down",
            "holding one garment, still on the hook, undisturbed",
        ],
    },

    # ── musical ──
    "grandpiano": {
        "home": "musical", "carries": "концертный зал, рояль, три педали",
        "phrase": "a black grand piano, lid, the harp and strings inside",
        "acts": [
            "the lid propped open, the strings gone slack and grey with dust",
            "half sunk into the ground, the legs stopping just short of bearing",
            "with the fallboard closed but the keys uncovered, a skin over them",
            "the pedals still in the down position and frozen there",
        ],
    },
    "upright": {
        "home": "musical", "carries": "гостиная, клавиши, ноты",
        "phrase": "an upright piano against nothing, keys exposed",
        "acts": [
            "the key bed exposed, one key down and the rest in a perfect plane",
            "tipped forward onto its front legs, the lid hanging open",
            "the pedals missing, holes left in the base board",
            "with its top lid resting level, holding one thing only",
        ],
    },
    "jukebox": {
        "home": "musical", "carries": "кафе, неон, монета, очередь",
        "phrase": "a chrome-rimmed jukebox, arch top, selector buttons",
        "acts": [
            "the selector row all in the same position, the last one",
            "tipped onto its back, the arch top resting in the ground",
            "split down the middle, both halves still wired together",
            "holding one bent coin in the mechanism and never giving it back",
        ],
    },
    "drumkit": {
        "home": "musical", "carries": "сцена, репетиция, зал",
        "phrase": "a full drum kit, three toms, cymbals, hardware",
        "acts": [
            "the skins gone, the shells left standing like open cups",
            "one cymbal still slowly settling after being struck",
            "the hardware folded flat, the whole kit a single low heap",
            "upright, assembled and played, waiting in an empty room",
        ],
    },

    # ── agricultural ──
    "beehive": {
        "home": "agri", "carries": "пасека, улей, урожай",
        "phrase": "a stack of bee boxes, wooden, with a lid",
        "acts": [
            "bees working it steadily, coming and going through the entrance",
            "the boxes bulging, combs pushed out through every joint",
            "half collapsed, boxes fallen outward like a spilled deck",
            "empty and dark, the entrance reducer still fitted",
        ],
    },
    "henhouse": {
        "home": "agri", "carries": "ферма, загон, яйца",
        "phrase": "a mobile chicken coop on small wheels, ramp down",
        "acts": [
            "the ramp still down, the door open, the wheel dug in and not going anywhere",
            "perched level on the surface, wheels spinning nothing",
            "with the nesting flap open and nothing inside",
            "half buried, the roof level with the ground",
        ],
    },
    "haybale": {
        "home": "agri", "carries": "поле, уборка, жатка",
        "phrase": "a round hay bale, netting still on",
        "acts": [
            "sunk to its axle in soft ground, netting gone at the bottom",
            "split down the middle, hay spilling out in one long sheet",
            "pushed up out of a hollow that is exactly its own shape",
            "still netted, sprouting evenly from the whole surface",
        ],
    },
    "milkingstool": {
        "home": "agri", "carries": "коровник, дойка, утро",
        "phrase": "a three-legged milking stool, wooden seat",
        "acts": [
            "one leg broken and gone, standing on two and a stone",
            "still squared up to nothing, facing the same way it always faced",
            "the seat worn into a dish on one side only",
            "sunk so only the seat top is above the surface",
        ],
    },
    "trough": {
        "home": "agri", "carries": "поилка, корм, поле",
        "phrase": "a galvanised livestock trough, water in it",
        "acts": [
            "full and still, its surface holding the sky exactly",
            "the water gone, the inside dry and clean at the bottom",
            "tipped and draining, one end buried in the ground",
            "with a rim of green exactly at the old waterline",
        ],
    },

    # ── industrial / civic leftovers ──
    "palletjack": {
        "home": "industrial", "carries": "склад, погрузчик, разметка пола",
        "phrase": "a hand pallet truck, forks and handle, lowered",
        "acts": [
            "the forks still under something, the load above it gone",
            "left mid-floor with the handle leaning, exactly where it was dropped",
            "the forks jammed into the ground, the whole frame tilted up",
            "with the wheel at the end still free to roll, on nothing",
        ],
    },
    "gascylinder": {
        "home": "industrial", "carries": "сварка, баллон, запах газа",
        "phrase": "a gas cylinder on its back, valve and cap chain",
        "acts": [
            "the cap still chained shut, the valve exposed to the weather",
            "lying on its side in a rut, the chain dragging",
            "upright and free-standing, the chain cut clean through",
            "with frost and a wet ring around its foot",
        ],
    },
    "gearbox": {
        "home": "industrial", "carries": "цех, станок, масло",
        "phrase": "a gearbox housing, bolted cover, sight glass",
        "acts": [
            "the bolts undone and laid in a neat line beside it",
            "still oily at the sight glass, the level line unmoved",
            "half sunk, bolts sheared off and lying around it",
            "with its inspection cover off and the interior empty and dry",
        ],
    },
    "conveyor": {
        "home": "industrial", "carries": "завод, линия, шум",
        "phrase": "a short conveyor section on its own legs, belt slack",
        "acts": [
            "the belt slack in the middle, one end turned over on itself",
            "the legs standing in their old bolt holes, nothing bolted",
            "still faintly rocking, the whole section going slowly over",
            "with the rollers seized and the belt going nowhere",
        ],
    },
    "valve": {
        "home": "industrial", "carries": "трубопровод, давление, штуцер",
        "phrase": "a large handwheel valve on a short pipe stub",
        "acts": [
            "the wheel locked open, and the pipe below it full to the brim",
            "the pipe crushed inward, the wheel still rigid",
            "sealed with a wooden plug driven into the pipe mouth",
            "the wheel gone, only the stem and its nut left",
        ],
    },

    # ── sports ──
    "billiards": {
        "home": "sports", "carries": "бильярдный зал, сукно, кий",
        "phrase": "a full-size billiard table, green cloth, six pockets",
        "acts": [
            "the cloth gone furry over the whole bed, rails still true",
            "pocketed with the balls still frozen in a triangle on the spot",
            "the slate lifting at one corner, the bed no longer level",
            "the cloth pulled partly away and stapled back wrong",
        ],
    },
    "startingblocks": {
        "home": "sports", "carries": "беговая дорожка, разметка, секундомер",
        "phrase": "a pair of starting blocks, footplates and rails",
        "acts": [
            "still bolted to their marks, plates set for a start that never came",
            "the rails driven down through the surface they were fixed to",
            "one plate torn off and lying flat beside the other",
            "with the numbers still pressed into the plate faces",
        ],
    },
    "ring": {
        "home": "sports", "carries": "ринг, канаты, зрители",
        "phrase": "a boxing ring corner post, pads and three ropes",
        "acts": [
            "the ropes hanging slack, one cut and lying on the canvas",
            "the corner sagging, all four posts leaning the same way",
            "still roped and squared, the canvas swept bare under it",
            "with the corner pad split open, padding pressed out of it",
        ],
    },
    "swing": {
        "home": "sports", "carries": "двор, качели, качелящийся",
        "phrase": "a garden swing set, A-frame, two chains",
        "acts": [
            "both chains still on, the seat swinging a little in no wind",
            "the A-frame buried to the crossbar, chain slack",
            "one chain on, the other lying coiled in the seat",
            "turned to face away from everything",
        ],
    },

    # ── aviculture: клетки, птицы, воля ──
    "birdcage": {
        "home": "aviculture", "carries": "клетка, корм, вода",
        "phrase": "a domed wire birdcage on a hook, feed cup still inside",
        "acts": [
            "hanging empty, the door swung open and the cup still full",
            "the bird still in it, alive, the whole cage motionless",
            "half buried, the hook still in its bracket",
            "with the feed cup gone and the door tied shut from outside",
        ],
    },
    "carrier": {
        "home": "aviculture", "carries": "перевозка, клетка, дорога",
        "phrase": "a pet carrier, wire front, door latched",
        "acts": [
            "the latch undone and the door swung out",
            "still latched, its base pad soaked through and dry-cracked",
            "sunk to the lid, standing on its own door",
            "with the whole roof gone and only the wire shell left",
        ],
    },
    "incubator": {
        "home": "aviculture", "carries": "вывод, тепло, яйца",
        "phrase": "an egg incubator, glass lid, thermometer on top",
        "acts": [
            "still warm, the glass fogged from inside, nothing moving",
            "open with its tray out, eggs in one neat row",
            "the glass gone, the thermostat dial still set",
            "half buried, its heater element exposed underneath",
        ],
    },

    # ── wild things that do not belong indoors ──
    "hive_frame": {
        "home": "wild", "carries": "пасека, улей, мёд",
        "phrase": "a single wax honeycomb frame, hanging from a wire",
        "acts": [
            "hanging on its own wire, the comb still perfectly level",
            "capped solid, the wax gone white and hard",
            "curled inward at both ends, a shape no comb holds on its own",
            "with bees working the capped surface steadily",
        ],
    },
    "larder": {
        "home": "rural", "carries": "погреб, кладовая, банки",
        "phrase": "a stone cold locket, plank door, iron hasp",
        "acts": [
            "the plank door still shut, the hasp fastened from the inside",
            "door swollen in its frame, and whatever was in it kept",
            "standing open, the shelves inside totally bare",
            "the door propped with a stone and the cold still coming out",
        ],
    },
    "dryingrack": {
        "home": "rural", "carries": "двор, бельё, ветер",
        "phrase": "a wooden washing line A-frame, galvanised wire stretched",
        "acts": [
            "the wire still stretched, holding the weight of its own sag",
            "collapsed flat, the wire snapped and lying beside it",
            "still standing, wire bare, one peg still clamped on",
            "the wire gone entirely, only the frame left",
        ],
    },
    "beehive_skeleton": {
        "home": "wild", "carries": "природа, остов, время",
        "phrase": "a stripped animal skeleton, articulated, legs together",
        "acts": [
            "standing square and complete, holding its own shape entirely",
            "half collapsed, the spine folded where it was propped",
            "the ribs sprung open like a hand, evenly, deliberately",
            "facing away, into the same direction it always faced",
        ],
    },
    "nesthole": {
        "home": "wild", "carries": "гнездо, птица, круг",
        "phrase": "a mud nest with its entrance lip, on a short stump",
        "acts": [
            "the lip worn smooth on one side only, from always landing the same way",
            "empty, the inner bowl still holding the exact shape of what sat in it",
            "built on the crown of the stump, the stump split beneath it",
            "half fallen, still intact, hanging by one side",
        ],
    },
    "antler_shed": {
        "home": "wild", "carries": "рога, линька, олень",
        "phrase": "a shed antler, both beams, burr end intact",
        "acts": [
            "lying where it fell, burr still on, not a mark on the tines",
            "stuck upright in the ground, balanced, still in the same place",
            "half buried, the beam running under and out the other side",
            "held in a fork of the branches, still the way it was left",
        ],
    },
    "beetle_case": {
        "home": "wild", "carries": "насекомое, хитин, цикл",
        "phrase": "a large insect case, hooked onto a twig",
        "acts": [
            "split open along the seam, hollow and light, still gripped to the twig",
            "on its back on flat ground, the legs folded in tight",
            "half buried, the hook end still clear of the surface",
            "closed and whole, and the twig it grips is grown over inside",
        ],
    },
    "rowboat": {
        "home": "rural", "carries": "лодка, причал, вёсла",
        "phrase": "a wooden rowing boat, oars shipped, keel up on blocks",
        "acts": [
            "the oars shipped and wet, blocks driven into the ground under the keel",
            "swamped and full, the oars floating apart inside it",
            "upright on its side, oars gone, the seats still in",
            "the gunwale gone along one whole side, the ribs open to the air",
        ],
    },
    "anvil": {
        "home": "rural", "carries": "кузница, молот, искры",
        "phrase": "a blacksmith anvil, horn and hardy hole, on its stump",
        "acts": [
            "face pitted and bright where the work was always done",
            "sunk into the stump so only the face and horn are above",
            "the stump rotted away, the anvil standing on its own base plate",
            "with the hardy hole plugged and the pritchel gone",
        ],
    },
    "fencepost": {
        "home": "rural", "carries": "забор, граница, поле",
        "phrase": "a split fence post, hand-cut, with two nail holes",
        "acts": [
            "still holding a wire that goes nowhere in either direction",
            "cut off at ground level, the top lying a metre away",
            "leaning at the angle of years, the ground banked on one side",
            "the split driven full of something growing out of the top",
        ],
    },
    "milestone": {
        "home": "civic", "carries": "дорога, граница, расстояние",
        "phrase": "a cast-iron milepost, rounded top, on a short column",
        "acts": [
            "upright and legible, the ground around it built up to its middle",
            "tipped off its base, the column separate a short way off",
            "sunk to the lettering, one shoulder still clear",
            "still standing where the road no longer goes",
        ],
    },
    "mural": {
        "home": "civic", "carries": "город, стена, роспись",
        "phrase": "a tiled nameplate on a wall, no legible characters",
        "acts": [
            "fallen face-up in the dust, backing intact",
            "still set in the wall, half the wall gone around it",
            "clean where the wall around it is not, one rectangle of it",
            "with the cement bed cracked through behind it",
        ],
    },
    "wingfence": {
        "home": "institutional", "carries": "аэропорт, трасса, полоса",
        "phrase": "an airport runway light, in a sealed housing on a short mast",
        "acts": [
            "still lit, one small hard point of light and nothing else",
            "the housing open, the bulb intact and dark",
            "unscrewed from its base, lying beside the empty hole",
            "half sunk, the lens level with the surface",
        ],
    },
    "substation": {
        "home": "industrial", "carries": "подстанция, провод, шум",
        "phrase": "a transformer unit, cooling fins, ceramic bushings on top",
        "acts": [
            "the bushings gone, only the studs left standing",
            "still humming, its whole mass subtly warm",
            "sunk to the plinth, the fins half buried",
            "with a bird's nest filling the gap between two bushings",
        ],
    },
    "switchgear": {
        "home": "industrial", "carries": "щит, рубильник, проводка",
        "phrase": "a cast-iron switchgear cabinet, levers, meter window",
        "acts": [
            "the door open on all four levers thrown to the same position",
            "the window gone, the meter face behind it intact",
            "sunk to its plinth, door swinging on one hinge",
            "with the levers rusted solid and the door fused shut",
        ],
    },
    "seatbelt_rail": {
        "home": "transit", "carries": "автобус, поручень, безопасность",
        "phrase": "a folded pair of bus wheelchairs, strapped upright",
        "acts": [
            "the strap still buckled, nothing in either seat",
            "folded down, wheels level, ready to be pushed",
            "one seat missing its restraint, the other still strapped",
            "with the frames intact and the upholstery gone to frame",
        ],
    },
    "pharmacy_shelf": {
        "home": "retail", "carries": "аптека, рецепт, белые ящики",
        "phrase": "an apothecary drawer rack, many small labelled drawers",
        "acts": [
            "every drawer pulled out a hand's width, nothing inside any",
            "one drawer out further than the rest, and something in it",
            "the whole rack settled, drawers no longer parallel",
            "with the glass fronts fogged and every pull still shiny",
        ],
    },
    "bicycle": {
        "home": "rural", "carries": "дорога, седло, звонок",
        "phrase": "a road bicycle, mudguards, chain on the big cog",
        "acts": [
            "still locked, through its frame, to nothing that holds anything",
            "leant so far forward it rests on its own handlebars",
            "the wheels taken off, propped on its fork and seat",
            "with the pedals level and the whole frame holding its shape",
        ],
    },
    "loom": {
        "home": "rural", "carries": "ткачество, станок, нить",
        "phrase": "a floor loom, warp strung, heddles hanging",
        "acts": [
            "the warp still strung under tension, the shed open",
            "the beater fallen forward, heddles hanging loose",
            "half buried, the frame still square and trued",
            "with one shuttle still threaded and the cloth cut away",
        ],
    },
    "letterbox": {
        "home": "civic", "carries": "улица, почта, адрес",
        "phrase": "a wall-mounted post box, rounded top, hinged slot",
        "acts": [
            "the slot empty, the flap still swinging on its spring",
            "the door unhinged and set back in place as it was",
            "grown into the wall, only the front curve standing proud",
            "full to the brim and the last one still half outside",
        ],
    },
    "sundial": {
        "home": "grand", "carries": "двор, время, камень",
        "phrase": "a stone sundial on a pedestal, gnomon cast",
        "acts": [
            "the gnomon's shadow standing at noon with nothing above it",
            "the face worn so only part of the hour lines still read",
            "the pedestal cracked through and the whole thing sitting level",
            "fallen face-down, the gnomon pointing at the ground",
        ],
    },
    "doormat": {
        "home": "domestic", "carries": "порог, грязь, дом",
        "phrase": "a coir doormat, rubber backing, edges gone",
        "acts": [
            "half sunk, the pile pressed flat in the middle only",
            "the rubber curled up at one corner and not the other",
            "still flat on its backing, and lifted clean of the surface under it",
            "with a worn path through the middle, one direction only",
        ],
    },
    "parasol": {
        "home": "hospitality", "carries": "пляж, кафе, тень",
        "phrase": "a beach parasol, canvas stretched on a pole",
        "acts": [
            "still up, the canvas holding its shape against the wind",
            "the canvas gone, the pole standing and the ribs bare",
            "tipped over and still holding its shape, keeping the ground dry under it",
            "with the pole driven deep and the whole thing leaning off vertical",
        ],
    },
    "radiator": {
        "home": "institutional", "carries": "подстанция, тепло, чугун",
        "phrase": "a cast-iron radiator, valve and pipe tails both ends",
        "acts": [
            "still warm in one section and stone cold everywhere else",
            "the valve off and the pipe tails capped with rags",
            "half sunk into the wall, the brackets pulled out with the plaster",
            "with its sections sheared apart and laid out in a line",
        ],
    },
    "ladder": {
        "home": "industrial", "carries": "высота, стройка, страх",
        "phrase": "an aluminium extension ladder, two sections, ropes",
        "acts": [
            "fully extended and standing square, the ropes slack",
            "the base section sunk and the top section still reaching",
            "folded shut and lying where it fell, feet still on the surface",
            "the ropes cut, the sections no longer able to reach each other",
        ],
    },
    "pallet": {
        "home": "industrial", "carries": "склад, тара, штабель",
        "phrase": "a wooden pallet, three blocks, nail heads showing",
        "acts": [
            "the top deck sprung, a fork still proud of the blocks",
            "sunk level with the surface, only the top deck showing",
            "broken into three pieces laid back out in the same shape",
            "sitting square on a surface that gives it no support",
        ],
    },
    "shower_stall": {
        "home": "sanitary", "carries": "ванная, кафель, слив",
        "phrase": "a tiled shower stall, tray and riser, doors gone",
        "acts": [
            "the tray holding water to the brim, level, not spilling",
            "the tiles intact inside, the tray sunk out of sight",
            "the riser still fitted, the head missing, the pipe capped",
            "growing through the drain hole and out of the tray",
        ],
    },
    "sauna_stove": {
        "home": "sanitary", "carries": "баня, жар, веник",
        "phrase": "a masonry sauna stove with stones on top, iron door",
        "acts": [
            "stones still piled on it, the iron door shut and the whole thing cold",
            "the chimney gone, the smoke finding the top and the stones burnt white",
            "half buried with only the stone crown above the surface",
            "with the iron door standing open and no fire in it at all",
        ],
    },
    "weighbridge": {
        "home": "institutional", "carries": "весы, приёмка, груз",
        "phrase": "a weighbridge platform, checker plate, recessed flush",
        "acts": [
            "the load cells under it gone, the plate not moving at all",
            "half sunk, the plate below grade and the ramp up to it",
            "the platform raised at one corner and jammed there",
            "with the anchor bolts still in place and nothing bolted to them",
        ],
    },
    "aquarium": {
        "home": "aquatic", "carries": "аквариум, вода, стекло",
        "phrase": "a large aquarium tank, still water, gravel bed",
        "acts": [
            "the water long gone, the gravel bed dry and level",
            "still holding clear water, the surface perfectly flat",
            "the glass starred from the inside, water stains down the marks",
            "the gravel gone flat and the tank down to half its depth",
        ],
    },
    "reef_frame": {
        "home": "aquatic", "carries": "риф, кораллы, солёная вода",
        "phrase": "a bare coral reef frame, dead white, arched",
        "acts": [
            "bleached to bone white, every branch still holding its exact shape",
            "grown over at the base and bare at the tip, halfway either way",
            "standing upright and level, the way it grew, now fixed there",
            "with the small marks of growth coming back only on one side",
        ],
    },
    "fish_crate_live": {
        "home": "aquatic", "carries": "живая рыба, лёд, улов",
        "phrase": "an open fish box packed in crushed ice",
        "acts": [
            "the ice long melted, the box dry and the inside stained through",
            "still half full of ice, holding its shape in the heat",
            "the lid off, contents gone, only the wet rings left in the base",
            "sunk to the rim, the ice holding the water level exactly",
        ],
    },
    "anchor_chain": {
        "home": "aquatic", "carries": "якорь, дно, вес, канат",
        "phrase": "a stockless ship anchor, shank and crown, with chain",
        "acts": [
            "the flukes buried, the crown proud, standing on nothing but its own weight",
            "the chain paid out in a heap around it, links still in order",
            "upside down, the crown in the air, and holding its balance",
            "with the shackle open and the chain leading away under the surface",
        ],
    },
    "boilerplate": {
        "home": "industrial", "carries": "судно, море, ржавчина",
        "phrase": "a marine steam boiler, riveted shell, fire door",
        "acts": [
            "the fire door shut, the shell cold, salt across everything",
            "half buried, the riveted seam line still running true",
            "open, with the interior completely grown through",
            "with the gauge glass still fitted and its water level unmoved",
        ],
    },
    "navalgun": {
        "home": "industrial", "carries": "судно, орудие, море",
        "phrase": "a ship's deck gun, shielded, on its training mount",
        "acts": [
            "still trained on its bearing, the training rack in place",
            "the shield folded back, the barrel full of something solid",
            "the mount seized, the barrel not on the bearing it was trained for",
            "with the ammunition lock open and the lock key gone",
        ],
    },
    "capstan": {
        "home": "industrial", "carries": "судно, канат, тяга",
        "phrase": "a mooring capstan, drum, pawl, bar sockets",
        "acts": [
            "the rope still in it, wound and jammed, holding nothing",
            "the pawl dropped, the drum free, the bar sockets empty",
            "the bars shipped and stowed, the capstan holding its line",
            "half sunk, the drum above and the rest of it gone",
        ],
    },
    "piano_wreck": {
        "home": "musical", "carries": "наводнение, инструмент, потеря",
        "phrase": "an upright piano, water-stained, action exposed",
        "acts": [
            "the soundboard split open, every string snapped at the pin block",
            "still square, the keys swollen and stuck at one height",
            "pushed down into the surface with the lid riding the ground",
            "the pedals rusted solid and the bench gone",
        ],
    },
    "hymnal": {
        "home": "institutional", "carries": "церковь, хор, органы",
        "phrase": "a church pew row, book rack under the seat",
        "acts": [
            "the rack under the seat holding one book and no spine showing",
            "the row level, the backs worn smooth on one side only",
            "the ends broken, the middle of the row still joined",
            "tipped forward, the backs resting on the floor",
        ],
    },
    "confessional": {
        "home": "grand", "carries": "церковь, тайна, дерево",
        "phrase": "a confessional box, carved, two doors",
        "acts": [
            "one door open, the other shut and latched from inside",
            "the curtain in the open side still hanging in its folds",
            "sunk to the sill, the carved top above the surface",
            "with the kneeler still out and nothing to kneel on",
        ],
    },
    "organconsole": {
        "home": "grand", "carries": "церковь, трубы, регистры",
        "phrase": "an organ console, two manuals, drawknobs in rows",
        "acts": [
            "one manual's keys gone, the stops left set and the wood exposed",
            "the bench gone and the pedalboard still down",
            "half sunk, the music desk above and the manuals below",
            "with every drawknob pulled out and pushed home again",
        ],
    },
    "choirstall": {
        "home": "grand", "carries": "церковь, дерево, сиденье",
        "phrase": "a carved choir stall, misericord, hinged seat",
        "acts": [
            "the seat still down, the misericord held out on its bracket",
            "the stall panelling gone, only the seat rail and end post left",
            "tipped off its fixings, the whole thing lying face-down",
            "with the seat worn through to the wood in one place",
        ],
    },
    "organpipe": {
        "home": "grand", "carries": "церковь, воздух, труба",
        "phrase": "a rank of organ pipes, mouths open, in a wooden case",
        "acts": [
            "the wind gone, the pipes silent and one still swaying",
            "the case open and the pipes exposed, mouths all facing out",
            "the rank unfooted, the toe holes stopped with wax",
            "with the whole case rotted through behind the front row",
        ],
    },
    "chest_freezer_bank": {
        "home": "institutional", "carries": "лаборатория, холод, этикетка",
        "phrase": "a laboratory freezer, door, dial and thermometer",
        "acts": [
            "still sealed, the dial reading low and steady",
            "the door open and the interior thick with ice, nothing inside",
            "the thermometer gone, the dial set to a temperature nothing can read",
            "sunk to the door line, the dial above the surface",
        ],
    },
    "centrifuge": {
        "home": "medical", "carries": "лаборатория, скорость, ротор",
        "phrase": "a bench centrifuge, lid and bowl",
        "acts": [
            "still closed, the counter reading a speed it has held a long time",
            "the lid off, the rotor exposed and perfectly still",
            "on its side, the bowl opening to the surface",
            "with the counter zeroed and the machine still warm",
        ],
    },
    "mortuary_table": {
        "home": "medical", "carries": "морг, каталка, протокол",
        "phrase": "a mortuary slab, drain channel, castors",
        "acts": [
            "the drain still running, the channel clear and running to the end",
            "the castors braked and the table level, nothing on it",
            "half sunk, the drain channel under the surface and still open",
            "with the drain cover off and nothing but the channel under it",
        ],
    },
    "ironlung": {
        "home": "medical", "carries": "больница, дыхание, металл",
        "phrase": "an iron lung cabinet, porthole windows, leather cuff",
        "acts": [
            "the leather cuff still strapped out and hanging open",
            "the portholes sealed and the bellows flat and slack",
            "half sunk, the windows above and the bellows below",
            "with the bellows at full stretch and held there",
        ],
    },
    "pharmacy_scale": {
        "home": "medical", "carries": "аптека, дозировка, вес",
        "phrase": "a brass counter scale, twin pans, pointer needle",
        "acts": [
            "still balanced, the pointer dead level with both pans up",
            "one pan gone, the arm hanging at a slant",
            "the weights set out in a line beside it in order",
            "with the needle still showing a weight, and the pans empty",
        ],
    },
    "stretcher_wheel": {
        "home": "medical", "carries": "скорая, дорога, каталка",
        "phrase": "an ambulance trolley, folding legs, mattress frame",
        "acts": [
            "the legs folded and the frame raised, standing on its wheels",
            "the mattress gone and the frame still trued and level",
            "sunk to its wheels, the frame level with the surface",
            "with the head end folded down and the frame resting on it",
        ],
    },
}

# ── сборка промпта ───────────────────────────────────────────────────────────

TAIL = ("the object large in frame and plainly out of place here, no people, "
        "no text, no lettering, no numbers, no signs, no labels, no arrows, "
        "no stickers, no posters, no graffiti, no logos, nothing written anywhere "
        "in frame")


def _norm(text: str) -> str:
    return " ".join(str(text).split()).strip().lower()


def object_home(obj: dict) -> str:
    return obj["home"]


def pairs_for(loc_key: str) -> list[tuple[str, str]]:
    """Все пары (location_key, object_key), которые типызация разрешает."""
    loc = LOCATIONS[loc_key]
    banned = set(loc["excludes"])
    return [(loc_key, ok) for ok, ov in OBJECTS.items() if ov["home"] not in banned]


def all_pairs() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for lk in LOCATIONS:
        out.extend(pairs_for(lk))
    return out


def _cursor(seed: int, modulus: int) -> int:
    return seed % modulus if modulus else 0


def prompts(count: int = 8, loc: str = "", seed: int | None = None,
            act_offset: int = 0) -> tuple[list[str], list[tuple[str, str, str, int]]]:
    """Детерминированный набор промптов «локация × предмет × действие».

    Обход: круг по локациям (иначе 8 задач съедают одну локацию целиком), внутри
    локации — круг по парам, разрешённым её типамизацией, внутри пары — круг по
    `acts`. Сдвиг по дню, чтобы соседние прогоны не повторялись, но любой прогон
    воспроизводим по seed.

    Возвращает (промпты, метаданные). Метаданные нужны в manifest.json: по ним
    видно, какая именно тройка ушла в генерацию.
    """
    if count <= 0:
        return [], []
    keys = list(LOCATIONS)
    named = (loc or "").strip()
    if named and named in LOCATIONS:
        keys = [named]
    elif named:
        raise ValueError(f"неизвестная локация '{named}': доступны {sorted(LOCATIONS)}")

    base = date.today().toordinal() if seed is None else int(seed)
    offset = _cursor(base, len(keys))
    keys = keys[offset:] + keys[:offset]

    # Разные сдвиги старта на локацию: пары не должны синхронно повторяться
    # день за днём на одинаковом индексе.
    cursors: dict[str, int] = {}
    for i, k in enumerate(keys):
        pairs = pairs_for(k)
        step = _cursor(base // len(keys) + i, len(pairs)) if pairs else 0
        cursors[k] = step

    out: list[str] = []
    meta: list[tuple[str, str, str, int]] = []
    used: set[tuple[str, str, str]] = set()
    guard = 0
    while len(out) < count and guard < count * 40:
        guard += 1
        progressed = False
        for k in keys:
            if len(out) >= count:
                break
            progressed = True
            loc_d = LOCATIONS[k]
            pairs = pairs_for(k)
            if not pairs:
                continue
            c = cursors[k]
            for attempt in range(len(pairs)):
                lk, ok = pairs[(c + attempt) % len(pairs)]
                obj = OBJECTS[ok]
                acts = obj["acts"]
                ai = (c + attempt + act_offset) % len(acts)
                cand = (lk, ok, _norm(acts[ai]))
                cursors[k] = c + attempt + 1
                if cand in used:
                    continue
                used.add(cand)
                meta.append((lk, ok, cand[2], ai))
                out.append(
                    f"close on {obj['phrase']}, {acts[ai]}, {loc_d['where']}, "
                    f"{loc_d['light']}, {TAIL}")
                break
        if not progressed:
            break
    return out, meta
