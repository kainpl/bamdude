// The catalogue / farm-read key of the current authorisation context (spec
// slicing-setup R10): `g<generation>` once the store's tuple is the one this
// render sees, `null` before that. A null key means: no request, no data, no
// refresh — so the first commit after a rights change never reads the list of
// the context before it.
import { useEffect, useLayoutEffect, useRef, useSyncExternalStore } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import { notePresetAuthTuple, presetContextSnapshot, subscribePresetContext } from '../../utils/presetContext';

/** The one status pair the tuple reads its cloud link states from. */
export const CLOUD_STATUS_KEY = ['cloudStatus'] as const;
export const ORCA_STATUS_KEY = ['orcaCloudStatus'] as const;

export function usePresetAuthKey(): string | null {
  const { user, hasPermission } = useAuth();
  const canBambu = hasPermission('cloud:auth');
  const canOrca = hasPermission('orca_cloud:auth');
  const bambu = useQuery({ queryKey: CLOUD_STATUS_KEY, queryFn: api.getCloudStatus, enabled: canBambu, staleTime: 60_000 });
  const orca = useQuery({ queryKey: ORCA_STATUS_KEY, queryFn: api.orcaCloudStatus, enabled: canOrca, staleTime: 60_000 });
  // A link state still being read is not yet part of any context.
  const pending = (canBambu && bambu.isPending) || (canOrca && orca.isPending);
  const tuple = JSON.stringify([
    user?.id ?? null,
    hasPermission('library:upload'),
    hasPermission('printers:read'),
    canBambu,
    canOrca,
    canBambu ? (bambu.data?.is_authenticated ?? 'unknown') : null,
    canOrca ? (orca.data?.connected ?? 'unknown') : null,
  ]);
  const snapshot = useSyncExternalStore(subscribePresetContext, presetContextSnapshot);

  useLayoutEffect(() => {
    if (!pending) notePresetAuthTuple(tuple);
  }, [pending, tuple]);

  // A confirmed binding change re-reads the link states the tuple is made of.
  const queryClient = useQueryClient();
  const seenBinding = useRef(snapshot.binding);
  useEffect(() => {
    if (snapshot.binding === seenBinding.current) return;
    seenBinding.current = snapshot.binding;
    void queryClient.invalidateQueries({ queryKey: CLOUD_STATUS_KEY });
    void queryClient.invalidateQueries({ queryKey: ORCA_STATUS_KEY });
  }, [queryClient, snapshot.binding]);

  return !pending && snapshot.tuple === tuple ? `g${snapshot.generation}` : null;
}
