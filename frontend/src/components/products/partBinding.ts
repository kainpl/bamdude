import type { TFunction } from 'i18next';
import type { CombinationReason, ProductPartVariant } from '../../api/client';

/**
 * A part's binding to variant options, named (spec product-part-variant-combinations, п. 5,
 * 23–24). A part bound to options of several groups is in a kit when, in EVERY group it has
 * options in, the choice is ONE of them — «and» between groups, «or» within one; a part bound
 * to nothing is always in the kit.
 *
 * ⚠️ These helpers only name a binding and a reason. Whether a combination can be ordered is the
 * server's answer (`GET /products/{id}/combinations`, `POST /products/{id}/combination`); nothing
 * here decides availability.
 */

/** What the helpers read of a variant group: `VariantGroup` fits, and so does a lighter one. */
export interface BindingGroup {
  id: number;
  name: string;
  position?: number;
  options: readonly { id: number; name: string; position?: number }[];
}

type Positioned = { id: number; position?: number };

/** `position` is the authority; `id` only breaks its ties — the server's order. */
function byPosition(a: Positioned, b: Positioned): number {
  return (a.position ?? 0) - (b.position ?? 0) || a.id - b.id;
}

/** The groups the options fall in, each with its bound options, both in position order. */
function walk(groups: readonly BindingGroup[], optionIds: readonly number[]) {
  const wanted = new Set(optionIds);
  const out: { group: BindingGroup; options: BindingGroup['options'][number][] }[] = [];
  for (const group of [...groups].sort(byPosition)) {
    const options = [...group.options].sort(byPosition).filter((option) => wanted.has(option.id));
    if (options.length > 0) out.push({ group, options });
  }
  return out;
}

/**
 * `group id → its bound option ids`, groups and options in position order. An option the
 * groups do not hold is left out; no binding is an empty map.
 */
export function bindingByGroup(groups: readonly BindingGroup[], optionIds: readonly number[]): Map<number, number[]> {
  return new Map(walk(groups, optionIds).map(({ group, options }) => [group.id, options.map((o) => o.id)]));
}

/** The binding as name snapshots, in the shape the stock views receive from the server. */
export function bindingVariants(groups: readonly BindingGroup[], optionIds: readonly number[]): ProductPartVariant[] {
  return walk(groups, optionIds).map(({ group, options }) => ({ group: group.name, options: options.map((o) => o.name) }));
}

/** «400 · сплошна», «закрутка · 400, 600» — the values; `null` for a part in every configuration. */
export function bindingLabel(groups: readonly BindingGroup[], optionIds: readonly number[]): string | null {
  return variantsLabel(bindingVariants(groups, optionIds));
}

/** The short form of the server's snapshots: values within a group by a comma, groups by a dot. */
export function variantsLabel(variants: readonly ProductPartVariant[]): string | null {
  return variants.length === 0 ? null : variants.map((v) => v.options.join(', ')).join(' · ');
}

/** The long form, for a title: «Кришка: закрутка · Вага: 400, 600». */
export function variantsTitle(variants: readonly ProductPartVariant[]): string | null {
  return variants.length === 0 ? null : variants.map((v) => `${v.group}: ${v.options.join(', ')}`).join(' · ');
}

/**
 * One reason a combination cannot be ordered, as a sentence (п. 24): a value no part of the kit
 * names, nothing chosen in a group without a standard option (`option: null`), or a printed part
 * with no sliced source.
 */
export function reasonText(t: TFunction, reason: CombinationReason): string {
  if (reason.code === 'part_not_sliced') {
    return t('products.combinations.reason.partNotSliced', { part: reason.part ?? '' });
  }
  if (reason.option == null) {
    return t('products.combinations.reason.nothingChosen', { group: reason.group ?? '' });
  }
  return t('products.combinations.reason.optionWithoutPart', { group: reason.group ?? '', option: reason.option });
}
