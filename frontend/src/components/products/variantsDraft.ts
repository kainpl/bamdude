import type { Product, VariantGroup, VariantsApply } from '../../api/client';

/**
 * The variants manager's draft (WS-13 E10 D02–D04) — pure functions over a plain value,
 * so every rule is tested without a dialog.
 *
 * Every element has a `key`: `g<id>` / `o<id>` for what exists, the temp id for what is
 * new. The standard is an option's key, or `null` — only for an existing group whose
 * stored standard was already `null` (A06). `stored` remembers what the group was when the
 * draft was read: the two independent checks of A06 (R11) are asked of it.
 */
export interface DraftOption {
  key: string;
  id?: number;
  tempId?: string;
  name: string;
}

export interface DraftGroup {
  key: string;
  id?: number;
  tempId?: string;
  name: string;
  options: DraftOption[];
  standard: string | null;
  /** «Every value needs its own part» — off for a new group; always sent. */
  requiresPart: boolean;
  /** The stored group as read; absent for a new one. */
  stored?: { hadOptions: boolean; hadStandard: boolean };
}

export interface VariantsDraft {
  groups: DraftGroup[];
}

export type DraftErrorCode =
  | 'groupNameEmpty'
  | 'optionNameEmpty'
  | 'groupNameTaken'
  | 'optionNameTaken'
  | 'newGroupNoOptions'
  | 'newGroupNoStandard'
  | 'emptied'
  | 'standardMissing';

/** A rule the draft breaks, at the group (and option) it is about. */
export interface DraftError {
  group: string;
  option?: string;
  code: DraftErrorCode;
}

export const groupKey = (id: number) => `g${id}`;
export const optionKey = (id: number) => `o${id}`;

// Unique for the page's life: two drafts never hand out the same temp id.
let tempSeq = 0;
const nextTemp = () => `t${++tempSeq}`;

const byPosition = <T extends { position: number; id: number }>(a: T, b: T) => a.position - b.position || a.id - b.id;

/** A uniqueness key as the server's `variant_key` folds it (trim + case fold). */
const nameKey = (name: string) => name.trim().toLowerCase();

function fromStored(group: VariantGroup): DraftGroup {
  return {
    key: groupKey(group.id),
    id: group.id,
    name: group.name,
    options: [...group.options].sort(byPosition).map((o) => ({ key: optionKey(o.id), id: o.id, name: o.name })),
    standard: group.default_option_id != null ? optionKey(group.default_option_id) : null,
    requiresPart: group.requires_part === true,
    stored: { hadOptions: group.options.length > 0, hadStandard: group.default_option_id != null },
  };
}

/** The product's groups and options in their positions. */
export function draftFromProduct(product: Pick<Product, 'variant_groups'>): VariantsDraft {
  return { groups: [...(product.variant_groups ?? [])].sort(byPosition).map(fromStored) };
}

/** The batch's body: the whole draft in order, ids for what exists, temp ids for what is new. */
export function draftToApply(draft: VariantsDraft, revision: string): VariantsApply {
  return {
    revision,
    groups: draft.groups.map((g) => {
      const ref = (key: string | null) => {
        const option = g.options.find((o) => o.key === key);
        return option ? (option.id ?? option.tempId ?? null) : null;
      };
      return {
        ...(g.id != null ? { id: g.id } : { temp_id: g.tempId }),
        name: g.name.trim(),
        default: ref(g.standard),
        requires_part: g.requiresPart,
        options: g.options.map((o) => ({
          ...(o.id != null ? { id: o.id } : { temp_id: o.tempId }),
          name: o.name.trim(),
        })),
      };
    }),
  };
}

/** Whether two drafts would send the same body — «nothing changed» closes without a request. */
export function sameDraft(a: VariantsDraft, b: VariantsDraft): boolean {
  return JSON.stringify(draftToApply(a, '').groups) === JSON.stringify(draftToApply(b, '').groups);
}

/** A new option at the end of a group (named by the caller); a group's first option does not
 *  become its standard by itself (A06). */
export function addOption(draft: VariantsDraft, group: string, name: string): VariantsDraft {
  const tempId = nextTemp();
  return {
    groups: draft.groups.map((g) =>
      g.key === group ? { ...g, options: [...g.options, { key: tempId, tempId, name }] } : g,
    ),
  };
}

/** A new group at the end; its first option is its standard. */
export function addGroup(draft: VariantsDraft, group: { name: string; options: string[] }): VariantsDraft {
  const tempId = nextTemp();
  const options = group.options.map((name) => {
    const id = nextTemp();
    return { key: id, tempId: id, name };
  });
  return {
    groups: [
      ...draft.groups,
      { key: tempId, tempId, name: group.name, options, standard: options[0]?.key ?? null, requiresPart: false },
    ],
  };
}

/** An option the server would not let go, back in its group at the place it had. */
export function restoreOption(draft: VariantsDraft, product: Pick<Product, 'variant_groups'>, optionId: number): VariantsDraft {
  const stored = (product.variant_groups ?? []).find((g) => g.options.some((o) => o.id === optionId));
  if (!stored) return draft;
  const options = [...stored.options].sort(byPosition);
  const index = options.findIndex((o) => o.id === optionId);
  const option = options[index];
  return {
    groups: draft.groups.map((g) => {
      if (g.id !== stored.id || g.options.some((o) => o.id === optionId)) return g;
      const next = [...g.options];
      next.splice(Math.min(index, next.length), 0, { key: optionKey(option.id), id: option.id, name: option.name });
      return { ...g, options: next };
    }),
  };
}

/** A group the server would not let go, back as it was stored, at the place it had. */
export function restoreGroup(draft: VariantsDraft, product: Pick<Product, 'variant_groups'>, groupId: number): VariantsDraft {
  if (draft.groups.some((g) => g.id === groupId)) return draft;
  const groups = [...(product.variant_groups ?? [])].sort(byPosition);
  const index = groups.findIndex((g) => g.id === groupId);
  if (index < 0) return draft;
  const next = [...draft.groups];
  next.splice(Math.min(index, next.length), 0, fromStored(groups[index]));
  return { groups: next };
}

/**
 * What the draft breaks before any request (D04). Not errors (A06, R11): an existing
 * group stored without a standard keeping none, an existing group stored empty staying
 * so. Errors: a blank name, a name taken by another group or by another option of the
 * group, a new group without options or without a standard, an emptied group that had
 * options, a standard taken away from a group that had one.
 */
export function draftErrors(draft: VariantsDraft): DraftError[] {
  const errors: DraftError[] = [];
  const groupNames = new Set<string>();
  for (const g of draft.groups) {
    const name = nameKey(g.name);
    if (name === '') errors.push({ group: g.key, code: 'groupNameEmpty' });
    else if (groupNames.has(name)) errors.push({ group: g.key, code: 'groupNameTaken' });
    else groupNames.add(name);

    const optionNames = new Set<string>();
    for (const o of g.options) {
      const optionName = nameKey(o.name);
      if (optionName === '') errors.push({ group: g.key, option: o.key, code: 'optionNameEmpty' });
      else if (optionNames.has(optionName)) errors.push({ group: g.key, option: o.key, code: 'optionNameTaken' });
      else optionNames.add(optionName);
    }

    const standardListed = g.standard != null && g.options.some((o) => o.key === g.standard);
    if (!g.stored) {
      if (g.options.length === 0) errors.push({ group: g.key, code: 'newGroupNoOptions' });
      if (!standardListed) errors.push({ group: g.key, code: 'newGroupNoStandard' });
      continue;
    }
    if (g.options.length === 0 && g.stored.hadOptions) errors.push({ group: g.key, code: 'emptied' });
    else if (g.standard == null ? g.stored.hadStandard : !standardListed) errors.push({ group: g.key, code: 'standardMissing' });
  }
  return errors;
}
