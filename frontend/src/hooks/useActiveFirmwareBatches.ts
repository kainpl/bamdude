import { useQuery } from '@tanstack/react-query';
import { firmwareApi } from '../api/client';

export function useActiveFirmwareBatches(enabled = true) {
  return useQuery({
    queryKey: ['firmware-active-batches'],
    queryFn: async () => (await firmwareApi.listBatches(true)).filter((batch) => batch.status === 'running'),
    enabled,
    refetchInterval: (query) => query.state.data?.length ? 2000 : 10_000,
  });
}
