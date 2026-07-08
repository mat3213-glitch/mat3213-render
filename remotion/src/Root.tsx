import React from 'react';
import {Composition} from 'remotion';
import {templatePropsSchema, TemplateProps, FPS, PREVIEW_SEC, FORMAT_DIMS} from './contract';
import {overlayPropsSchema, OverlayProps} from './overlay_contract';
import {KineticCard} from './templates/KineticCard';
import {KineticWords} from './templates/KineticWords';
import {FocusBracket} from './overlays/FocusBracket';
import {ShapeWipe} from './overlays/ShapeWipe';
import {AccentBurst} from './overlays/AccentBurst';
import {BeatPulse} from './overlays/BeatPulse';
import {InnerScratch} from './overlays/InnerScratch';
import {MobyTitle} from './overlays/MobyTitle';

/**
 * Реестр шаблонов. mimo ДОБАВЛЯЕТ сюда РОВНО одну строку на свой новый шаблон
 * (import выше + объект в массиве). Это единственное место вне templates/, куда он пишет.
 */
const TEMPLATES: {id: string; component: React.FC<TemplateProps>}[] = [
  {id: 'KineticCard', component: KineticCard},
  {id: 'KineticWords', component: KineticWords},
];

const DEFAULT_PROPS: TemplateProps = {
  artUrl: 'testart.jpg',
  trackTitle: 'sky is in my hands',
  brand: 'yaromat',
  palette: ['#0b0d10', '#1a2230', '#cfd6dd'],
  seed: 42,
  format: 'vertical',
};

/**
 * ОВЕРЛЕИ — графические хуки с альфой (à la CapCut). Рендерятся прозрачными
 * (ProRes 4444), композитятся поверх клипа. mimo добавляет сюда новые так же —
 * одна строка (import + объект). Это ОСНОВНОЕ применение TSX (не полнокадровый текст).
 */
const OVERLAYS: {id: string; component: React.FC<OverlayProps>}[] = [
  {id: 'FocusBracket', component: FocusBracket},
  {id: 'ShapeWipe', component: ShapeWipe},
  {id: 'AccentBurst', component: AccentBurst},
  {id: 'BeatPulse', component: BeatPulse},
  {id: 'InnerScratch', component: InnerScratch},
  {id: 'MobyTitle', component: MobyTitle},
];

const OVERLAY_DEFAULTS: OverlayProps = {
  seed: 42,
  format: 'vertical',
  durationSec: 2,
  palette: ['#e8e2d6', '#cfd6dd', '#1a2230'],
};

export const RemotionRoot: React.FC = () => {
  return (
    <>
      {TEMPLATES.map(({id, component}) => (
        <Composition
          key={id}
          id={id}
          component={component as React.FC<Record<string, unknown>>}
          schema={templatePropsSchema}
          defaultProps={DEFAULT_PROPS}
          fps={FPS}
          durationInFrames={FPS * PREVIEW_SEC}
          width={FORMAT_DIMS.vertical.width}
          height={FORMAT_DIMS.vertical.height}
          calculateMetadata={({props}: {props: TemplateProps}) => {
            const dims = FORMAT_DIMS[props.format as 'square' | 'vertical'] ?? FORMAT_DIMS.vertical;
            const durationInFrames = props.durationSec
              ? Math.round(props.durationSec * FPS)
              : FPS * PREVIEW_SEC;
            return {width: dims.width, height: dims.height, fps: FPS, durationInFrames};
          }}
        />
      ))}

      {OVERLAYS.map(({id, component}) => (
        <Composition
          key={id}
          id={id}
          component={component as React.FC<Record<string, unknown>>}
          schema={overlayPropsSchema}
          defaultProps={OVERLAY_DEFAULTS}
          fps={FPS}
          durationInFrames={FPS * 2}
          width={FORMAT_DIMS.vertical.width}
          height={FORMAT_DIMS.vertical.height}
          calculateMetadata={({props}: {props: OverlayProps}) => {
            const dims = FORMAT_DIMS[props.format as 'square' | 'vertical'] ?? FORMAT_DIMS.vertical;
            return {
              width: dims.width,
              height: dims.height,
              fps: FPS,
              durationInFrames: Math.round((props.durationSec || 2) * FPS),
            };
          }}
        />
      ))}
    </>
  );
};
