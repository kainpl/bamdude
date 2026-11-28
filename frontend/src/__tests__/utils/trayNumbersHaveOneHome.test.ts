/**
 * Tray numbers are computed, split and labelled in one place: `utils/trayIds.ts`
 * (the twin of `backend/app/utils/tray_ids.py`, whose own guard is
 * `test_tray_numbers_have_one_home.py`).
 *
 * The rule is lexical, line by line in `src/**\/*.{ts,tsx}` (tests excluded): a
 * line that names a tray-ish value (`ams|tray|unit|slot|global`, any case) and
 * holds a numbering form — `* 4 +`, `<< 8 |`, `254 +`, `+ 254`, `/ 4)`, `% 4`,
 * `& 3`, `>> 2`, `>> 8`, `& 0xFF`, `- 254`, `- 128`, `>= 254`, `< 254`,
 * `>= 128`, `128 <= … <= 135`, `fromCharCode(65 +` — is a finding. A finding
 * is moved to the helper, or named below with the stage that owns it (N01 C:
 * K-profile, drying) and why. An entry that no longer matches anything fails
 * too, so the list shrinks as the stages land (stage B — dispatch — has).
 */
import { describe, it, expect } from 'vitest';
import { readdirSync, readFileSync } from 'node:fs';
import { join, relative, sep } from 'node:path';

const SRC = join(process.cwd(), 'src');
const HOME = 'utils/trayIds.ts';
const TRAYISH = /ams|tray|unit|slot|global/i;
const FORMS: Array<[string, RegExp]> = [
  ['* 4 +', /\*\s*4\s*\+/],
  ['<< 8 |', /<<\s*8\s*\|/],
  ['254 +', /\b254\s*\+/],
  ['+ 254', /\+\s*254\b/],
  ['/ 4)', /\/\s*4\s*\)/],
  ['% 4', /%\s*4\b/],
  ['& 3', /&\s*(?:3|0x0?3)\b/],
  ['>> 2', />>\s*2\b/],
  ['>> 8', />>\s*8\b/],
  ['& 0xFF', /&\s*0xff\b/i],
  ['- 254', /-\s*254\b/],
  ['- 128', /-\s*128\b/],
  ['>= 254', />=\s*254\b/],
  ['< 254', /<\s*254\b/],
  ['>= 128', />=\s*128\b/],
  ['128 <= x <= 135', /128\s*<=.*<=\s*135/],
  ['fromCharCode(65 +', /fromCharCode\(\s*65\s*\+/],
];

/** `file :: form` → the stage that owns it and why it is still here. */
const EXEMPT: Record<string, string> = {
  // N01 stage C — K-profile, drying.
  'components/ConfigureAmsSlotModal.tsx :: - 128': 'C: K-profile of a slot (extrusion_cali_set tray id)',
  'components/ConfigureAmsSlotModal.tsx :: >= 128': 'C: K-profile of a slot (extrusion_cali_set tray id)',
  'components/DryingScheduleModal.tsx :: >= 128': 'C: drying ceiling per unit',
  'utils/scheduledDrying.ts :: >= 128': 'C: drying schedule unit label',
  'utils/scheduledDrying.ts :: - 128': 'C: drying schedule unit label',
  'utils/scheduledDrying.ts :: fromCharCode(65 +': 'C: drying schedule unit label',
};

function walk(dir: string, out: string[] = []): string[] {
  for (const d of readdirSync(dir, { withFileTypes: true })) {
    const p = join(dir, d.name);
    if (d.isDirectory()) {
      if (d.name !== '__tests__' && d.name !== 'part-render') walk(p, out);
    } else if (/\.(ts|tsx)$/.test(d.name) && !/\.test\.tsx?$/.test(d.name)) {
      out.push(p);
    }
  }
  return out;
}

function stripComment(line: string): string {
  const at = line.indexOf('//');
  return at >= 0 ? line.slice(0, at) : line;
}

function findings(): Map<string, string[]> {
  const found = new Map<string, string[]>();
  for (const path of walk(SRC)) {
    const rel = relative(SRC, path).split(sep).join('/');
    if (rel === HOME) continue;
    readFileSync(path, 'utf8')
      .split(/\r?\n/)
      .forEach((raw, i) => {
        const line = stripComment(raw);
        if (!TRAYISH.test(line)) return;
        for (const [form, re] of FORMS) {
          if (re.test(line)) {
            const key = `${rel} :: ${form}`;
            found.set(key, [...(found.get(key) ?? []), `${rel}:${i + 1}`]);
          }
        }
      });
  }
  return found;
}

describe('tray numbers have one home (utils/trayIds.ts)', () => {
  const found = findings();

  it('no hand-written tray arithmetic outside the helper, but for the named stages', () => {
    const unexpected = [...found.entries()].filter(([key]) => !(key in EXEMPT)).map(([, at]) => at.join(', '));
    expect(unexpected).toEqual([]);
  });

  it('every exemption still names a finding (a landed stage removes its rows)', () => {
    const stale = Object.keys(EXEMPT).filter(key => !found.has(key));
    expect(stale).toEqual([]);
  });
});
