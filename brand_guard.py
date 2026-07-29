#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""brand_guard.py — фильтр нарушений бренда в текстовых промптах/брифах/идеях.

Ловит запрещённые паттерны (неон, лица, текст в кадре, вектор/мульт,
апскейл и soft-антипаттерны) до генерации. Правила подтягиваются из
DESIGN.md (frontmatter + секция «Анти-референсы»); при отсутствии файла
работает fail-open на встроенном минимуме.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Встроенные правила (HARD всегда, SOFT — рекомендации)
# ---------------------------------------------------------------------------

Finding = Dict[str, str]
Rules = Dict[str, Any]

_HINTS: Dict[str, str] = {
    "neon": "приглушённый холодный градиент",
    "face": "силуэт со спины, тень, руки",
    "text_in_frame": "чистый кадр, надписи только слоем поверх",
    "vector": "фотографичное движение камеры",
    "upscale": "естественная детализация без HDR/шарпа",
    "lone_figure": "присутствие через след (рука, тень, отражение)",
    "corridor": "ломаная композиция, смещённый горизонт, обрезка",
    "drone": "низкая/средняя камера, ручной/steadicam ритм",
    "psychedelic": "сдержанная цветокоррекция, единый тон",
    "stock": " thrеаtrе of one moment, grain, imperfect light",
    "anti_reference": "убрать анти-референс, держаться бренд-языка",
    "forbidden_motion": "заменить запрещённый motion-приём из DESIGN.md",
}

# Исправление опечатки в stock hint — только русский
_HINTS["stock"] = "живой кадр: зерно, неидеальный свет, без «глянца»"

# (rule, severity, hint_key, list of phrase patterns — already lowercased concepts)
_HARD_PHRASES: List[Tuple[str, str, Sequence[str]]] = [
    (
        "neon",
        "neon",
        (
            "neon",
            "cyberpunk",
            "glowing purple gradient",
            "cyan on dark",
            "неон",
        ),
    ),
    (
        "face",
        "face",
        (
            "face",
            "portrait",
            "close-up face",
            "close up face",
            "eyes looking at camera",
            "smiling",
            # Формы перечислены явно, БЕЗ основы «лиц»: она цепляет «лицензия»
            # и «лицевая сторона» — а про лицензии мы пишем постоянно.
            # «лице» исключено намеренно: правая граница снята, и оно цепляет
            # «лицензия»/«лицевая». Терять «на лице» дешевле, чем ложно ругаться
            # на каждый разговор про лицензии.
            "лицо", "лица", "лицом", "лицах",
            "портрет",      # портрет/портретный/портрета
        ),
    ),
    (
        "text_in_frame",
        "text_in_frame",
        (
            "text",
            "letters",
            "caption",
            "watermark",
            "logo",
            "subtitles",
            "текст",
            "надпись",
            "водяной знак",
            "субтитры",
            "логотип",
        ),
    ),
    (
        "vector",
        "vector",
        (
            "vector animation",
            "cartoon",
            "lottie",
            "flat illustration",
            "anime",
            "vector",
            "мультяшн",
            "мультяшный",
            "мультяшная",
            "мультяшное",
            "мультфильм",
            "вектор",
            "аниме",
        ),
    ),
    (
        "upscale",
        "upscale",
        (
            "upscaled",
            "upscale",
            "sharpened",
            "oversharpened",
            "hdr look",
            "апскейл",
        ),
    ),
]

_SOFT_PHRASES: List[Tuple[str, str, Sequence[str]]] = [
    (
        "lone_figure",
        "lone_figure",
        (
            "lone figure",
            "silhouette standing alone",
            "одинокая фигура",
            "одинокой фигуры",
            "одинокую фигуру",
        ),
    ),
    (
        "corridor",
        "corridor",
        (
            "corridor perspective vanishing point",
            "vanishing point",
            "corridor perspective",
            "коридор",
            "точка схода",
        ),
    ),
    (
        "drone",
        "drone",
        (
            "drone epic flyover",
            "drone flyover",
            "epic flyover",
            "дрон",
        ),
    ),
    (
        "psychedelic",
        "psychedelic",
        (
            "psychedelic kaleidoscope",
            "psychedelic",
            "kaleidoscope",
            "психоделик",
            "психоделика",
            "психоделический",
            "калейдоскоп",
        ),
    ),
    (
        "stock",
        "stock",
        (
            "stock photo look",
            "stock photo",
            "glossy commercial",
            "стоковое фото",
            "глянцевый коммерческий",
        ),
    ),
]

# Встроенный минимум правил при отсутствии DESIGN.md
_BUILTIN_RULES: Rules = {
    "forbidden_motion": [],
    "anti_references": [],
    "palette": {},
}


def _phrase_to_regex(phrase: str) -> re.Pattern[str]:
    """Собрать регистронезависимое regex для фразы (слова/пробелы/дефисы)."""
    parts = re.split(r"[\s\-]+", phrase.strip())
    parts = [re.escape(p) for p in parts if p]
    if not parts:
        return re.compile(r"(?!)")  # never matches
    # гибкие разделители между словами: пробел, дефис, en-dash
    body = r"[\s\-–—]+".join(parts)
    # Слева граница жёсткая, СПРАВА — нет: русский словоизменяемый, и «неон» обязан
    # ловить «неоновый», «лицо» → «лица», «мультяшн» → «мультяшная». С правой границей
    # 29.07 фильтр пропустил половину русского теста, поймав 2 нарушения из 4.
    # Английские фразы от этого не страдают: «face» и так внутри «surface» не всплывёт —
    # левая граница отсекает (перед «face» там буква).
    # НО: без правой границы латинская фраза ловит СВОИ ЖЕ продолжения — «text» бил
    # в «texture» и «context» (поймано 29.07 на реальном LTX-промпте, где «wet asphalt
    # texture» объявили текстом в кадре). У латиницы словоизменения нет, поэтому правую
    # границу возвращаем ей — и только ей. Кириллица остаётся открытой справа.
    tail = "" if re.search(r"[^\W\d_]", phrase, re.ASCII) is None else r"(?!\w)"
    return re.compile(
        rf"(?<![\w]){body}{tail}",
        re.IGNORECASE | re.UNICODE,
    )


def _compile_catalog(
    catalog: Sequence[Tuple[str, str, Sequence[str]]],
    severity: str,
) -> List[Tuple[str, str, str, re.Pattern[str], str]]:
    """(rule, severity, phrase, pattern, hint) для каждого phrase."""
    out: List[Tuple[str, str, str, re.Pattern[str], str]] = []
    for rule, hint_key, phrases in catalog:
        hint = _HINTS.get(hint_key, "")
        for ph in phrases:
            out.append((rule, severity, ph, _phrase_to_regex(ph), hint))
    # длинные фразы первыми — чтобы «close-up face» бил раньше «face»
    out.sort(key=lambda t: len(t[2]), reverse=True)
    return out


_HARD_COMPILED = _compile_catalog(_HARD_PHRASES, "hard")
_SOFT_COMPILED = _compile_catalog(_SOFT_PHRASES, "soft")


# ---------------------------------------------------------------------------
# Парсинг DESIGN.md
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(
    r"\A---\s*\n(.*?)\n---\s*\n?",
    re.DOTALL,
)

_ANTI_SECTION_RE = re.compile(
    r"^##\s*Анти-референсы\s*$",
    re.MULTILINE | re.IGNORECASE,
)


def _parse_simple_yaml_scalar(raw: str) -> Any:
    """Примитивный скаляр YAML: строки, числа, bool, null."""
    s = raw.strip()
    if not s:
        return ""
    if (s.startswith('"') and s.endswith('"')) or (
        s.startswith("'") and s.endswith("'")
    ):
        return s[1:-1]
    low = s.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "~", "none"):
        return None
    try:
        if re.fullmatch(r"-?\d+", s):
            return int(s)
        if re.fullmatch(r"-?\d+\.\d+", s):
            return float(s)
    except ValueError:
        pass
    # inline list: [a, b, c]
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        if not inner:
            return []
        return [_parse_simple_yaml_scalar(x) for x in _split_csv(inner)]
    # inline map: {a: 1, b: 2}
    if s.startswith("{") and s.endswith("}"):
        return _parse_inline_map(s[1:-1])
    return s


def _split_csv(inner: str) -> List[str]:
    """Разбить по запятым с учётом кавычек."""
    items: List[str] = []
    buf: List[str] = []
    in_q: Optional[str] = None
    for ch in inner:
        if in_q:
            buf.append(ch)
            if ch == in_q:
                in_q = None
            continue
        if ch in ('"', "'"):
            in_q = ch
            buf.append(ch)
            continue
        if ch == ",":
            items.append("".join(buf).strip())
            buf = []
            continue
        buf.append(ch)
    if buf or items:
        items.append("".join(buf).strip())
    return [x for x in items if x != ""]


def _parse_inline_map(inner: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for part in _split_csv(inner):
        if ":" not in part:
            continue
        k, v = part.split(":", 1)
        result[k.strip()] = _parse_simple_yaml_scalar(v)
    return result


def _parse_frontmatter(block: str) -> Dict[str, Any]:
    """Разобрать YAML-frontmatter регулярками (без PyYAML).

    Поддерживает: ключ: значение, вложенность по отступам, списки `- item`,
    inline [ ] и { }.
    """
    lines = block.splitlines()
    root: Dict[str, Any] = {}
    # стек: (indent, container, key_in_parent_or_None_for_list)
    stack: List[Tuple[int, Any, Optional[str]]] = [( -1, root, None )]

    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i]
        i += 1
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        # отступ = число ведущих пробелов (таб = 2)
        expanded = raw.replace("\t", "  ")
        indent = len(expanded) - len(expanded.lstrip(" "))
        content = expanded.strip()

        # всплыть до нужного уровня
        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()

        parent = stack[-1][1]

        # элемент списка
        if content.startswith("- "):
            item_raw = content[2:].strip()
            if isinstance(parent, list):
                target_list = parent
            else:
                # список без явного ключа — не должно, пропуск
                continue

            if item_raw == "" or item_raw.endswith(":"):
                # `- key:` или `-` → вложенный dict
                key_only = item_raw[:-1].strip() if item_raw.endswith(":") else None
                new_obj: Dict[str, Any] = {}
                if key_only:
                    # `- name:` странный кейс; трактуем как dict-элемент {name: ...}
                    # чаще: `- item` с вложенными полями → dict
                    new_obj = {}
                    target_list.append(new_obj)
                    stack.append((indent, new_obj, None))
                    if key_only:
                        # оставим пустым, следующие indented keys заполнят
                        pass
                else:
                    target_list.append(new_obj)
                    stack.append((indent, new_obj, None))
                continue

            if ":" in item_raw and not (
                item_raw.startswith("[") or item_raw.startswith("{")
            ):
                # `- key: value` → dict-элемент списка
                k, v = item_raw.split(":", 1)
                k, v = k.strip(), v.strip()
                if v == "":
                    nested: Dict[str, Any] = {}
                    target_list.append({k: nested})
                    # следующий уровень пишет в nested — упрощённо кладём dict
                    d = {k: nested}
                    # заменим последний
                    target_list[-1] = d
                    stack.append((indent, nested, k))
                else:
                    target_list.append({k: _parse_simple_yaml_scalar(v)})
                continue

            target_list.append(_parse_simple_yaml_scalar(item_raw))
            continue

        # ключ: значение
        if ":" in content:
            key, rest = content.split(":", 1)
            key, rest = key.strip(), rest.strip()
            if not isinstance(parent, dict):
                continue
            if rest == "":
                # смотрим следующую непустую строку — list или map
                j = i
                child: Any = {}
                while j < n:
                    peek = lines[j]
                    j += 1
                    if not peek.strip() or peek.lstrip().startswith("#"):
                        continue
                    peek_exp = peek.replace("\t", "  ")
                    peek_indent = len(peek_exp) - len(peek_exp.lstrip(" "))
                    peek_c = peek_exp.strip()
                    if peek_indent > indent and peek_c.startswith("- "):
                        child = []
                    break
                parent[key] = child
                stack.append((indent, child, key))
            else:
                parent[key] = _parse_simple_yaml_scalar(rest)
            continue

    return root


def _extract_list_items(section_body: str) -> List[str]:
    """Вытащить пункты списка / строки из секции markdown."""
    items: List[str] = []
    for line in section_body.splitlines():
        s = line.strip()
        if not s:
            # пустая строка внутри — продолжаем, пока не встретим новый ##
            continue
        if s.startswith("##"):
            break
        m = re.match(r"^[-*+]\s+(.+)$", s)
        if m:
            items.append(m.group(1).strip().strip("*_"))
            continue
        m = re.match(r"^\d+[.)]\s+(.+)$", s)
        if m:
            items.append(m.group(1).strip().strip("*_"))
            continue
        # жирная строка-заголовок подпункта пропускаем, обычный текст — берём
        if s.startswith("#"):
            break
        if not s.startswith(">"):
            # однострочные анти-референсы без маркера
            if len(s) > 2 and not s.endswith(":"):
                items.append(s.strip("*_"))
    return items


def _parse_anti_references(md_body: str) -> List[str]:
    """Секция ## Анти-референсы → список строк."""
    m = _ANTI_SECTION_RE.search(md_body)
    if not m:
        # альтернативные заголовки
        alt = re.search(
            r"^##\s*Anti[- ]?references?\s*$",
            md_body,
            re.MULTILINE | re.IGNORECASE,
        )
        if not alt:
            return []
        m = alt
    rest = md_body[m.end() :]
    # обрезать до следующего h2
    stop = re.search(r"^##\s+", rest, re.MULTILINE)
    if stop:
        rest = rest[: stop.start()]
    return _extract_list_items(rest)


def _flatten_forbidden(motion: Any) -> List[str]:
    """Достать motion.forbidden (list) и опционально grade."""
    if not isinstance(motion, dict):
        return []
    forbidden = motion.get("forbidden", [])
    result: List[str] = []
    if isinstance(forbidden, list):
        for item in forbidden:
            if isinstance(item, str):
                result.append(item)
            elif isinstance(item, dict):
                # {name: ...} / {phrase: ...} / одноключевой
                for v in item.values():
                    if isinstance(v, str):
                        result.append(v)
            else:
                result.append(str(item))
    elif isinstance(forbidden, str):
        result.append(forbidden)
    return result


def _palette_from_fm(fm: Dict[str, Any]) -> Dict[str, Any]:
    """Собрать palette из colors / typography frontmatter."""
    palette: Dict[str, Any] = {}
    colors = fm.get("colors")
    if isinstance(colors, dict):
        palette["colors"] = colors
    elif isinstance(colors, list):
        palette["colors"] = colors
    elif isinstance(colors, str):
        palette["colors"] = colors
    typo = fm.get("typography")
    if typo is not None:
        palette["typography"] = typo
    if "name" in fm:
        palette["name"] = fm["name"]
    if "version" in fm:
        palette["version"] = fm["version"]
    motion = fm.get("motion")
    if isinstance(motion, dict) and "grade" in motion:
        palette["motion_grade"] = motion["grade"]
    return palette


def _find_design_md(explicit: Optional[str] = None) -> Optional[Path]:
    """Найти DESIGN.md: явный путь, рядом со скриптом/cwd, до 2 уровней вверх."""
    if explicit:
        p = Path(explicit).expanduser().resolve()
        return p if p.is_file() else None

    starts: List[Path] = []
    try:
        starts.append(Path(__file__).resolve().parent)
    except NameError:
        pass
    starts.append(Path.cwd().resolve())

    seen: set[Path] = set()
    for start in starts:
        cur = start
        for _ in range(3):  # 0 = рядом, 1 и 2 = вверх
            if cur in seen:
                break
            seen.add(cur)
            candidate = cur / "DESIGN.md"
            if candidate.is_file():
                return candidate
            if cur.parent == cur:
                break
            cur = cur.parent
    return None


def load_rules(design_path: Optional[str] = None) -> Rules:
    """Прочитать DESIGN.md и вернуть словарь правил бренда.

    Ищет файл рядом со скриптом/cwd и до двух уровней вверх.
    Fail-open: нет файла → встроенный минимум + предупреждение в stderr.

    Returns:
        {
            'forbidden_motion': [...],
            'anti_references': [...],
            'palette': {...},
        }
    """
    path = _find_design_md(design_path)
    if path is None:
        print(
            "brand_guard: DESIGN.md не найден — использую встроенный минимум правил",
            file=sys.stderr,
        )
        return {
            "forbidden_motion": list(_BUILTIN_RULES["forbidden_motion"]),
            "anti_references": list(_BUILTIN_RULES["anti_references"]),
            "palette": dict(_BUILTIN_RULES["palette"]),
        }

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(
            f"brand_guard: не удалось прочитать {path}: {exc} — встроенный минимум",
            file=sys.stderr,
        )
        return {
            "forbidden_motion": list(_BUILTIN_RULES["forbidden_motion"]),
            "anti_references": list(_BUILTIN_RULES["anti_references"]),
            "palette": dict(_BUILTIN_RULES["palette"]),
        }

    fm: Dict[str, Any] = {}
    body = text
    m = _FRONTMATTER_RE.match(text)
    if m:
        try:
            fm = _parse_frontmatter(m.group(1))
        except Exception as exc:  # noqa: BLE001 — fail-open
            print(
                f"brand_guard: ошибка frontmatter ({exc}) — motion/palette пустые",
                file=sys.stderr,
            )
            fm = {}
        body = text[m.end() :]

    forbidden = _flatten_forbidden(fm.get("motion"))
    anti = _parse_anti_references(body)
    palette = _palette_from_fm(fm)

    return {
        "forbidden_motion": forbidden,
        "anti_references": anti,
        "palette": palette,
    }


# ---------------------------------------------------------------------------
# Проверка и очистка
# ---------------------------------------------------------------------------

def _iter_dynamic_patterns(
    phrases: Iterable[str],
    rule: str,
    severity: str,
    hint_key: str,
) -> List[Tuple[str, str, str, re.Pattern[str], str]]:
    compiled: List[Tuple[str, str, str, re.Pattern[str], str]] = []
    hint = _HINTS.get(hint_key, _HINTS.get(rule, ""))
    for ph in phrases:
        ph = str(ph).strip()
        if not ph:
            continue
        compiled.append((rule, severity, ph, _phrase_to_regex(ph), hint))
    compiled.sort(key=lambda t: len(t[2]), reverse=True)
    return compiled


def _scan(
    text: str,
    patterns: Sequence[Tuple[str, str, str, re.Pattern[str], str]],
    seen_spans: Optional[List[Tuple[int, int]]] = None,
) -> List[Finding]:
    """Найти совпадения; не дублировать перекрывающиеся span'ы."""
    findings: List[Finding] = []
    spans = seen_spans if seen_spans is not None else []
    for rule, severity, _phrase, cre, hint in patterns:
        for m in cre.finditer(text):
            span = m.span()
            if any(not (span[1] <= a or span[0] >= b) for a, b in spans):
                continue
            spans.append(span)
            findings.append(
                {
                    "rule": rule,
                    "severity": severity,
                    "match": m.group(0),
                    "hint": hint,
                }
            )
    return findings


def check(text: str, rules: Optional[Rules] = None) -> List[Finding]:
    """Проверить текст на нарушения бренда.

    Args:
        text: промпт / бриф / идея.
        rules: результат load_rules(); если None — загрузится автоматически.

    Returns:
        Список находок {'rule', 'severity', 'match', 'hint'}.
    """
    if rules is None:
        rules = load_rules()

    findings: List[Finding] = []
    spans: List[Tuple[int, int]] = []

    # HARD встроенные
    findings.extend(_scan(text, _HARD_COMPILED, spans))
    # forbidden_motion из DESIGN.md → hard
    fm_patterns = _iter_dynamic_patterns(
        rules.get("forbidden_motion") or [],
        rule="forbidden_motion",
        severity="hard",
        hint_key="forbidden_motion",
    )
    findings.extend(_scan(text, fm_patterns, spans))
    # anti_references → hard (явный запрет бренда)
    ar_patterns = _iter_dynamic_patterns(
        rules.get("anti_references") or [],
        rule="anti_reference",
        severity="hard",
        hint_key="anti_reference",
    )
    findings.extend(_scan(text, ar_patterns, spans))
    # SOFT встроенные
    findings.extend(_scan(text, _SOFT_COMPILED, spans))

    return findings


def clean(text: str, rules: Optional[Rules] = None) -> Tuple[str, List[Finding]]:
    """Вырезать hard-фразы из текста.

    Returns:
        (очищенный_текст, список_находок) — находки те же, что у check(),
        но из текста удаляются только severity=='hard'.
    """
    if rules is None:
        rules = load_rules()

    findings = check(text, rules)
    hard_matches = [f["match"] for f in findings if f.get("severity") == "hard"]
    # длинные первыми
    hard_matches = sorted(set(hard_matches), key=len, reverse=True)

    cleaned = text
    for match in hard_matches:
        # удаляем все вхождения этой точной подстроки (как поймали)
        cre = _phrase_to_regex(match)
        cleaned = cre.sub(" ", cleaned)

    # схлопнуть пробелы, сохранить переводы строк грубо
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r" ?\n ?", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    cleaned = cleaned.strip(" \t")
    # убрать висячие запятые/точки удвоенные после вырезания
    cleaned = re.sub(r"[ \t]*,[ \t]*,", ",", cleaned)
    cleaned = re.sub(r"\(\s*\)", "", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()

    return cleaned, findings


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

_RULE_TITLES_RU: Dict[str, str] = {
    "neon": "неон / cyberpunk-свечение",
    "face": "лицо / портрет",
    "text_in_frame": "текст в кадре",
    "vector": "вектор / мультфильм / flat",
    "upscale": "апскейл / oversharpen / HDR look",
    "lone_figure": "одинокая фигура",
    "corridor": "коридорная перспектива / vanishing point",
    "drone": "дрон / epic flyover",
    "psychedelic": "психоделика / калейдоскоп",
    "stock": "стоковый / glossy commercial вид",
    "anti_reference": "анти-референс из DESIGN.md",
    "forbidden_motion": "запрещённый motion из DESIGN.md",
}


def _format_human(findings: List[Finding]) -> str:
    if not findings:
        return "Нарушений не найдено."
    lines: List[str] = []
    hard_n = sum(1 for f in findings if f["severity"] == "hard")
    soft_n = len(findings) - hard_n
    lines.append(
        f"Найдено нарушений: {len(findings)} "
        f"(жёстких: {hard_n}, мягких: {soft_n})"
    )
    lines.append("")
    for i, f in enumerate(findings, 1):
        title = _RULE_TITLES_RU.get(f["rule"], f["rule"])
        sev = "ЖЁСТКОЕ" if f["severity"] == "hard" else "мягкое"
        lines.append(f"{i}. [{sev}] {title}")
        lines.append(f"   совпадение: «{f['match']}»")
        if f.get("hint"):
            lines.append(f"   замена: {f['hint']}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _read_input(args: argparse.Namespace) -> str:
    if args.text is not None:
        return args.text
    if args.file is not None:
        return Path(args.file).read_text(encoding="utf-8")
    if not sys.stdin.isatty():
        return sys.stdin.read()
    print(
        "Укажите --text, --file или передайте текст через stdin.",
        file=sys.stderr,
    )
    sys.exit(2)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI: проверка текста брифа/промпта на бренд-нарушения."""
    parser = argparse.ArgumentParser(
        prog="brand_guard",
        description=(
            "Фильтр нарушений бренда в тексте (промпт/бриф/идея) до генерации."
        ),
    )
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--text", "-t", type=str, help="Текст для проверки")
    src.add_argument("--file", "-f", type=str, help="Путь к файлу с текстом")
    parser.add_argument(
        "--design",
        type=str,
        default=None,
        help="Путь к DESIGN.md (иначе автопоиск)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Вывод находок в JSON",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Код выхода 1 при наличии hard-нарушений",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        dest="do_clean",
        help="Вырезать hard-фразы и напечатать очищенный текст",
    )
    parser.add_argument(
        "--rules-dump",
        action="store_true",
        help="Показать загруженные правила и выйти",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    rules = load_rules(args.design)

    if args.rules_dump:
        if args.json:
            print(json.dumps(rules, ensure_ascii=False, indent=2))
        else:
            print("forbidden_motion:")
            for x in rules.get("forbidden_motion") or []:
                print(f"  - {x}")
            print("anti_references:")
            for x in rules.get("anti_references") or []:
                print(f"  - {x}")
            print("palette:")
            print(json.dumps(rules.get("palette") or {}, ensure_ascii=False, indent=2))
        return 0

    text = _read_input(args)

    if args.do_clean:
        cleaned, findings = clean(text, rules)
        if args.json:
            print(
                json.dumps(
                    {"text": cleaned, "findings": findings},
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            if findings:
                sys.stderr.write(_format_human(findings))
            print(cleaned)
    else:
        findings = check(text, rules)
        if args.json:
            print(json.dumps(findings, ensure_ascii=False, indent=2))
        else:
            sys.stdout.write(_format_human(findings))

    if args.strict and any(f.get("severity") == "hard" for f in findings):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
