import { describe, expect, it } from 'vitest';
import { BODY_FONT_STACK } from '../theme/fonts';
import { analysisChartConfig } from './chartConfig';

const config = (theme: 'sun' | 'dark') =>
  analysisChartConfig({ title: 'Carry', labels: ['1', '2'], values: [150, null], unit: 'm', kind: 'line', theme });

describe('analysis chart', () => {
  it.each(['sun', 'dark'] as const)('draws ticks, the axis title and tooltips in the body face (%s)', (theme) => {
    const { options } = config(theme);
    const scales = options?.scales as Record<'x' | 'y', { ticks: { font: { family: string; size: number } }; title?: { font: { family: string } } }>;
    expect(scales.x.ticks.font).toEqual({ family: BODY_FONT_STACK, size: 16 });
    expect(scales.y.ticks.font).toEqual({ family: BODY_FONT_STACK, size: 16 });
    expect(scales.y.title?.font.family).toBe(BODY_FONT_STACK);
    const tooltip = options?.plugins?.tooltip as { titleFont: { family: string }; bodyFont: { family: string } };
    expect(tooltip.titleFont.family).toBe(BODY_FONT_STACK);
    expect(tooltip.bodyFont.family).toBe(BODY_FONT_STACK);
  });

  it('draws the night line in neon green', () => {
    expect(config('dark').data.datasets[0].borderColor).toBe('#00ff78');
  });

  it('keeps the dark green for the sun chart, where a thin neon line vanishes on white', () => {
    expect(config('sun').data.datasets[0].borderColor).toBe('#007a39');
  });
});
