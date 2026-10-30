import { useQuery } from '@tanstack/react-query';
import { api } from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { farmPollInterval, farmQueryResumeOptions, farmRead, farmReadRetry, farmReadRetryDelay } from '../api/farmReadBudget';

/** Shell owns the timer; cards observe this same compact resource without polling. */
export function useQueueStatus(poll = false) {
  const { authEnabled, user, hasPermission } = useAuth();
  const enabled = !authEnabled || (!!user && hasPermission('queue:read'));
  return useQuery({
    ...farmQueryResumeOptions,
    queryKey: ['queues', 'status-summary'],
    queryFn: ({ signal }) => farmRead('queue-status-summary', signal, owned => api.getQueueStatusSummary({ signal: owned })),
    enabled,
    staleTime: 15_000,
    refetchInterval: poll ? query => farmPollInterval(15_000, query) : false,
    retry: farmReadRetry,
    retryDelay: farmReadRetryDelay,
  });
}

export function useMaintenanceAttention() {
  const { authEnabled, user, hasPermission } = useAuth();
  return useQuery({
    ...farmQueryResumeOptions,
    queryKey: ['maintenanceSummary'],
    queryFn: ({ signal }) => farmRead('maintenance-summary', signal, owned => api.getMaintenanceSummary({ signal: owned })),
    enabled: !authEnabled || (!!user && hasPermission('maintenance:read')),
    refetchInterval: query => farmPollInterval(60_000, query),
    retry: farmReadRetry,
    retryDelay: farmReadRetryDelay,
  });
}
