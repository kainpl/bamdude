import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { ChevronLeft, FolderKanban, Loader2, Search, X, XCircle } from 'lucide-react';
import { api } from '../../api/client';
import type { Archive } from '../../api/client';
import { useToast } from '../../contexts/ToastContext';
import { selectableProjects } from '../../utils/projects';

interface AddToOrderMenuProps {
  archive: Archive;
  onDone: () => void;
}

/**
 * File one archive under an order, and then under one of its lines.
 *
 * ⚠️ **The offer rule is `selectableProjects`, not an inline status test.**
 * The ArchivesPage carried two copies of this menu, each filtering
 * `status === 'active'` by hand — stricter than the shared rule, so an
 * archive bound to a closed order saw its own order missing from the list.
 *
 * ⚠️ **Two levels, because a line only means something inside its order.**
 * The line list is fetched for the chosen order and the server rejects (400) a
 * line from any other, so the second level cannot offer a mismatch.
 */
export function AddToOrderMenu({ archive, onDone }: AddToOrderMenuProps) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const [query, setQuery] = useState('');
  const [chosenOrderId, setChosenOrderId] = useState<number | null>(null);

  const { data: orders, isLoading } = useQuery({
    queryKey: ['projects', {}],
    queryFn: () => api.getOrders({}),
  });

  const offered = useMemo(() => {
    const selectable = selectableProjects(orders, archive.project_id != null ? [archive.project_id] : null);
    const q = query.trim().toLowerCase();
    const matching = q ? selectable.filter((o) => o.name.toLowerCase().includes(q)) : selectable;
    return [...matching].sort((a, b) => a.name.localeCompare(b.name));
  }, [orders, archive.project_id, query]);

  const { data: chosenOrder, isLoading: linesLoading } = useQuery({
    queryKey: ['project', chosenOrderId],
    queryFn: () => api.getOrder(chosenOrderId as number),
    enabled: chosenOrderId != null,
  });

  const assign = useMutation({
    mutationFn: ({ orderId, lineId }: { orderId: number; lineId: number | null }) =>
      api.addArchivesToOrder(orderId, [archive.id], lineId),
    onSuccess: (_, { orderId }) => {
      queryClient.invalidateQueries({ queryKey: ['archives'] });
      queryClient.invalidateQueries({ queryKey: ['project', orderId] });
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      showToast(t('archives.toast.projectUpdated'));
      onDone();
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const unbind = useMutation({
    // Not `addArchivesToOrder` — there is no order to add to. Clearing the
    // line alongside is not optional: a line without its order is a row the
    // server would refuse on the next edit.
    mutationFn: () => api.updateArchive(archive.id, { project_id: null, project_line_id: null }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['archives'] });
      queryClient.invalidateQueries({ queryKey: ['projects'] });
      showToast(t('archives.toast.projectUpdated'));
      onDone();
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const isPending = assign.isPending || unbind.isPending;
  const lines = chosenOrder?.lines ?? [];

  return (
    <div className="fixed inset-0 bg-black/50 backdrop-blur-sm flex items-center justify-center z-50 p-4">
      <div className="bg-bambu-dark-secondary rounded-lg w-full max-w-sm border border-bambu-dark-tertiary flex flex-col max-h-[80vh]">
        <div className="p-3 border-b border-bambu-dark-tertiary flex items-center gap-2">
          {chosenOrderId != null && (
            <button
              type="button"
              onClick={() => setChosenOrderId(null)}
              className="p-1 rounded hover:bg-bambu-dark text-bambu-gray hover:text-white"
              aria-label={t('common.back')}
            >
              <ChevronLeft className="w-4 h-4" />
            </button>
          )}
          <h2 className="text-sm font-semibold text-white flex-1 flex items-center gap-2">
            <FolderKanban className="w-4 h-4 text-bambu-green" />
            {t('archives.menu.addToOrder')}
          </h2>
          <button
            type="button"
            onClick={onDone}
            aria-label={t('common.close')}
            className="p-1 rounded hover:bg-bambu-dark text-bambu-gray hover:text-white"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        {chosenOrderId == null ? (
          <>
            <div className="p-2 border-b border-bambu-dark-tertiary">
              <div className="relative">
                <Search className="w-3.5 h-3.5 text-bambu-gray absolute left-2 top-1/2 -translate-y-1/2 pointer-events-none" />
                <input
                  type="text"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder={t('archives.menu.searchOrders')}
                  className="w-full pl-7 pr-2 py-1.5 text-xs bg-bambu-dark border border-bambu-dark-tertiary rounded text-white placeholder:text-bambu-gray focus:outline-none focus:border-bambu-green"
                />
              </div>
            </div>

            <div className="overflow-y-auto py-1">
              {archive.project_id != null && (
                <button
                  type="button"
                  disabled={isPending}
                  onClick={() => unbind.mutate()}
                  className="w-full flex items-center gap-2 px-3 py-2 text-sm text-left text-red-700 dark:text-red-400 hover:bg-red-50 dark:hover:bg-red-400/10 disabled:opacity-50"
                >
                  <XCircle className="w-4 h-4 flex-shrink-0" />
                  {t('archives.menu.removeFromOrder')}
                </button>
              )}

              {isLoading && (
                <p className="flex items-center gap-2 px-3 py-2 text-sm text-bambu-gray">
                  <Loader2 className="w-4 h-4 animate-spin" />
                  {t('archives.menu.loading')}
                </p>
              )}

              {!isLoading && offered.length === 0 && (
                <p className="px-3 py-2 text-sm text-bambu-gray">{t('archives.menu.noOrdersAvailable')}</p>
              )}

              {offered.map((order) => (
                <button
                  key={order.id}
                  type="button"
                  disabled={isPending}
                  onClick={() => setChosenOrderId(order.id)}
                  className="w-full flex items-center gap-2 px-3 py-2 text-sm text-left text-white hover:bg-bambu-dark-tertiary disabled:opacity-50"
                >
                  <span
                    className="w-3 h-3 rounded-full flex-shrink-0"
                    style={{ backgroundColor: order.color || '#888' }}
                  />
                  <span className="flex-1 min-w-0 truncate">{order.name}</span>
                </button>
              ))}
            </div>
          </>
        ) : (
          <div className="overflow-y-auto py-1">
            <button
              type="button"
              disabled={isPending}
              onClick={() => assign.mutate({ orderId: chosenOrderId, lineId: null })}
              className="w-full px-3 py-2 text-sm text-left text-white hover:bg-bambu-dark-tertiary disabled:opacity-50"
            >
              {t('archives.menu.noLine')}
            </button>

            {linesLoading && (
              <p className="flex items-center gap-2 px-3 py-2 text-sm text-bambu-gray">
                <Loader2 className="w-4 h-4 animate-spin" />
                {t('archives.menu.loading')}
              </p>
            )}

            {lines.map((line) => (
              <button
                key={line.id}
                type="button"
                disabled={isPending}
                onClick={() => assign.mutate({ orderId: chosenOrderId, lineId: line.id })}
                className="w-full px-3 py-2 text-sm text-left text-white hover:bg-bambu-dark-tertiary disabled:opacity-50"
              >
                {`${line.product_name} × ${line.quantity}${line.material ? ` [${line.material}]` : ''}`}
              </button>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
