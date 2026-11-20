/**
 * One home for the slicing-setup controls (spec slicing-setup r5 G1, G4,
 * §11.1). The guard looks for interactive TRANSITIONS — a select whose options
 * are preset refs, a select that writes a bed plate, a per-job slicer pick, a
 * saved-set apply list, a source or farm switch, a forced catalogue refresh,
 * the "my preset" id pattern — anywhere in `src` outside their owners: the
 * family (`components/slicing-setup/`) and R1's module
 * (`utils/presetPickerUtils.ts`). Names and types alone are never a finding:
 * a slicer health line, a global slicer setting, a plate badge, the pipelines
 * CRUD and a passive banner all pass.
 *
 * Every marker has an inline copy it must catch, so a marker that stops
 * matching anything fails here instead of guarding nothing.
 */
import { describe, it, expect } from 'vitest';
import { existsSync, readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative, sep } from 'node:path';

const SRC = join(process.cwd(), 'src');
const OWNERS = ['components/slicing-setup/', 'utils/presetPickerUtils.ts'];

interface Marker {
  name: string;
  /** Lines of one file that carry the transition. */
  find: (text: string) => string[];
  /** Copies the marker must catch. */
  copies: string[];
}

const lines = (text: string) => text.split('\n');
const byLine = (re: RegExp, skip?: RegExp) => (text: string) =>
  lines(text).filter((line) => re.test(line) && !(skip && skip.test(line)));

const MARKERS: Marker[] = [
  {
    name: 'a select whose options are preset refs',
    find: (text) => [
      ...byLine(/\b(toRefValue|fromRefValue)\(/, /export function (toRefValue|fromRefValue)/)(text),
      ...byLine(/\$\{[\w?]+\.source\}:\$\{[\w?]+\.id\}/)(text),
    ],
    copies: [
      // flat
      '<select value={toRefValue(value)} onChange={(e) => onChange(fromRefValue(e.target.value))}>',
      // grouped
      '<optgroup label="Imported">{entries.map((p) => <option value={`${p.source}:${p.id}`}>{p.name}</option>)}</optgroup>',
      // Select-wrapped, options built beforehand
      "const choices = rows.map((p) => ({ key: `${tier.source}:${p.id}`, label: p.name }));",
    ],
  },
  {
    name: 'a select that writes a bed plate',
    find: byLine(/\bas BedType\b/),
    copies: ['<Select value={bed} onChange={(e) => setBed(e.target.value as BedType)}>'],
  },
  {
    name: 'a per-job slicer pick',
    find: byLine(/onClick=\{\(\) => [\w.]+\(\s*['"](orcaslicer|bambu_studio)['"]\s*\)\}/),
    copies: ["<button onClick={() => setPickedSlicer('orcaslicer')}>OrcaSlicer</button>"],
  },
  {
    name: 'a saved-set apply list',
    // a file that reads the sets and offers them as options
    find: (text) => (/listSlicerPipelines\(/.test(text) && /<option\b/.test(text) ? byLine(/listSlicerPipelines\(/)(text) : []),
    copies: [
      "const q = useQuery({ queryFn: () => api.listSlicerPipelines() });\n<select>{q.data?.pipelines.map((p) => <option value={p.id}>{p.name}</option>)}</select>",
    ],
  },
  {
    name: 'a source or farm switch',
    find: byLine(/\bset(OnlyLoaded|OnlyFarmModels|OnlyOnline|OwnerFilter)\(|\bonOwnerFilterChange\b|\bsetSource\(\s*['"](all|mine|builtin)['"]/),
    copies: [
      '<input type="checkbox" checked={onlyLoaded} onChange={(e) => setOnlyLoaded(e.target.checked)} />',
      "<button aria-pressed={source === 'mine'} onClick={() => filters.setSource('mine')}>Mine</button>",
    ],
  },
  {
    name: 'a forced catalogue refresh',
    find: byLine(/getSlicerPresets\(\s*\{\s*refresh:\s*true/),
    copies: ["const fresh = await api.getSlicerPresets({ refresh: true });"],
  },
  {
    name: 'the "my preset" id pattern',
    find: byLine(/P\[FPM\]US/),
    copies: ['const mine = /^(P[FPM]US|PF\\d)/.test(id);'],
  },
];

function sources(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const path = join(dir, entry);
    if (statSync(path).isDirectory()) {
      if (entry === '__tests__') continue;
      out.push(...sources(path));
    } else if (/\.(ts|tsx)$/.test(entry) && !/\.d\.ts$/.test(entry)) {
      out.push(path);
    }
  }
  return out;
}
const rel = (path: string) => relative(SRC, path).split(sep).join('/');
const owned = (path: string) => OWNERS.some((owner) => rel(path).startsWith(owner));

describe('slicing-setup ownership', () => {
  for (const marker of MARKERS) {
    it(`catches a copy: ${marker.name}`, () => {
      for (const copy of marker.copies) expect(marker.find(copy), copy).not.toEqual([]);
    });
  }

  it('finds no copy outside the family and R1’s module', () => {
    const findings: string[] = [];
    for (const path of sources(SRC)) {
      if (owned(path)) continue;
      const text = readFileSync(path, 'utf-8');
      for (const marker of MARKERS) {
        for (const line of marker.find(text)) findings.push(`${rel(path)} — ${marker.name}: ${line.trim()}`);
      }
    }
    expect(findings).toEqual([]);
  });

  it('lets the passive and global parts of the app be', () => {
    // §11.1's positives: health and the global setting, the plate badges, the
    // pipelines CRUD, the local-profiles CRUD, the profiles base-preset picker.
    for (const file of [
      'pages/SystemInfoPage.tsx',
      'pages/SettingsPage.tsx',
      'utils/bedType.ts',
      'components/SlicerPipelinesPanel.tsx',
      'components/LocalProfilesView.tsx',
      'pages/ProfilesPage.tsx',
      'components/SlicerOutdatedNote.tsx',
    ]) {
      const path = join(SRC, file);
      if (!existsSync(path)) continue;
      const text = readFileSync(path, 'utf-8');
      for (const marker of MARKERS) expect(marker.find(text), `${file}: ${marker.name}`).toEqual([]);
    }
  });

  it('has no other home for the pickers', () => {
    expect(existsSync(join(SRC, 'components/preset-picker'))).toBe(false);
    expect(existsSync(join(SRC, 'components/calibration/preset-picker'))).toBe(false);
    expect(existsSync(join(SRC, 'components/library/AssemblePresetSelect.tsx'))).toBe(false);
  });
});
