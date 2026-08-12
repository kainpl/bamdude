/**
 * The coordinator's state, shared by every badge that shows it.
 *
 * One query key with a stale time, so five mounted badges cost one request
 * rather than five: this is rendered on the printers page, in the switchbar and
 * on every Zigbee plug card at once.
 */

import { useQuery } from '@tanstack/react-query';

import { api } from '../../api/client';

export function useZigbeeStatus() {
  const { data: status } = useQuery({
    queryKey: ['zigbee-status'],
    queryFn: api.getZigbeeStatus,
    staleTime: 30_000,
  });

  return {
    status,
    // Only `error` counts as down. `disabled` is a correct configuration for an
    // install that does not want Zigbee, and `starting` is transient — flagging
    // either would train the operator to ignore the badge.
    isDown: status?.state === 'error',
  };
}
