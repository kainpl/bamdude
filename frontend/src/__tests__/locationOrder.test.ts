import { describe, expect, it } from 'vitest';
import { byLocationName, compareLocationNames } from '../utils/locationOrder';

describe('compareLocationNames', () => {
  it('puts Ukrainian letters where the alphabet puts them, not where their code points do', () => {
    // The default sort() compares UTF-16 code units, and Ґ Є І Ї live outside
    // the А–Я block — so "Ірпінь" came first and the list read as unordered.
    const names = ['Цех', 'Ірпінь', 'Ангар', 'Єдиний'];

    expect([...names].sort(compareLocationNames)).toEqual(['Ангар', 'Єдиний', 'Ірпінь', 'Цех']);
  });

  it('does not push lowercase names to the end', () => {
    expect(['Цех', 'ангар'].sort(compareLocationNames)).toEqual(['ангар', 'Цех']);
  });

  it('counts numbered halls rather than spelling them', () => {
    expect(['Цех 10', 'Цех 2'].sort(compareLocationNames)).toEqual(['Цех 2', 'Цех 10']);
  });
});

describe('byLocationName', () => {
  it('orders rows by the name they carry', () => {
    const rows = [{ loc: { name: 'Цех' } }, { loc: { name: null } }, { loc: { name: 'Ангар' } }];

    expect(rows.sort(byLocationName((r) => r.loc.name)).map((r) => r.loc.name)).toEqual([null, 'Ангар', 'Цех']);
  });
});
