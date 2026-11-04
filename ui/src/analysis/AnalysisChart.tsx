import { useEffect, useRef } from 'react';
import { Chart, BarController, BarElement, LineController, LineElement, PointElement, CategoryScale, LinearScale, Tooltip } from 'chart.js';
import { useThemeStore } from '../stores/useThemeStore';
import { bodyFontPending } from '../theme/fonts';
import { analysisChartConfig } from './chartConfig';

Chart.register(BarController, BarElement, LineController, LineElement, PointElement, CategoryScale, LinearScale, Tooltip);

export function AnalysisChart({ title, labels, values, unit, kind }: { title: string; labels: string[]; values: (number | null)[]; unit: string; kind: 'line' | 'bar' }) {
  const ref = useRef<HTMLCanvasElement>(null);
  const theme = useThemeStore((state) => state.theme);
  useEffect(() => {
    if (!ref.current) return;
    const chart = new Chart(ref.current, analysisChartConfig({ title, labels, values, unit, kind, theme }));
    let destroyed = false;
    // Drawn before Nexa loaded, the axes keep the fallback face until the next draw.
    void bodyFontPending()?.then(() => {
      if (!destroyed) chart.update();
    });
    return () => {
      destroyed = true;
      chart.destroy();
    };
  }, [title, labels, values, unit, kind, theme]);
  return <section className="analysis-card"><h2>{title}</h2><div className="analysis-chart"><canvas ref={ref} role="img" aria-label={title} /></div></section>;
}
