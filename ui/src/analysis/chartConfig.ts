import type { ChartConfiguration } from 'chart.js';
import type { Theme } from '../theme/theme';
import { BODY_FONT_STACK } from '../theme/fonts';

export interface AnalysisChartInput {
  title: string;
  labels: string[];
  values: (number | null)[];
  unit: string;
  kind: 'line' | 'bar';
  theme: Theme;
}

/** Chart.js draws on a canvas, so it gets the body face by name, not through CSS. */
const family = BODY_FONT_STACK;
const font = { family, size: 16 };

export function analysisChartConfig({ title, labels, values, unit, kind, theme }: AnalysisChartInput): ChartConfiguration<'line' | 'bar'> {
  // Neon green at night; the sun chart keeps a darker green, as a thin neon line vanishes on white.
  const color = theme === 'dark' ? '#00ff78' : '#007a39';
  // Sun: the muted token (rgba(0,0,0,.82) on white), not a grey-green that fades in daylight.
  const text = theme === 'dark' ? '#bcc5bc' : '#2e2e2e';
  return {
    type: kind,
    data: { labels, datasets: [{ label: `${title} (${unit})`, data: values, borderColor: color, backgroundColor: color, borderWidth: 2, pointRadius: values.length > 150 ? 0 : 3, spanGaps: false }] },
    options: { responsive: true, maintainAspectRatio: false, animation: false,
      plugins: { tooltip: { titleFont: font, bodyFont: font } },
      scales: { x: { ticks: { color: text, maxTicksLimit: 9, font }, grid: { display: false } },
        y: { beginAtZero: true, title: { display: true, text: unit, color: text, font: { family } }, ticks: { color: text, font }, grid: { color: theme === 'dark' ? '#303830' : '#c8c8c8' } } },
    },
  };
}
