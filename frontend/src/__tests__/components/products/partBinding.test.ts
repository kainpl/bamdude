/**
 * A part's binding to variant options (spec product-part-variant-combinations, п. 5, 23):
 * the options of several groups — «and» between groups, «or» within one. These helpers only
 * NAME a binding and a reason; whether a combination can be ordered is the server's answer.
 */
import { describe, it, expect } from 'vitest';
import i18n from '../../../i18n';
import type { VariantGroup } from '../../../api/client';
import {
  bindingByGroup,
  bindingLabel,
  bindingVariants,
  reasonText,
  variantsLabel,
  variantsTitle,
} from '../../../components/products/partBinding';

const en = i18n.getFixedT('en');
const uk = i18n.getFixedT('uk');

function group(id: number, name: string, position: number, options: [number, string][]): VariantGroup {
  return {
    id,
    name,
    position,
    default_option_id: options[0][0],
    requires_part: false,
    lines_count: 0,
    stock_count: 0,
    parts_count: 0,
    options: options.map(([oid, oname], index) => ({
      id: oid,
      name: oname,
      position: index,
      lines_count: 0,
      parts_count: 0,
      stock_count: 0,
    })),
  };
}

// Out of position order on purpose: the label follows `position`, not the array.
const groups: VariantGroup[] = [
  group(2, 'Вага', 1, [
    [21, '400'],
    [22, '600'],
    [23, '1200'],
  ]),
  group(3, 'Вид', 2, [
    [31, 'сплошна'],
    [32, 'сітчаста'],
  ]),
  group(1, 'Кришка', 0, [
    [11, 'закрутка'],
    [12, 'защолка'],
  ]),
];

describe('bindingByGroup', () => {
  it('files each option under its group, groups and options in position order', () => {
    const map = bindingByGroup(groups, [31, 22, 11, 21]);
    expect([...map.entries()]).toEqual([
      [1, [11]],
      [2, [21, 22]],
      [3, [31]],
    ]);
  });

  it('an option the product does not have is left out; none — an empty map', () => {
    expect([...bindingByGroup(groups, [99, 21]).entries()]).toEqual([[2, [21]]]);
    expect(bindingByGroup(groups, []).size).toBe(0);
  });
});

describe('bindingLabel', () => {
  it('one value per group: the values, groups in position order', () => {
    expect(bindingLabel(groups, [31, 21])).toBe('400 · сплошна');
  });

  it('two values in one group: joined by a comma, the groups by a dot', () => {
    expect(bindingLabel(groups, [22, 11, 21])).toBe('закрутка · 400, 600');
  });

  it('no binding: null — the part is always in the kit', () => {
    expect(bindingLabel(groups, [])).toBeNull();
  });

  it('as name snapshots, in the shape the stock views receive', () => {
    expect(bindingVariants(groups, [22, 11, 21])).toEqual([
      { group: 'Кришка', options: ['закрутка'] },
      { group: 'Вага', options: ['400', '600'] },
    ]);
  });
});

describe('variantsLabel / variantsTitle — the server’s name snapshots', () => {
  const variants = [
    { group: 'Кришка', options: ['закрутка'] },
    { group: 'Вага', options: ['400', '600'] },
  ];

  it('the short form names the values, the long one the groups too', () => {
    expect(variantsLabel(variants)).toBe('закрутка · 400, 600');
    expect(variantsTitle(variants)).toBe('Кришка: закрутка · Вага: 400, 600');
  });

  it('no groups: null', () => {
    expect(variantsLabel([])).toBeNull();
    expect(variantsTitle([])).toBeNull();
  });
});

describe('reasonText', () => {
  const noPart = { code: 'option_without_part', group: 'Вид', option: 'сітчаста', part: null } as const;
  const nothing = { code: 'option_without_part', group: 'Колір', option: null, part: null } as const;
  const unsliced = { code: 'part_not_sliced', group: null, option: null, part: 'Корпус 1200' } as const;

  it('a value no part names', () => {
    expect(reasonText(uk, noPart)).toBe('немає деталі для «сітчаста» у групі «Вид»');
    expect(reasonText(en, noPart)).toBe('no part for «сітчаста» in «Вид»');
  });

  it('nothing chosen in a group without a standard option', () => {
    expect(reasonText(uk, nothing)).toBe('у групі «Колір» нічого не обрано');
    expect(reasonText(en, nothing)).toBe('nothing chosen in «Колір»');
  });

  it('a printed part with no sliced source', () => {
    expect(reasonText(uk, unsliced)).toBe('«Корпус 1200» не нарізано');
    expect(reasonText(en, unsliced)).toBe('«Корпус 1200» is not sliced');
  });
});
