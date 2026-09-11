import {z} from 'zod';

/**
 * Контракт ОВЕРЛЕЯ — анимированный графический хук с АЛЬФА-каналом (à la CapCut:
 * акценты, переходы, стикеры). Композитится ПОВЕРХ клипа в нужный момент.
 * Рендерится прозрачным (ProRes 4444) — НЕ красить непрозрачный фон!
 *
 * Назначение оверлея = добавить динамику / акцентировать важное. Текст здесь —
 * максимум короткий бренд-акцент (1-2 фиксированных приёма = брендбук), НЕ основное.
 * Палитра приглушённая, БЕЗ неона.
 */
export const overlayPropsSchema = z.object({
  seed: z.number().default(42),
  format: z.enum(['square', 'vertical']).default('vertical'),
  durationSec: z.number().default(2),
  // приглушённый акцентный цвет (без неона)
  palette: z.array(z.string()).default(['#e8e2d6', '#cfd6dd', '#1a2230']),
  // опц. короткий акцент-текст (1-2 слова) — НЕ обязателен, оверлей самоценен графикой
  accentText: z.string().optional(),
  // опц. шрифтовой кандидат для текстовых оверлеев (ключ из TITLE_FONT_CANDIDATES)
  titleFont: z.string().optional(),
});

export type OverlayProps = z.infer<typeof overlayPropsSchema>;
