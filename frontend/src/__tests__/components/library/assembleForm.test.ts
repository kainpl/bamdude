/**
 * The form of "Assemble project" as plain data: what a new form starts with, what a stored record turns into,
 * what is refused before the request, and the request itself.
 */
import { describe, expect, it } from 'vitest';

import type { AssemblyRecordResponse, PresetRef } from '../../../api/client';
import {
  MAX_COPIES,
  MAX_TARGETS,
  defaultName,
  emptyTarget,
  formFromRecord,
  initialForm,
  parseSpacing,
  toRelayoutRequest,
  toRequest,
  validate,
  type AssembleForm,
} from '../../../components/library/assembleForm';

const ref = (id: string): PresetRef => ({ source: 'local', id });
const FILES = [
  { id: 4, filename: 'jar.stl' },
  { id: 9, filename: 'lid.v2.obj' },
];

function ready(): AssembleForm {
  const form = initialForm(FILES);
  form.targets = [{ ...emptyTarget(), printer: ref('1'), process: ref('2'), filament: ref('3') }];
  return form;
}

describe('assembleForm', () => {
  it('names the result after the single file, or the first one and how many more', () => {
    expect(defaultName([])).toBe('');
    expect(defaultName([FILES[0]])).toBe('jar');
    expect(defaultName(FILES)).toBe('jar +1');
    expect(defaultName([{ filename: 'lid.v2.obj' }])).toBe('lid.v2');
  });

  it('starts with one copy of every file, one empty printer row and the agreed layout', () => {
    const form = initialForm(FILES);
    expect(form.parts).toEqual([
      { fileId: 4, name: 'jar.stl', quantity: 1, fill: false, orientation: 'as_is' },
      { fileId: 9, name: 'lid.v2.obj', quantity: 1, fill: false, orientation: 'as_is' },
    ]);
    expect(form.targets).toHaveLength(1);
    expect(form.targets[0]).toMatchObject({ printer: null, process: null, filament: null, nozzle: 1 });
    expect(form).toMatchObject({ spacing: '2', rotationStep: -1, centre: true, splitPlates: false, longSearch: false });
  });

  it('refuses what the server would refuse, by field', () => {
    expect(validate(initialForm(FILES))).toEqual(['targets']);
    expect(validate(ready())).toEqual([]);
    expect(validate({ ...ready(), name: '  ' })).toEqual(['name']);
    expect(validate({ ...ready(), targets: [] })).toEqual(['targets']);
    const tooMany = { ...ready(), targets: Array.from({ length: MAX_TARGETS + 1 }, () => ready().targets[0]) };
    expect(validate(tooMany)).toEqual(['targets']);
    const zero = ready();
    zero.parts[0].quantity = 0;
    expect(validate(zero)).toEqual(['quantity']);
    const many = ready();
    many.parts[0].quantity = MAX_COPIES;
    many.parts[1].quantity = 1;
    expect(validate(many)).toEqual(['copies']);
    many.parts[1].fill = true; // a filled plate asks for no number
    expect(validate(many)).toEqual([]);
    expect(validate({ ...ready(), spacing: 'wide' })).toEqual(['spacing']);
    expect(validate({ ...ready(), spacing: '51' })).toEqual(['spacing']);
  });

  it('reads the gap as typed, with a comma or a dot', () => {
    expect(parseSpacing('2,5')).toBe(2.5);
    expect(parseSpacing(' 0 ')).toBe(0);
    expect(parseSpacing('')).toBeNull();
    expect(parseSpacing('-1')).toBeNull();
  });

  it('becomes the request the server takes', () => {
    const form = ready();
    form.parts[0].quantity = 6;
    form.parts[1].fill = true;
    form.parts[1].quantity = 40; // ignored once the plate is filled
    form.parts[1].orientation = 'x+90';
    form.targets[0].nozzle = 2;
    form.spacing = '1,5';
    form.splitPlates = true;
    form.name = '  jars  ';
    expect(toRequest(form)).toEqual({
      parts: [
        { file_id: 4, quantity: 6, fill: false, orientation: 'as_is' },
        { file_id: 9, quantity: 1, fill: true, orientation: 'x+90' },
      ],
      targets: [{ printer_preset: ref('1'), process_preset: ref('2'), filament_preset: ref('3'), nozzle: 2 }],
      layout: { spacing: 1.5, rotation_step: -1, centre: true, split_plates: true },
      name: 'jars',
    });
  });

  it('opens a re-layout with the values the file was made with', () => {
    const record: AssemblyRecordResponse = {
      set_id: 'a'.repeat(32),
      base_name: 'jars',
      parts: [
        { name: 'jar.stl', quantity: 6, fill: false, orientation: 'as_is' },
        { name: 'lid.stl', quantity: 1, fill: true, orientation: 'as_is' },
      ],
      printer: { printer: ref('1'), process: ref('2'), filament: ref('3'), nozzle: 2, model: 'H2D' },
      layout: { spacing: 3, rotation_step: 45, centre: false, split_plates: true },
      plates: [],
      set_files: [],
    };
    const form = formFromRecord(record);
    expect(form.name).toBe('jars');
    expect(form.parts.map((part) => [part.name, part.quantity, part.fill, part.fileId])).toEqual([
      ['jar.stl', 6, false, undefined],
      ['lid.stl', 1, true, undefined],
    ]);
    expect(form.targets[0]).toMatchObject({ printer: ref('1'), process: ref('2'), filament: ref('3'), nozzle: 2 });
    expect(form).toMatchObject({ spacing: '3', rotationStep: 45, centre: false, splitPlates: true, trashPrevious: true });
    expect(validate(form)).toEqual([]);

    const request = toRelayoutRequest({ ...form, trashPrevious: false });
    expect(request.trash_previous).toBe(false);
    expect(request.parts.every((part) => !('file_id' in part))).toBe(true);
    expect(request.layout).toEqual({ spacing: 3, rotation_step: 45, centre: false, split_plates: true });
  });
});
