import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import en from '../../i18n/locales/en';
import uk from '../../i18n/locales/uk';
import { isGcodeCompatible } from '../../utils/printer';

describe('routing mirrors cannot silently drift', () => {
  it.each([['en', en], ['uk', uk]] as const)('keeps %s refusal sentences identical to the backend', (lang, locale) => {
    const backend = JSON.parse(readFileSync(resolve(process.cwd(), `../backend/app/data/filament_routing_${lang}.json`), 'utf8'));
    for (const [code, message] of Object.entries(locale.filamentRouting.feasibility.reason)) {
      expect(message, code).toBe(backend[code]);
    }
    for (const [key, message] of Object.entries(locale.filamentRouting.feasibility.detail)) {
      const backendKey = key.replace(/[A-Z]/g, letter => `_${letter.toLowerCase()}`);
      expect(message.replace(/\{\{([^}]+)\}\}/g, '{$1}'), key).toBe(backend.detail[backendKey]);
    }
  });

  it('uses the backend compatibility families for all known model pairs', () => {
    const backend = readFileSync(resolve(process.cwd(), '../backend/app/utils/printer_models.py'), 'utf8');
    const declaration = backend.match(/GCODE_COMPAT_FAMILIES = ([^\n]+)/)?.[1];
    expect(declaration).toBeTruthy();
    const families = [...declaration!.matchAll(/frozenset\(\[([^\]]+)\]/g)]
      .map(match => [...match[1].matchAll(/"([^"]+)"/g)].map(row => row[1]));
    expect(families.length).toBeGreaterThan(0);
    const frontend = readFileSync(resolve(process.cwd(), 'src/utils/printer.ts'), 'utf8');
    const frontendDeclaration = frontend.match(/const GCODE_COMPAT_FAMILIES[^=]*= ([\s\S]*?);/)?.[1];
    expect(frontendDeclaration).toBeTruthy();
    const frontendFamilies = [...frontendDeclaration!.matchAll(/new Set\(\[([^\]]+)\]/g)]
      .map(match => [...match[1].matchAll(/'([^']+)'/g)].map(row => row[1]));
    expect(frontendFamilies.map(group => group.sort()).sort()).toEqual(families.map(group => group.sort()).sort());
    const models = [...new Set([...families.flat(), 'P2S', 'H2D', 'A1', 'A1MINI', 'X2D'])];
    for (const a of models) for (const b of models) {
      expect(isGcodeCompatible(a, b), `${a}/${b}`).toBe(a === b || families.some(group => group.includes(a) && group.includes(b)));
    }
    expect(isGcodeCompatible('C11', 'Bambu Lab X1 Carbon')).toBe(true);
  });
});
