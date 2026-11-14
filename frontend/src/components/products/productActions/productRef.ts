import type { ProductListItem } from '../../../api/client';

/** Every action a product's doors offer (WS-13 E8 F02), in the mockup's menu order. */
export type ProductAction = 'edit' | 'toOrder' | 'duplicate' | 'export' | 'hide' | 'show' | 'promote' | 'delete';

/** What an action needs of a product — a catalog row and the product's detail both carry it;
 *  the variant groups, where the caller has them, let «To order…» ask the combination at once. */
export type ProductRef = Pick<ProductListItem, 'id' | 'code' | 'name' | 'origin' | 'is_active'> &
  Partial<Pick<ProductListItem, 'variant_groups'>>;
