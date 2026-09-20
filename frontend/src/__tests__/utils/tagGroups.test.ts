import { describe, it, expect } from 'vitest';
import { groupByTag } from '../../utils/tagGroups';

const t = (id: number, name: string, color: string | null = null) => ({ id, name, color });

describe('groupByTag', () => {
  it('puts a printer under EACH of its tags, tag groups by name, untagged last', () => {
    const printers = [
      { id: 1, name: 'A', tags: [t(2, 'Phase 2'), t(1, 'Phase 1', '#f59e0b')] },
      { id: 2, name: 'B', tags: [t(1, 'Phase 1', '#f59e0b')] },
      { id: 3, name: 'C', tags: [] },
    ];
    const groups = groupByTag(printers, (p) => p.tags, 'No tag');
    expect(groups.map((g) => [g.label, g.items.map((p) => p.id)])).toEqual([
      ['Phase 1', [1, 2]],
      ['Phase 2', [1]],
      ['No tag', [3]],
    ]);
    expect(groups[0].color).toBe('#f59e0b');
    expect(groups[2].tagId).toBeNull();
  });

  it('omits the untagged group when everyone is tagged', () => {
    expect(groupByTag([{ id: 1, tags: [t(1, 'X')] }], (p) => p.tags, 'No tag').map((g) => g.label)).toEqual(['X']);
  });
});
