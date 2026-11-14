import { queryOptions, useQuery, type UseQueryResult } from '@tanstack/react-query';
import type { TFunction } from 'i18next';
import { api, ApiError } from '../../api/client';
import type { CombinationReason, CombinationState } from '../../api/client';
import { reasonText } from '../products/partBinding';

/**
 * Whether an order can take a product's choice of variant options, as the server answers it
 * (spec product-part-variant-combinations, п. 18, 25) — for the two order dialogs: a line's
 * configuration (`useCombinationState`) and «Add to order», which asks for every picked product
 * at once (`combinationQuery` + `combinationView` under one `useQueries`).
 *
 * ⚠️ The server decides (`POST /products/{id}/combination`); this module only carries its answer.
 * It is a courtesy: adding a line or changing a line's choice is refused by the server itself
 * (409 `combination_unavailable`), so a dialog never trusts its own reading over that refusal.
 *
 * `enabled` is false — and nothing is asked — for a product without variant groups, a parts
 * line and a caller without the stock's read (the route's gate, as for the add dialog's kits):
 * the gate is then `open` and the server alone decides.
 */

/** The query key's first element; the second is the product id, the third the choices' key. */
export const COMBINATION_STATE_KEY = 'product-combination-state';

/** `open` — nothing stands in the way (available, nothing to ask, or the ask failed: the server
 *  decides); `checking` — the answer for THIS choice is not in yet; `closed` — not orderable. */
export type CombinationGate = 'open' | 'checking' | 'closed';

/** One value beside the current choice: `here` — the combination stays available with this value
 *  and the other groups as chosen; `anywhere` — the value is in at least one available combination
 *  (`null` — unknown: the product has too many combinations to enumerate). */
export interface OptionFlags {
  here: boolean;
  anywhere: boolean | null;
}

export interface CombinationView {
  gate: CombinationGate;
  /** `null` while there is no answer for this choice (being asked, failed, or not asked). */
  available: boolean | null;
  /** Why this choice is not available — only from an answer for this very choice. */
  reasons: CombinationReason[];
  /** Option id → its flags. `here` is read only off an answer for this choice (an older answer
   *  marks nothing); `anywhere` does not depend on the choice, so an older answer keeps it. */
  optionFlags: Map<number, OptionFlags>;
  isFetching: boolean;
  /** The last ask failed and nothing answers this choice. */
  isError: boolean;
  refetch: () => void;
}

/** The choices as one stable string, by group id — the query key and the dialogs' comparisons. */
export function choicesKey(choices: Record<number, number>): string {
  return Object.entries(choices)
    .map(([group, option]) => [Number(group), option] as const)
    .sort((a, b) => a[0] - b[0])
    .map(([group, option]) => `${group}=${option}`)
    .join(',');
}

/** The one query of a product's choice — the line dialog's hook and the add dialog's list share it. */
export function combinationQuery(productId: number, choices: Record<number, number>, enabled: boolean) {
  return queryOptions({
    queryKey: [COMBINATION_STATE_KEY, productId, choicesKey(choices)],
    queryFn: () => api.describeCombination(productId, choices),
    enabled,
    // The last answer of the SAME product stays while another choice is asked, so a value in no
    // available combination does not flicker back to clickable; it decides nothing (see the gate).
    placeholderData: (previous: CombinationState | undefined, previousQuery) =>
      previousQuery?.queryKey[1] === productId ? previous : undefined,
    retry: false,
  });
}

const NO_FLAGS = new Map<number, OptionFlags>();

/** Nothing is known yet — the add dialog's answer for a product it has not seen listed. */
export const CHECKING_VIEW: CombinationView = {
  gate: 'checking',
  available: null,
  reasons: [],
  optionFlags: NO_FLAGS,
  isFetching: false,
  isError: false,
  refetch: () => {},
};

const OPEN_VIEW: CombinationView = { ...CHECKING_VIEW, gate: 'open' };

function flagsOf(state: CombinationState | undefined, own: boolean): Map<number, OptionFlags> {
  if (!state) return NO_FLAGS;
  return new Map(
    state.options.map((o) => [o.option_id, { here: own ? o.available_here : true, anywhere: o.available_anywhere }]),
  );
}

/** What a dialog reads off the query of a choice. */
export function combinationView(query: UseQueryResult<CombinationState>, enabled: boolean): CombinationView {
  if (!enabled) return OPEN_VIEW;
  // An answer for THIS choice — perhaps being asked again; a placeholder answers another one.
  const own = query.data !== undefined && !query.isPlaceholderData ? query.data : undefined;
  const gate: CombinationGate =
    query.isFetching || query.isPlaceholderData
      ? 'checking'
      : own
        ? own.available
          ? 'open'
          : 'closed'
        : query.isError
          ? 'open'
          : 'checking';
  return {
    gate,
    available: own ? own.available : null,
    reasons: own && !own.available ? own.reasons : [],
    optionFlags: flagsOf(query.data, own !== undefined),
    isFetching: query.isFetching,
    isError: query.isError && !own && !query.isFetching,
    refetch: () => void query.refetch(),
  };
}

/** The state of one product's choice (the line dialog). */
export function useCombinationState(productId: number, choices: Record<number, number>, enabled: boolean): CombinationView {
  return combinationView(useQuery(combinationQuery(productId, choices, enabled)), enabled);
}

/** The server refused a closed combination (409 `combination_unavailable`) — by its code, never its text. */
export function isCombinationRefusal(error: unknown): error is ApiError {
  return error instanceof ApiError && error.code === 'combination_unavailable';
}

/** The reasons as one line of sentences. */
export function reasonsText(t: TFunction, reasons: readonly CombinationReason[]): string {
  return reasons.map((reason) => reasonText(t, reason)).join('; ');
}
