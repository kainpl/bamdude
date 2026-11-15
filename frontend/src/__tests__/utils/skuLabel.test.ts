import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { skuLabel } from '../../utils/skuLabel';

describe('skuLabel', () => {
  const sku = (subtype: string | null, color_name: string | null) =>
    ({ brand: 'Bambu Lab', material: 'PLA', subtype, color_name });

  it('names a colour that repeats the subtype once', () => {
    expect(skuLabel(sku('Basic Black', 'Basic Black'))).toBe('Bambu Lab PLA Basic Black');
    expect(skuLabel(sku('Basic Black', '  basic black '))).toBe('Bambu Lab PLA Basic Black');
  });

  it('keeps a colour of its own', () => {
    expect(skuLabel(sku('Basic', 'Black'))).toBe('Bambu Lab PLA Basic Black');
    expect(skuLabel(sku(null, 'Black'))).toBe('Bambu Lab PLA Black');
    expect(skuLabel(sku('Basic', null))).toBe('Bambu Lab PLA Basic');
    expect(skuLabel({ brand: null, material: 'PLA', subtype: null, color_name: null })).toBe('PLA');
  });

  it('is the only place the forecast panel joins a SKU name', () => {
    const source = readFileSync(resolve(__dirname, '../../components/ForecastPanel.tsx'), 'utf8');
    expect(source).not.toMatch(/subtype,\s*\w+\.color_name\]\.filter\(Boolean\)/);
    expect(source).toMatch(/const rowLabel = skuLabel;/);
  });
});
