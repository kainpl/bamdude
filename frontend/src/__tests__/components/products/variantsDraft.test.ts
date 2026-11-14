/**
 * The variants manager's draft, as pure functions (WS-13 E10 D02–D04): read from the
 * product, written back as the batch's body, checked before any request. An inherited
 * group without a standard, or empty, is kept as it is stored (A06, R11): its names and
 * options may change, a standard never appears by itself.
 */

import { describe, it, expect } from 'vitest';
import type { Product } from '../../../api/client';
import {
  addGroup,
  addOption,
  draftErrors,
  draftFromProduct,
  draftToApply,
  restoreGroup,
  restoreOption,
  sameDraft,
} from '../../../components/products/variantsDraft';

const usage = { lines_count: 0, stock_count: 0, parts_count: 0 };
const product = {
  variant_groups: [
    {
      id: 2,
      name: 'Base',
      position: 1,
      default_option_id: null,
      requires_part: true,
      ...usage,
      options: [
        { id: 21, name: 'X', position: 0, ...usage },
        { id: 22, name: 'Y', position: 1, ...usage },
      ],
    },
    {
      id: 1,
      name: 'Tail',
      position: 0,
      default_option_id: 12,
      requires_part: false,
      ...usage,
      options: [
        { id: 12, name: 'B', position: 1, ...usage },
        { id: 11, name: 'A', position: 0, ...usage },
      ],
    },
    { id: 3, name: 'Extra', position: 2, default_option_id: null, ...usage, options: [] },
  ],
} as unknown as Product;

describe('variantsDraft', () => {
  it('reads groups and options in their positions, the standard by key, the stored state kept', () => {
    const draft = draftFromProduct(product);
    expect(draft.groups.map((g) => g.name)).toEqual(['Tail', 'Base', 'Extra']);
    expect(draft.groups[0].options.map((o) => o.name)).toEqual(['A', 'B']);
    expect(draft.groups[0].standard).toBe('o12');
    expect(draft.groups[1].standard).toBeNull();
    expect(draft.groups[2].stored).toEqual({ hadOptions: false, hadStandard: false });
  });

  it('writes the whole draft back: ids for what exists, temp ids for what is new, names trimmed', () => {
    let draft = draftFromProduct(product);
    draft.groups[0].options[0].name = ' A wide ';
    draft = addOption(draft, 'g1', 'C');
    draft = addGroup(draft, { name: 'Size', options: ['S', 'M'] });
    const body = draftToApply(draft, 'rev-1');
    expect(body.revision).toBe('rev-1');
    expect(body.groups[0]).toEqual({
      id: 1,
      name: 'Tail',
      default: 12,
      requires_part: false,
      options: [
        { id: 11, name: 'A wide' },
        { id: 12, name: 'B' },
        { temp_id: expect.any(String), name: 'C' },
      ],
    });
    expect(body.groups[1].default).toBeNull();
    const size = body.groups[3];
    expect(size.id).toBeUndefined();
    expect(size.temp_id).toEqual(expect.any(String));
    // A new group's standard is its first option, by its temp id.
    expect(size.default).toBe(size.options[0].temp_id);
    const temps = body.groups.flatMap((g) => [g.temp_id, ...g.options.map((o) => o.temp_id)]).filter(Boolean);
    expect(new Set(temps).size).toBe(temps.length);
  });

  it('an untouched draft is the same as the product; any change is not', () => {
    const draft = draftFromProduct(product);
    expect(sameDraft(draft, draftFromProduct(product))).toBe(true);
    const renamed = draftFromProduct(product);
    renamed.groups[1].name = 'Stand';
    expect(sameDraft(renamed, draftFromProduct(product))).toBe(false);
  });

  it('carries «every value needs its own part» as the server has it; a new group is off; the mark alone is a change', () => {
    let draft = draftFromProduct(product);
    expect(draft.groups.map((g) => g.requiresPart)).toEqual([false, true, false]);
    draft = addGroup(draft, { name: 'Size', options: ['S'] });
    expect(draftToApply(draft, 'r').groups.map((g) => g.requires_part)).toEqual([false, true, false, false]);
    const marked = draftFromProduct(product);
    marked.groups[0].requiresPart = true;
    expect(sameDraft(marked, draftFromProduct(product))).toBe(false);
    expect(draftToApply(marked, 'r').groups[0]).toEqual(expect.objectContaining({ id: 1, requires_part: true }));
  });

  it('an inherited group without a standard, and an empty one, are not errors', () => {
    let draft = draftFromProduct(product);
    draft.groups[1].name = 'Stand';
    draft = addOption(draft, 'g3', 'Felt pad');
    expect(draftErrors(draft)).toEqual([]);
    expect(draftToApply(draft, 'r').groups[2].default).toBeNull();
  });

  it('empty names, duplicates, a new group without options or standard, an emptied group are', () => {
    let draft = draftFromProduct(product);
    draft.groups[0].name = '  ';
    draft = addOption(draft, 'g1', 'a');
    draft = addGroup(draft, { name: 'base', options: [] });
    draft.groups[3].standard = null;
    draft.groups[1].options = draft.groups[1].options.filter(() => false);
    const codes = draftErrors(draft).map((e) => [e.group, e.option ?? null, e.code]);
    expect(codes).toEqual(
      expect.arrayContaining([
        ['g1', null, 'groupNameEmpty'],
        ['g1', expect.any(String), 'optionNameTaken'],
        ['g2', null, 'emptied'],
        [draft.groups[3].key, null, 'groupNameTaken'],
        [draft.groups[3].key, null, 'newGroupNoOptions'],
        [draft.groups[3].key, null, 'newGroupNoStandard'],
      ]),
    );
  });

  it('a taken-away standard is an error even where the UI would not allow it', () => {
    const draft = draftFromProduct(product);
    draft.groups[0].standard = null;
    expect(draftErrors(draft).map((e) => e.code)).toEqual(['standardMissing']);
  });

  it('a refused delete is undone at the place it had', () => {
    let draft = draftFromProduct(product);
    draft.groups[0].options = draft.groups[0].options.filter((o) => o.id !== 11);
    draft.groups = draft.groups.filter((g) => g.id !== 2);
    draft = restoreOption(draft, product, 11);
    draft = restoreGroup(draft, product, 2);
    expect(draft.groups.map((g) => g.name)).toEqual(['Tail', 'Base', 'Extra']);
    expect(draft.groups[0].options.map((o) => o.name)).toEqual(['A', 'B']);
    expect(sameDraft(draft, draftFromProduct(product))).toBe(true);
  });
});
