import { useMemo, useState } from 'react';
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { CheckCheck, Loader2, Trash2 } from 'lucide-react';
import { api, type InboxFilters, type InboxSeverity } from '../../api/client';
import { useToast } from '../../contexts/ToastContext';
import { Button } from '../Button';
import { ConfirmModal } from '../ConfirmModal';
import { LoadingBlock } from '../LoadingBlock';
import { formatRelativeTime } from '../../utils/date';
import { INBOX_QUERY_KEY } from '../../hooks/useInboxUnreadCount';
import { SeverityIcon } from './SeverityIcon';

const PERIOD_MS = { day: 24 * 3600e3, week: 7 * 24 * 3600e3, month: 30 * 24 * 3600e3 } as const;
type Period = keyof typeof PERIOD_MS | 'all';
const PAGE = 50;

const selectClass =
  'px-2 py-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-sm text-white';

export function InboxTab() {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();

  const [severity, setSeverity] = useState<InboxSeverity | ''>('');
  const [printerId, setPrinterId] = useState<number | ''>('');
  const [unreadOnly, setUnreadOnly] = useState(false);
  const [period, setPeriod] = useState<Period>('all');
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const [confirmClear, setConfirmClear] = useState(false);

  // `since` is computed when the period changes, not on every render — a fresh
  // Date per render would be a fresh query key per render.
  const since = useMemo(
    () => (period === 'all' ? undefined : new Date(Date.now() - PERIOD_MS[period]).toISOString()),
    [period],
  );
  const filters = useMemo<InboxFilters>(
    () => ({
      ...(severity ? { severity } : {}),
      ...(printerId ? { printer_id: printerId } : {}),
      ...(unreadOnly ? { unread_only: true } : {}),
      ...(since ? { since } : {}),
    }),
    [severity, printerId, unreadOnly, since],
  );

  const { data: printers } = useQuery({ queryKey: ['printers'], queryFn: api.getPrinters });
  const list = useInfiniteQuery({
    queryKey: [...INBOX_QUERY_KEY, 'list', filters],
    queryFn: ({ pageParam }) => api.getInbox({ ...filters, before_id: pageParam ?? undefined, limit: PAGE }),
    initialPageParam: null as number | null,
    getNextPageParam: (last) => last.next_before_id,
  });
  const items = list.data?.pages.flatMap((p) => p.items) ?? [];

  const invalidate = () => queryClient.invalidateQueries({ queryKey: INBOX_QUERY_KEY });
  const markRead = useMutation({ mutationFn: api.markInboxRead, onSuccess: invalidate });
  const remove = useMutation({ mutationFn: api.deleteInboxItem, onSuccess: invalidate });
  const markAll = useMutation({
    mutationFn: () => api.markInboxAllRead(filters),
    onSuccess: (r) => {
      invalidate();
      showToast(t('notifications.center.inbox.markedRead', { count: r.updated }), 'success');
    },
  });
  const clear = useMutation({
    mutationFn: () => api.clearInbox(filters),
    onSuccess: (r) => {
      setConfirmClear(false);
      invalidate();
      showToast(t('notifications.center.inbox.cleared', { count: r.deleted }), 'success');
    },
  });

  const onRowClick = (id: number, unread: boolean) => {
    if (unread) markRead.mutate(id);
    setExpandedId((cur) => (cur === id ? null : id));
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <select
          aria-label={t('notifications.center.inbox.allSeverities')}
          className={selectClass}
          value={severity}
          onChange={(e) => setSeverity(e.target.value as InboxSeverity | '')}
        >
          <option value="">{t('notifications.center.inbox.allSeverities')}</option>
          {(['error', 'warning', 'info'] as const).map((s) => (
            <option key={s} value={s}>{t(`notifications.center.severity.${s}`)}</option>
          ))}
        </select>
        <select
          aria-label={t('notifications.center.inbox.allPrinters')}
          className={selectClass}
          value={printerId}
          onChange={(e) => setPrinterId(e.target.value ? Number(e.target.value) : '')}
        >
          <option value="">{t('notifications.center.inbox.allPrinters')}</option>
          {printers?.map((p) => (
            <option key={p.id} value={p.id}>{p.name}</option>
          ))}
        </select>
        <select
          aria-label={t('notifications.center.inbox.periodLabel')}
          className={selectClass}
          value={period}
          onChange={(e) => setPeriod(e.target.value as Period)}
        >
          {(['all', 'day', 'week', 'month'] as const).map((p) => (
            <option key={p} value={p}>{t(`notifications.center.inbox.period.${p}`)}</option>
          ))}
        </select>
        <label className="flex items-center gap-2 text-sm text-bambu-gray">
          <input type="checkbox" checked={unreadOnly} onChange={(e) => setUnreadOnly(e.target.checked)} />
          {t('notifications.center.inbox.unreadOnly')}
        </label>
        <div className="ml-auto flex items-center gap-2">
          <Button variant="secondary" size="sm" onClick={() => markAll.mutate()} disabled={markAll.isPending || items.length === 0}>
            <CheckCheck className="w-4 h-4" />
            {t('notifications.center.inbox.markAllRead')}
          </Button>
          <Button variant="outline" size="sm" onClick={() => setConfirmClear(true)} disabled={items.length === 0}>
            <Trash2 className="w-4 h-4" />
            {t('notifications.center.inbox.clear')}
          </Button>
        </div>
      </div>

      {list.isLoading ? (
        <LoadingBlock label={t('common.loading')} className="h-40 text-bambu-gray" />
      ) : items.length === 0 ? (
        <div className="py-12 text-center text-bambu-gray">
          <p className="text-white">{t('notifications.center.inbox.empty')}</p>
          <p className="text-sm mt-1">{t('notifications.center.inbox.emptyHint')}</p>
        </div>
      ) : (
        <ul className="space-y-2">
          {items.map((item) => {
            const unread = item.read_at === null;
            return (
              <li
                key={item.id}
                className={`flex gap-3 p-3 rounded-lg cursor-pointer ${unread ? 'bg-bambu-dark-tertiary' : 'bg-bambu-dark'}`}
                onClick={() => onRowClick(item.id, unread)}
              >
                <SeverityIcon severity={item.severity} />
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    {unread && (
                      <span
                        data-testid="unread-dot"
                        title={t('notifications.center.inbox.unread')}
                        className="w-2 h-2 rounded-full bg-bambu-green flex-shrink-0"
                      />
                    )}
                    <span className={`truncate ${unread ? 'text-white font-medium' : 'text-bambu-gray-light'}`}>{item.title}</span>
                  </div>
                  <p className={`text-sm text-bambu-gray whitespace-pre-line ${expandedId === item.id ? '' : 'line-clamp-2'}`}>
                    {item.message}
                  </p>
                  <div className="flex items-center gap-2 text-xs text-bambu-gray mt-1">
                    {item.printer_name && (
                      <span className="px-2 py-0.5 rounded bg-bambu-dark-secondary">{item.printer_name}</span>
                    )}
                    <span>{formatRelativeTime(item.created_at, 'system', t)}</span>
                  </div>
                </div>
                <button
                  type="button"
                  aria-label={t('notifications.center.inbox.delete')}
                  className="self-start p-1 rounded text-bambu-gray hover:text-white hover:bg-bambu-dark-secondary"
                  onClick={(e) => {
                    e.stopPropagation();
                    remove.mutate(item.id);
                  }}
                >
                  <Trash2 className="w-4 h-4" />
                </button>
              </li>
            );
          })}
        </ul>
      )}

      {list.hasNextPage && (
        <div className="flex justify-center">
          <Button variant="ghost" size="sm" onClick={() => list.fetchNextPage()} disabled={list.isFetchingNextPage}>
            {list.isFetchingNextPage ? <Loader2 className="w-4 h-4 animate-spin" /> : null}
            {t('notifications.center.inbox.loadMore')}
          </Button>
        </div>
      )}

      {confirmClear && (
        <ConfirmModal
          title={t('notifications.center.inbox.clearConfirmTitle')}
          message={t('notifications.center.inbox.clearConfirmBody')}
          variant="danger"
          isLoading={clear.isPending}
          onConfirm={() => clear.mutate()}
          onCancel={() => setConfirmClear(false)}
        />
      )}
    </div>
  );
}
