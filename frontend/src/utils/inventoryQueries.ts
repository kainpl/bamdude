import type { QueryClient } from '@tanstack/react-query';

/** React Query key for GET /inventory/locations (catalog + spool counts). */
export const inventoryLocationsQueryKey = ['inventory-locations'] as const;

export function invalidateInventoryLocations(queryClient: QueryClient) {
  return queryClient.invalidateQueries({ queryKey: inventoryLocationsQueryKey });
}

/**
 * The three server-computed forecast feeds. `['inventory-forecast']` does NOT
 * prefix the other two — TanStack matches key ARRAYS element-wise, and
 * `'inventory-forecast-chart'` is a different first element, not a child — so
 * all three must be named.
 */
export const forecastQueryKeys = [
  ['inventory-forecast'],
  ['inventory-forecast-chart'],
  ['inventory-forecast-logistics'],
] as const;

/** Refresh every forecast feed after something the engine computes over moved. */
export function invalidateForecastQueries(queryClient: QueryClient) {
  return Promise.all(
    forecastQueryKeys.map((queryKey) => queryClient.invalidateQueries({ queryKey: [...queryKey] })),
  );
}

/**
 * Refresh spool list, location counts AND the forecast after inventory
 * mutations.
 *
 * ⚠️ The forecast is not an independent view of a different dataset: every
 * spool create/edit/delete/archive/restore/reset moves the totals, the rate
 * tier, the alerts, the chart and the logistics rows the panel renders. And
 * every one of those mutation surfaces (the header buttons, the stats bar,
 * the bulk toolbar) renders while the Forecast tab is open — so a spool
 * mutation that skipped this left the panel asserting pre-mutation numbers
 * for the whole sitting, healing only on a window refocus past `staleTime`.
 */
export function invalidateSpoolAndLocationQueries(
  queryClient: QueryClient,
  spoolsQueryKey: readonly string[],
) {
  return Promise.all([
    queryClient.invalidateQueries({ queryKey: [...spoolsQueryKey] }),
    invalidateInventoryLocations(queryClient),
    invalidateForecastQueries(queryClient),
  ]);
}
