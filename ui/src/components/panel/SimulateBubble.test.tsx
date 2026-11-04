import { renderToString } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import { SimulateBubble } from './SimulateBubble';

describe('SimulateBubble', () => {
  it('is a single floating control for simulating a shot', () => {
    const html = renderToString(<SimulateBubble label="Simulate shot" onSimulate={() => {}} />);

    expect(html).toContain('simulate-bubble');
    expect(html).toContain('>Simulate shot<');
    expect(html).not.toContain('Mock');
    expect(html).not.toContain('role="menu"');
  });

  it('takes an extra class, as the range does to clear its bar', () => {
    const html = renderToString(<SimulateBubble label="Simulate shot" onSimulate={() => {}} className="on-range" />);

    expect(html).toContain('class="simulate-bubble on-range"');
  });
});
