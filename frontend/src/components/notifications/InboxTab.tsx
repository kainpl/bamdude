import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { CheckCheck, Trash2 } from 'lucide-react';
import { api, type InboxFilters, type InboxSeverity } from '../../api/client';
import { useToast } from '../../contexts/ToastContext';
import { Button } from '../Button';
import { ConfirmModal } from '../ConfirmModal';
import { LoadingBlock } from '../LoadingBlock';
import { formatRelativeTime } from '../../utils/date';
import { INBOX_QUERY_KEY } from '../../hooks/useInboxUnreadCount';
import { PaginationBar } from '../PaginationBar';
import { SeverityIcon } from './SeverityIcon';

const PERIOD_MS = { day: 24 * 3600e3, week: 7 * 24 * 3600e3, month: 30 * 24 * 3600e3 } as const;
type Period = keyof typeof PERIOD_MS | 'all';
// The page size the Archives and Inventory tables default to, and remembered
// the way they remember theirs, so a farm sees the same amount of list on every
// page it reads.
const PAGE_SIZE_KEY = 'bamdude-inbox-pageSize';
const DEFAULT_PAGE_SIZE = 24;

const selectClass =
  'px-2 py-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-sm text-white';

/**
 * The left edge of a row, by severity.
 *
 * Read notifications used to be drawn on `bg-bambu-dark` — which IS the page's
 * own background — so an inbox that had been read was one flat sheet with
 * nothing telling one entry from the next. They now sit on the card surface the
 * rest of the app uses, with a border, and this edge carries the severity the
 * icon already states, so a long list can be skimmed for the two levels that
 * matter without reading a word.
 */
const SEVERITY_EDGE: Record<InboxSeverity, string> = {
  error: 'border-l-status-error',
  warning: 'border-l-status-warning',
  info: 'border-l-bambu-dark-tertiary',
};

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
  const [page, setPage] = useState(1);
  const [perPage, setPerPage] = useState(() => {
    try {
      const stored = Number(localStorage.getItem(PAGE_SIZE_KEY));
      if ([12, 24, 48, 96, -1].includes(stored)) return stored;
    } catch { /* ignore */ }
    return DEFAULT_PAGE_SIZE;
  });

  // `since` is computed when the period changes, not on every render — a fresh
  // Date per render would be a fresh query key per render, and so an endless
  // refetch. The consequence is deliberate: the window FREEZES at the moment
  // the period was picked, so a tab left open for hours drifts away from a
  // literal "last 24 hours". It is also what Clear DELETES by — one value
  // bounds the list, read-all and the DELETE alike.
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
  // Any filter change is a new result set, so it starts at its first page.
  useEffect(() => setPage(1), [filters]);

  const { data: printers } = useQuery({ queryKey: ['printers'], queryFn: api.getPrinters });
  const list = useQuery({
    queryKey: [...INBOX_QUERY_KEY, 'list', filters, page, perPage],
    queryFn: () => api.getInbox({ ...filters, page, per_page: perPage }),
    placeholderData: (previous) => previous,
  });
  const items = list.data?.items ?? [];
  // A filter can shrink the result under the page you are on — asking for page
  // 9 of 2 answers an empty list, which reads as "nothing here" when there is
  // plenty. Step back instead of showing that.
  useEffect(() => {
    if (list.data && page > list.data.last_page) setPage(list.data.last_page);
  }, [list.data, page]);
  // Mark-all-read acts on the FILTERED set, so it is gated on the filtered set:
  // the server's `unread_count` is the whole inbox (it feeds the sidebar badge),
  // and gating on it leaves the button live while you look at a printer whose
  // rows are all read — firing a no-op that toasts "0 marked as read".
  const unreadShown = items.some((item) => item.read_at === null);

  const invalidate = () => queryClient.invalidateQueries({ queryKey: INBOX_QUERY_KEY });
  // `appQueryClient` installs a QueryCache onError but no MutationCache, so a
  // failed mutation is silent unless it says so itself — and two of these four
  // destroy data.
  const reportError = (e: Error) => showToast(e.message || t('common.error'), 'error');
  const markRead = useMutation({ mutationFn: api.markInboxRead, onSuccess: invalidate, onError: reportError });
  const remove = useMutation({ mutationFn: api.deleteInboxItem, onSuccess: invalidate, onError: reportError });
  const markAll = useMutation({
    mutationFn: () => api.markInboxAllRead(filters),
    onSuccess: (r) => {
      invalidate();
      showToast(t('notifications.center.inbox.markedRead', { count: r.updated }), 'success');
    },
    onError: reportError,
  });
  const clear = useMutation({
    mutationFn: () => api.clearInbox(filters),
    onSuccess: (r) => {
      setConfirmClear(false);
      invalidate();
      showToast(t('notifications.center.inbox.cleared', { count: r.deleted }), 'success');
    },
    // Close on failure too: the dialog's own spinner stops either way, and a
    // dialog sitting open with no reason given reads as "nothing happened".
    onError: (e: Error) => {
      setConfirmClear(false);
      reportError(e);
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
          aria-label={t('notifications.center.inbox.severityLabel')}
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
          aria-label={t('notifications.center.inbox.printerLabel')}
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
          <Button variant="secondary" size="sm" onClick={() => markAll.mutate()} disabled={markAll.isPending || !unreadShown}>
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
                className={`flex gap-3 p-3 rounded-lg border border-l-4 border-bambu-dark-tertiary ${SEVERITY_EDGE[item.severity] ?? SEVERITY_EDGE.info} ${
                  unread ? 'bg-bambu-dark-tertiary' : 'bg-bambu-dark-secondary'
                }`}
              >
                <SeverityIcon severity={item.severity} />
                {/* The row body is a real <button>, SIBLING to the delete one —
                    never its parent: a button inside a button is invalid HTML,
                    and nesting is what forced the old stopPropagation. Because
                    <button> takes phrasing content only, everything inside is a
                    <span>. (Precedent: FolderTreePicker.) */}
                <button
                  type="button"
                  className="min-w-0 flex-1 text-left"
                  aria-expanded={expandedId === item.id}
                  onClick={() => onRowClick(item.id, unread)}
                >
                  <span className="flex items-center gap-2">
                    {unread && (
                      <span
                        data-testid="unread-dot"
                        title={t('notifications.center.inbox.unread')}
                        className="w-2 h-2 rounded-full bg-bambu-green flex-shrink-0"
                      />
                    )}
                    <span className={`truncate ${unread ? 'text-white font-medium' : 'text-bambu-gray-light'}`}>{item.title}</span>
                  </span>
                  <span className={`block text-sm text-bambu-gray whitespace-pre-line ${expandedId === item.id ? '' : 'line-clamp-2'}`}>
                    {item.message}
                  </span>
                  <span className="flex items-center gap-2 text-xs text-bambu-gray mt-1">
                    {item.printer_name && (
                      <span className="px-2 py-0.5 rounded bg-bambu-dark-secondary">{item.printer_name}</span>
                    )}
                    <span>{formatRelativeTime(item.created_at, 'system', t)}</span>
                  </span>
                </button>
                <button
                  type="button"
                  aria-label={t('notifications.center.inbox.delete')}
                  disabled={remove.isPending && remove.variables === item.id}
                  className="self-start p-1 rounded text-bambu-gray hover:text-white hover:bg-bambu-dark-secondary disabled:opacity-50 disabled:cursor-not-allowed"
                  onClick={() => remove.mutate(item.id)}
                >
                  <Trash2 className="w-4 h-4" />
                </button>
              </li>
            );
          })}
        </ul>
      )}

      {list.data && (
        <PaginationBar
          page={list.data.current_page}
          totalPages={list.data.last_page}
          perPage={perPage}
          total={list.data.total}
          items={t('notifications.center.inbox.notificationCount', { count: list.data.total })}
          variant="bare"
          onPageChange={setPage}
          onPerPageChange={(size) => {
            setPerPage(size);
            setPage(1);
            try { localStorage.setItem(PAGE_SIZE_KEY, String(size)); } catch { /* ignore */ }
          }}
        />
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
