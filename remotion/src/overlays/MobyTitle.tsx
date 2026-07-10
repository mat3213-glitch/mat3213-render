import React from 'react';
import {AbsoluteFill, useCurrentFrame, useVideoConfig, interpolate, Easing} from 'remotion';
import {OverlayProps} from '../overlay_contract';
import {TITLE_FONT} from '../fonts';

/**
 * MobyTitle — сдержанный liner-note титр в регистре ранних клипов Moby (Play-эра):
 * плоская 2D-типографика поверх зернистого кадра, как подпись на обороте пластинки.
 * НЕ хук-всплеск: имя бренда строчными + трек мельче + тонкая линейка-hairline,
 * медленный fade-in + микро-дрейф вверх, тихий «разъезд» линейки. Альфа-композит
 * (прозрачный фон, ProRes 4444) — не красить непрозрачный фон. Без неона.
 *
 * Бренд `yaromat` зафиксирован (брендбук: строчными, крупнее трека).
 * accentText (опц.) = название трека; пусто → только имя+линейка.
 */
export const MobyTitle: React.FC<OverlayProps> = ({
  palette,
  durationSec,
  accentText,
}) => {
  const frame = useCurrentFrame();
  const {fps, width, height} = useVideoConfig();
  const total = Math.round((durationSec || 2) * fps);

  const ink = palette[0] ?? '#e8e2d6'; // тёплый off-white — имя
  const muted = palette[1] ?? '#cfd6dd'; // холодный серый — трек/линейка

  const ease = Easing.bezier(0.16, 1, 0.3, 1); // мягкий settle, без овершута

  // общий fade-in (первые ~0.8с) + fade-out (последние ~0.5с)
  const fadeIn = interpolate(frame, [0, Math.round(fps * 0.8)], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: ease,
  });
  const fadeOut = interpolate(frame, [total - Math.round(fps * 0.5), total], [1, 0], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });
  const groupOpacity = Math.min(fadeIn, fadeOut);

  // микро-дрейф вверх (+14px → 0) за ~1.2с — дыхание, не движение
  const driftY = interpolate(frame, [0, Math.round(fps * 1.2)], [14, 0], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: ease,
  });

  // тихий «разъезд» hairline-линейки (0 → 100%) за ~1.0с
  const ruleGrow = interpolate(frame, [Math.round(fps * 0.2), Math.round(fps * 1.2)], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: ease,
  });

  const sideMargin = Math.round(width * 0.085); // безопасное поле liner-note
  const brandSize = Math.round(width * 0.078); // имя крупнее
  const trackSize = Math.round(width * 0.036); // трек мельче

  return (
    <AbsoluteFill>
      <div
        style={{
          position: 'absolute',
          left: sideMargin,
          bottom: Math.round(height * 0.14),
          opacity: groupOpacity,
          translate: `0px ${driftY}px`,
          fontFamily: TITLE_FONT,
          textAlign: 'left',
        }}
      >
        <div
          style={{
            fontSize: brandSize,
            lineHeight: 1.0,
            fontWeight: 600,
            color: ink,
            letterSpacing: '-0.005em',
          }}
        >
          yaromat
        </div>

        <div
          style={{
            height: 1,
            width: `${ruleGrow * 100}%`,
            maxWidth: Math.round(width * 0.42),
            marginTop: Math.round(brandSize * 0.28),
            marginBottom: Math.round(brandSize * 0.24),
            background: muted,
            opacity: 0.6,
          }}
        />

        {accentText ? (
          <div
            style={{
              fontSize: trackSize,
              lineHeight: 1.2,
              color: muted,
              letterSpacing: '0.04em',
              opacity: 0.9,
            }}
          >
            {accentText}
          </div>
        ) : null}
      </div>
    </AbsoluteFill>
  );
};
