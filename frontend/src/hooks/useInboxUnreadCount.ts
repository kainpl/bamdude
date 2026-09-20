import { useQuery } from '@tanstack/react-query';
import { api } from '../api/client';

/**
 * ONE unread-count question for the whole app (the sidebar badge, the compact
 * header bell and the inbox page all read it). The WebSocket `inbox_item`
 * handler invalidates the `['inbox']` prefix, so this normally refreshes by
 * push; the interval is the disconnected-socket fallback, not the mechanism.
 */
export const INBOX_QUERY_KEY = ['inbox'] as const;
export const INBOX_UNREAD_KEY = ['inbox', 'unread-count'] as const;

export function useInboxUnreadCount(enabled = true) {
  return useQuery({
    queryKey: INBOX_UNREAD_KEY,
    queryFn: api.getInboxUnreadCount,
    enabled,
    staleTime: 30_000,
    refetchInterval: 60_000,
    select: (data) => data.unread_count,
  });
}
