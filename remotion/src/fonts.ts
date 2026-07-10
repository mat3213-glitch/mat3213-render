import {loadFont as loadGaramond} from '@remotion/google-fonts/EBGaramond';
import {loadFont as loadCaveat} from '@remotion/google-fonts/Caveat';

/**
 * Брендовые шрифты через @remotion/google-fonts — официальный загрузчик Remotion:
 * корректно держит загрузку через весь рендер (включая многотабовую concurrency),
 * версия пиннута пакетом → детерминированно. Не ЯД — npm-зависимость (на GH).
 *
 * Кастомные (НЕ гугл) шрифты — класть .ttf в public/fonts/ и грузить через @remotion/fonts.
 */
const {fontFamily: GARAMOND} = loadGaramond();  // тёплый литературный serif (liner-note à la Moby)
const {fontFamily: CAVEAT} = loadCaveat();       // рукописный бренд-акцент

export const TITLE_FONT = `${GARAMOND}, Georgia, serif`;
export const HAND_FONT = `${CAVEAT}, cursive`;
