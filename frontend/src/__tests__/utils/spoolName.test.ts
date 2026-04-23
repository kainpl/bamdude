import { describe, it, expect } from 'vitest';

import type { InventorySpool } from '../../api/client';
import {
  DEFAULT_SPOOL_DISPLAY_TEMPLATE,
  formatSpoolDisplayName,
  spoolDisplayNameMatches,
  SPOOL_PLACEHOLDERS,
} from '../../utils/spoolName';

function makeSpool(overrides: Partial<InventorySpool> = {}): InventorySpool {
  return {
    id: 1,
    material: 'PETG',
    subtype: null,
    color_name: 'Black',
    rgba: '000000FF',
    brand: 'SUNLU',
    label_weight: 1000,
    core_weight: 250,
    core_weight_catalog_id: null,
    weight_used: 250,
    slicer_filament: null,
    slicer_filament_name: null,
    nozzle_temp_min: null,
    nozzle_temp_max: null,
    note: null,
    added_full: null,
    last_used: null,
    encode_time: null,
    tag_uid: null,
    tray_uuid: null,
    data_origin: null,
    tag_type: null,
    archived_at: null,
    created_at: '2026-01-01T00:00:00',
    updated_at: '2026-01-01T00:00:00',
    cost_per_kg: 25,
    last_scale_weight: null,
    last_weighed_at: null,
    ...overrides,
  };
}

describe('formatSpoolDisplayName', () => {
  it('renders the default template for a fully-populated spool', () => {
    expect(formatSpoolDisplayName(makeSpool(), DEFAULT_SPOOL_DISPLAY_TEMPLATE)).toBe('SUNLU PETG Black');
  });

  it('falls back to the default template when the caller passes empty', () => {
    expect(formatSpoolDisplayName(makeSpool(), '')).toBe('SUNLU PETG Black');
    expect(formatSpoolDisplayName(makeSpool(), '   ')).toBe('SUNLU PETG Black');
    expect(formatSpoolDisplayName(makeSpool(), null)).toBe('SUNLU PETG Black');
  });

  it('collapses whitespace left by missing placeholders', () => {
    // subtype is null → "{brand} {subtype} {material}" should not leave "SUNLU  PETG"
    expect(formatSpoolDisplayName(makeSpool(), '{brand} {subtype} {material}')).toBe('SUNLU PETG');
  });

  it('leaves unknown placeholders visible so typos surface in preview', () => {
    expect(formatSpoolDisplayName(makeSpool(), '{brand} {typo} {material}')).toBe('SUNLU {typo} PETG');
  });

  it('computes remaining_g / remaining_kg / remaining_pct from label − used', () => {
    const spool = makeSpool({ label_weight: 1000, weight_used: 250 });
    expect(formatSpoolDisplayName(spool, '{remaining_g}')).toBe('750');
    expect(formatSpoolDisplayName(spool, '{remaining_kg}')).toBe('0.75');
    expect(formatSpoolDisplayName(spool, '{remaining_pct}')).toBe('75%');
  });

  it('clamps negative remaining to zero when usage exceeds label', () => {
    const spool = makeSpool({ label_weight: 1000, weight_used: 1200 });
    expect(formatSpoolDisplayName(spool, '{remaining_g}')).toBe('0');
    expect(formatSpoolDisplayName(spool, '{remaining_pct}')).toBe('0%');
  });

  it('formats label_weight_kg as integer when round, 2-decimal otherwise', () => {
    expect(formatSpoolDisplayName(makeSpool({ label_weight: 1000 }), '{label_weight_kg}')).toBe('1');
    expect(formatSpoolDisplayName(makeSpool({ label_weight: 750 }), '{label_weight_kg}')).toBe('0.75');
    expect(formatSpoolDisplayName(makeSpool({ label_weight: 250 }), '{label_weight_kg}')).toBe('0.25');
  });

  it('derives color_hex from the first 6 chars of rgba, uppercase, with #', () => {
    expect(formatSpoolDisplayName(makeSpool({ rgba: 'ff3300ff' }), '{color_hex}')).toBe('#FF3300');
    expect(formatSpoolDisplayName(makeSpool({ rgba: null }), '{color_hex}')).toBe('');
  });

  it('skips missing optional fields rather than printing "null"', () => {
    expect(
      formatSpoolDisplayName(
        makeSpool({ brand: null, subtype: null }),
        '{brand} {material} {subtype} {color_name}',
      ),
    ).toBe('PETG Black');
  });
});

describe('spoolDisplayNameMatches', () => {
  it('matches when every whitespace-separated token is a case-insensitive substring', () => {
    // The motivating case: "SUN Bl" should find "SUNLU PETG Black".
    expect(spoolDisplayNameMatches('SUNLU PETG Black', 'SUN Bl')).toBe(true);
    expect(spoolDisplayNameMatches('SUNLU PETG Black', 'petg black')).toBe(true);
  });

  it('fails when any one token is absent', () => {
    expect(spoolDisplayNameMatches('SUNLU PETG Black', 'SUN abs')).toBe(false);
    expect(spoolDisplayNameMatches('SUNLU PETG Black', 'overture')).toBe(false);
  });

  it('treats an empty / whitespace query as "show everything"', () => {
    expect(spoolDisplayNameMatches('anything', '')).toBe(true);
    expect(spoolDisplayNameMatches('anything', '   ')).toBe(true);
  });
});

describe('SPOOL_PLACEHOLDERS registry', () => {
  it('has unique keys', () => {
    const keys = SPOOL_PLACEHOLDERS.map((p) => p.key);
    expect(new Set(keys).size).toBe(keys.length);
  });

  it('every formatter returns a string (never null/undefined) on a fully-populated spool', () => {
    const spool = makeSpool({
      subtype: 'Matte',
      slicer_filament_name: 'Generic PLA @BBL X1C',
      note: 'Kitchen shelf',
    });
    for (const ph of SPOOL_PLACEHOLDERS) {
      const value = ph.format(spool);
      expect(typeof value).toBe('string');
    }
  });
});
