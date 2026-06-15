import React from 'react';
import {AbsoluteFill, useCurrentFrame, useVideoConfig, interpolate, random} from 'remotion';
import {OverlayProps} from '../overlay_contract';

/**
 * InnerScratch — «слой внутреннего монолога»: абстрактные рукописные штрихи-мысли,
 * просвечивающие как неровный почерк на коже. НЕ читается — ощущается.
 * Низкая прозрачность, дрейф, дыхание. Без непрозрачного фона (альфа-композит).
 */
export const InnerScratch: React.FC<OverlayProps> = ({
  seed,
  palette,
  durationSec,
  accentText,
}) => {
  const frame = useCurrentFrame();
  const {fps, width, height} = useVideoConfig();
  const total = Math.round((durationSec || 2) * fps);
  const accent = palette[0] ?? '#e8e2d6';

  const strokeCount = 8 + Math.floor(random(seed) * 7); // 8–14

  const globalDrift = interpolate(frame, [0, total], [0, -height * 0.025], {
    extrapolateRight: 'clamp',
  });

  const globalFadeOut = interpolate(frame, [total - Math.round(fps * 0.5), total], [1, 0], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
  });

  const strokes: React.ReactNode[] = [];
  for (let i = 0; i < strokeCount; i++) {
    const r = (offset: number) => random(seed + i * 7 + offset);

    // позиция: сгущение к центру-низу
    const baseX = r(0) * width;
    const baseY = height * 0.4 + r(1) * height * 0.55;
    const cx = baseX + (r(2) - 0.5) * width * 0.3;
    const cy = baseY + (r(3) - 0.5) * height * 0.15;

    const angle = (r(4) - 0.5) * 24; // ±12°
    const len = 30 + r(5) * 90;
    const strokeW = 1.5 + r(6) * 1.0;

    // начало и конец штриха с лёгкой кривизной
    const dx = Math.cos((angle * Math.PI) / 180) * len;
    const dy = Math.sin((angle * Math.PI) / 180) * len;
    const cpx = dx * 0.4 + (r(7) - 0.5) * 20;
    const cpy = dy * 0.4 + (r(8) - 0.5) * 20;
    const d = `M0,0 Q${cpx},${cpy} ${dx},${dy}`;

    // opacity штриха 0.12–0.34
    const strokeOpacity = 0.12 + r(9) * 0.22;

    // stagger: каждый штрих появляется в своё время
    const appearStart = Math.floor(r(10) * total * 0.6);
    const appearEnd = appearStart + Math.floor(fps * 0.4);
    const appear = interpolate(frame, [appearStart, appearEnd], [0, 1], {
      extrapolateLeft: 'clamp',
      extrapolateRight: 'clamp',
    });

    // индивидуальный дрейф
    const driftX = interpolate(frame, [0, total], [0, (r(11) - 0.5) * 12], {
      extrapolateRight: 'clamp',
    });
    const driftY = interpolate(frame, [0, total], [0, (r(12) - 0.5) * 8], {
      extrapolateRight: 'clamp',
    });

    // дыхание
    const breathe = 1 + Math.sin(frame / fps * 1.5 + r(13) * Math.PI * 2) * 0.03;

    strokes.push(
      <path
        key={i}
        d={d}
        fill="none"
        stroke={accent}
        strokeWidth={strokeW}
        strokeLinecap="round"
        strokeOpacity={strokeOpacity * appear * globalFadeOut}
        transform={`translate(${cx + driftX}, ${cy + driftY}) rotate(${angle * 0.3}) scale(${breathe})`}
      />,
    );
  }

  // опц. accentText как ещё один штрих
  const textNode = accentText ? (() => {
    const tx = random(seed + 99) * width;
    const ty = height * 0.5 + random(seed + 100) * height * 0.4;
    const textAppear = interpolate(frame, [total * 0.3, total * 0.5], [0, 1], {
      extrapolateLeft: 'clamp',
      extrapolateRight: 'clamp',
    });
    const textDrift = interpolate(frame, [0, total], [0, -6], {extrapolateRight: 'clamp'});
    const slice = accentText.slice(0, 3 + Math.floor(random(seed + 101) * 5));
    return (
      <text
        x={tx}
        y={ty + textDrift}
        fill={accent}
        fontSize={14 + random(seed + 102) * 10}
        fontFamily="monospace"
        opacity={0.2 * textAppear * globalFadeOut}
        letterSpacing={2 + random(seed + 103) * 6}
        transform={`rotate(${(random(seed + 104) - 0.5) * 10})`}
      >
        {slice}
      </text>
    );
  })() : null;

  return (
    <AbsoluteFill>
      <svg
        width={width}
        height={height}
        style={{transform: `translateY(${globalDrift}px)`}}
      >
        {strokes}
        {textNode}
      </svg>
    </AbsoluteFill>
  );
};
