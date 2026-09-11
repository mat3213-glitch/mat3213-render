import {loadFont as loadGaramond} from '@remotion/google-fonts/EBGaramond';
import {loadFont as loadCaveat} from '@remotion/google-fonts/Caveat';
import {loadFont as loadManrope} from '@remotion/google-fonts/Manrope';
import {loadFont as loadFraunces} from '@remotion/google-fonts/Fraunces';
import {loadFont as loadMarckScript} from '@remotion/google-fonts/MarckScript';
import {loadFont as loadSpaceMono} from '@remotion/google-fonts/SpaceMono';

/**
 * Брендовые шрифты через @remotion/google-fonts — официальный загрузчик Remotion:
 * корректно держит загрузку через весь рендер (включая многопоточную concurrency),
 * версия пиннута пакетом → детерминированно. Не ЯД — npm-зависимость (на GH).
 *
 * Кастомные (НЕ гугл) шрифты — класть .ttf в public/fonts/ и грузить через @remotion/fonts.
 */
const {fontFamily: GARAMOND} = loadGaramond();  // тёплый литературный serif (liner-note à la Moby)
const {fontFamily: CAVEAT} = loadCaveat();       // рукописный бренд-акцент
const {fontFamily: MANROPE} = loadManrope();     // строгий современный sans
const {fontFamily: FRAUNCES} = loadFraunces();   // тёплый литературный serif с характером
const {fontFamily: MARCK_SCRIPT} = loadMarckScript(); // рукописный «обложка пластинки»
const {fontFamily: SPACE_MONO} = loadSpaceMono();     // моноширинная «машинистка»

export const TITLE_FONT = `${GARAMOND}, Georgia, serif`;
export const HAND_FONT = `${CAVEAT}, cursive`;

/** Кандидаты для titleFont (оверлеи): ключ → font-family.
 *  Боевой дефолт остаётся TITLE_FONT; ключи перечислены в порядке проб. */
export const TITLE_FONT_CANDIDATES: Record<string, string> = {
  sans: `${MANROPE}, 'Helvetica Neue', Arial, sans-serif`,
  serif: `${FRAUNCES}, Georgia, serif`,
  hand: `${MARCK_SCRIPT}, Caveat, cursive`,
  mono: `${SPACE_MONO}, 'Courier New', monospace`,
};

/** Доступные titleFont-ключи для workflow_dispatch. */
export const TITLE_FONT_KEYS = Object.keys(TITLE_FONT_CANDIDATES);