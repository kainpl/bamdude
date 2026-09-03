import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import { Clock, Layers, ListTodo, Package } from 'lucide-react';
import { api, withStreamToken } from '../../api/client';
import type { PrintQueueItem } from '../../api/client';
import { formatDuration, formatETA, type TimeFormat } from '../../utils/date';

interface OrderQueueProps {
  orderId: number;
}

/**
 * What this order has on a printer right now, and what is waiting.
 *
 * The queue is global; this panel is the order's slice of it, filtered client
 * side exactly as the old project page did — there is no per-order queue
 * endpoint, and adding one to answer a panel would be a second source of truth
 * for the queue's contents.
 *
 * Informational only, by design: no pause / cancel / reorder here. Those live
 * on the queue page, where the whole picture is, and a farm decision taken
 * from inside one order is the decision most likely to be wrong.
 *
 * The order comes from the SAME `['project', id]` cache entry the page already
 * filled — the component takes an id so it stays independent of the page's
 * render, not so it can fetch a second copy.
 */
export function OrderQueue({ orderId }: OrderQueueProps) {
  const { t } = useTranslation();

  const { data: order } = useQuery({
    queryKey: ['project', orderId],
    queryFn: () => api.getOrder(orderId),
  });
  const { data: settings } = useQuery({ queryKey: ['settings'], queryFn: api.getSettings, staleTime: 60_000 });
  const { data: printingAll } = useQuery({
    queryKey: ['queue', 'printing'],
    queryFn: () => api.getQueue(undefined, 'printing'),
    refetchInterval: 10_000,
  });
  const { data: pendingAll } = useQuery({
    queryKey: ['queue', 'pending'],
    queryFn: () => api.getQueue(undefined, 'pending'),
    refetchInterval: 30_000,
  });

  const mine = (items: PrintQueueItem[] | undefined) =>
    (items ?? []).filter((item) => item.project_id === orderId);
  const printing = mine(printingAll);
  const pending = mine(pendingAll);

  // A finished order with nothing running has nothing to say here; an active
  // one answers "is anything moving?" even when the answer is no.
  if (printing.length === 0 && pending.length === 0 && order?.status !== 'active') return null;

  const lineName = (item: PrintQueueItem) =>
    order?.lines.find((line) => line.id === item.project_line_id)?.product_name;

  const timeFormat: TimeFormat = settings?.time_format || 'system';

  return (
    <section className="space-y-3">
      <div className="flex items-center justify-between gap-4">
        <h2 className="text-lg font-semibold text-white flex items-center gap-2">
          <ListTodo className="w-5 h-5" />
          {t('orders.queue.title')}
        </h2>
        {/* No `?project=` — QueuePage reads only `view` off the URL, so the
            filter this link was copied with was never applied. */}
        <Link to="/queue" className="text-sm text-bambu-green hover:underline">
          {t('orders.queue.viewAll')}
        </Link>
      </div>

      {printing.length === 0 && pending.length === 0 ? (
        <p className="text-sm text-bambu-gray/70 italic">{t('orders.queue.empty')}</p>
      ) : (
        <>
          {printing.length > 0 && (
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
              {printing.map((item) => (
                <CurrentPrintInfoCard
                  key={item.id}
                  item={item}
                  timeFormat={timeFormat}
                  lineName={lineName(item)}
                />
              ))}
            </div>
          )}

          {pending.length > 0 && (
            <ul className="space-y-2">
              {pending.map((item) => (
                <PendingRow key={item.id} item={item} lineName={lineName(item)} />
              ))}
            </ul>
          )}
        </>
      )}
    </section>
  );
}

function LineLabel({ name }: { name: string | undefined }) {
  const { t } = useTranslation();
  if (!name) return null;
  return <p className="text-xs text-bambu-gray truncate">{t('orders.queue.line', { name })}</p>;
}

/**
 * A waiting job: what it is, where it is going, which line it answers to.
 *
 * ⚠️ `archive_thumbnail` / `library_file_thumbnail` are the server's DISK
 * paths, not URLs — they say a picture exists, and the id says where to ask
 * for it. Feeding the path straight to an `<img src>` is a broken image on
 * every row that has one.
 */
function PendingRow({ item, lineName }: { item: PrintQueueItem; lineName: string | undefined }) {
  const thumbnail =
    item.archive_id != null && item.archive_thumbnail
      ? api.getArchiveThumbnail(item.archive_id)
      : item.library_file_id != null && item.library_file_thumbnail
        ? api.getLibraryFileThumbnailUrl(item.library_file_id)
        : null;
  const name = item.archive_name || item.library_file_name || `#${item.id}`;

  return (
    <li className="flex items-center gap-3 rounded-lg bg-bambu-dark-secondary border border-bambu-dark-tertiary p-2">
      {thumbnail ? (
        <img src={thumbnail} alt="" className="w-10 h-10 rounded object-contain bg-bambu-dark flex-shrink-0" />
      ) : (
        <div className="w-10 h-10 rounded bg-bambu-dark flex items-center justify-center flex-shrink-0">
          <Package className="w-4 h-4 text-bambu-gray" />
        </div>
      )}
      <div className="min-w-0 flex-1">
        <p className="text-sm text-white truncate">{name}</p>
        <LineLabel name={lineName} />
      </div>
      {item.printer_name && (
        <span className="text-xs text-bambu-gray flex-shrink-0">{item.printer_name}</span>
      )}
    </li>
  );
}

interface CurrentPrintInfoCardProps {
  item: PrintQueueItem;
  timeFormat: TimeFormat;
  lineName: string | undefined;
}

/**
 * Info-only current-print card, moved here from the old project page. Mirrors
 * the layout of QueueCard's live-print block (thumbnail + name + progress +
 * ETA/layer) but renders nothing interactive — this panel exists solely to
 * surface which of the order's jobs are live on which printer. The progress
 * bar uses the same green / amber (paused) fill as the printers and queue
 * pages.
 */
function CurrentPrintInfoCard({ item, timeFormat, lineName }: CurrentPrintInfoCardProps) {
  const { t } = useTranslation();
  const { data: status } = useQuery({
    queryKey: ['printerStatus', item.printer_id],
    queryFn: () => api.getPrinterStatus(item.printer_id as number),
    enabled: item.printer_id != null,
    refetchInterval: 5000,
  });

  const name =
    status?.subtask_name
    || status?.current_print
    || item.archive_name
    || item.library_file_name
    || `#${item.id}`;
  const thumbnail = status?.cover_url;
  const isLive = status?.state === 'RUNNING' || status?.state === 'PAUSE';
  const progress = status?.progress ?? 0;

  return (
    <div className="p-3 rounded-lg bg-bambu-dark-secondary border border-bambu-dark-tertiary">
      <div className="flex items-start gap-3">
        {thumbnail ? (
          <img
            src={withStreamToken(thumbnail)}
            alt=""
            className="w-20 h-20 rounded-lg object-contain flex-shrink-0 bg-bambu-dark-tertiary"
          />
        ) : (
          <div className="w-20 h-20 rounded-lg bg-bambu-dark-tertiary flex-shrink-0" />
        )}
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5 mb-1">
            <p className="text-sm text-bambu-gray">{t('queueCard.currentPrint')}</p>
            {item.printer_name && item.printer_id != null && (
              <Link
                to={`/#printer-${item.printer_id}`}
                className="text-xs text-bambu-gray/70 hover:text-bambu-green transition-colors"
                title={t('queueCard.goToPrinter')}
              >
                · {item.printer_name}
              </Link>
            )}
          </div>
          <p className="text-sm text-white truncate mb-1">{name}</p>
          <LineLabel name={lineName} />
          {isLive ? (
            <>
              <div className="flex items-center gap-2 mt-1">
                <div className="flex-1 bg-bambu-dark-tertiary rounded-full h-2 overflow-hidden">
                  <div
                    className={`${status?.state === 'PAUSE' ? 'bg-status-warning' : 'bg-bambu-green'} h-2 rounded-full transition-all`}
                    style={{ width: `${progress}%` }}
                  />
                </div>
                <span className="text-sm text-white font-medium flex-shrink-0">{Math.round(progress)}%</span>
              </div>
              <div className="flex items-center gap-3 mt-2 text-xs text-bambu-gray">
                {status?.remaining_time != null && status.remaining_time > 0 && (
                  <>
                    <span className="flex items-center gap-1">
                      <Clock className="w-3 h-3" />
                      {formatDuration(status.remaining_time * 60)}
                    </span>
                    <span className="text-bambu-green font-medium">
                      {t('orders.queue.eta')} {formatETA(status.remaining_time, timeFormat, t)}
                    </span>
                  </>
                )}
                {status.layer_num != null && status.total_layers != null && status.total_layers > 0 && (
                  <span className="flex items-center gap-1">
                    <Layers className="w-3 h-3" />
                    {status.layer_num}/{status.total_layers}
                  </span>
                )}
              </div>
            </>
          ) : (
            <div className="flex items-center gap-2 mt-1">
              <div className="flex-1 bg-bambu-dark-tertiary rounded-full h-2">
                <div className="bg-bambu-green h-2 rounded-full" style={{ width: '0%' }} />
              </div>
              <span className="text-sm text-white font-medium flex-shrink-0">0%</span>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
