import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router-dom';
import { MoreVertical, Package, Unlink } from 'lucide-react';
import { api } from '../../api/client';
import type { Archive, Order, ProjectLine } from '../../api/client';
import { useToast } from '../../contexts/ToastContext';
import { formatDateOnly } from '../../utils/date';
import { getArchiveStatusBadge } from '../../utils/archiveStatus';
import { LoadingBlock } from '../LoadingBlock';
import { OrderLinePicker } from '../pickers/OrderLinePicker';
import { invalidateOrderViews } from '../../utils/queryInvalidation';

interface OrderPrintsProps {
  order: Order;
  canEdit: boolean;
}

interface Group {
  key: string;
  testId: string;
  title: string;
  archives: Archive[];
}

/** What one page of `getProjectArchives` asks for. The endpoint declares no
 *  maximum, so this is the page size, not a ceiling somebody else set. */
const ARCHIVE_PAGE = 500;

/** How many pages one read will walk before it stops and asks the operator.
 *  Ten thousand prints under one order is a reporting problem; the walk must
 *  not become an unbounded loop over somebody's whole archive because a single
 *  id cannot be found. */
const MAX_PAGES = 20;

interface LoadedArchives {
  archives: Archive[];
  /** The walk hit `MAX_PAGES` with ids the order names still unloaded. */
  truncated: boolean;
}

/**
 * Read pages until the order's own list is satisfied.
 *
 * ⚠️ **A full page is not the end of the history.** `limit` rows back means
 * `limit` was the LIMIT, so the walk asks again; a SHORT page is the only
 * proof there is nothing older. The other stop is the order's own accounting:
 * once every id it names is in hand, older prints belong to nobody here.
 *
 * ⚠️ **Pages overlap.** Offset paging over `created_at desc` shifts under a
 * farm that is still printing, so the same archive can arrive twice — hence
 * the id-keyed map rather than a concatenation. Insertion order is the
 * server's order, which is what the groups render in.
 *
 * ⚠️ **An EMPTY `named` is not "nothing to fetch".** A server older than the
 * per-line archive ids names nothing at all, and stopping on that would show
 * one page of a long history with no sign of the rest; the short page is then
 * the only stop the walk has.
 *
 * ⚠️ **A short page while some named ids are still missing is `truncated:
 * false`.** There is nothing older to read, so the history IS complete and the
 * button that offers to read further cannot help: the ids left over name
 * archives that were deleted (or re-filed elsewhere), and pinning a "load
 * older prints" button to them would offer the operator a click that walks the
 * whole archive and comes back with exactly what is already on screen.
 */
async function loadOrderArchives(orderId: number, named: Set<number>, maxPages: number): Promise<LoadedArchives> {
  const byId = new Map<number, Archive>();
  for (let page = 0; page < maxPages; page++) {
    const batch = await api.getProjectArchives(orderId, ARCHIVE_PAGE, page * ARCHIVE_PAGE);
    for (const archive of batch) byId.set(archive.id, archive);
    const satisfied = named.size > 0 && [...named].every((id) => byId.has(id));
    if (batch.length < ARCHIVE_PAGE || satisfied) return { archives: [...byId.values()], truncated: false };
  }
  return { archives: [...byId.values()], truncated: true };
}

/**
 * Every print that counts towards this order, grouped the way the SERVER
 * grouped it.
 *
 * ⚠️ **`lines[].archive_ids` is not a partition.** A plate that carries parts
 * of two products counts against both lines, so the same archive is expected
 * under two headings — walking the archives and asking each one "which line?"
 * would file it under one of them and quietly under-count the other. The
 * grouping is therefore read out of the order response, never rebuilt from
 * `archive.project_line_id`; that field only decides which BADGE the card
 * wears (filed by hand vs attributed by the server's own accounting).
 *
 * "Unlisted" is a net, not a feature: an archive the response bound to the
 * order but named in no group would otherwise vanish from a page whose whole
 * job is to account for it.
 */
export function OrderPrints({ order, canEdit }: OrderPrintsProps) {
  const { t } = useTranslation();

  // Every archive the order NAMES — the walk's own finish line, and the same
  // set the figures above were computed from. `pick()` drops an id it cannot
  // resolve in silence, so a page that stopped short showed fewer prints than
  // the order claimed and looked wrong rather than incomplete.
  const named = useMemo(() => {
    const ids = new Set<number>();
    for (const line of order.lines) for (const id of line.archive_ids ?? []) ids.add(id);
    for (const id of order.other_archive_ids ?? []) ids.add(id);
    return ids;
  }, [order]);

  // Pages bought by hand past the guard. In the key, so a click is a fetch —
  // `placeholderData` keeps the prints on screen while it runs, rather than
  // dropping the grid back to its spinner.
  //
  // ⚠️ **This is a bigger CAP, not the next page.** Clicking "load older
  // prints" re-walks from offset 0 with `MAX_PAGES + extraPages` allowed;
  // nothing is paged incrementally. Offset paging over `created_at desc`
  // shifts under a farm that is still printing, so resuming from where the
  // last walk stopped would skip whatever moved across the boundary — and the
  // id-keyed map makes re-reading the pages already in hand cost nothing but
  // the requests.
  const [extraPages, setExtraPages] = useState(0);

  const { data, isLoading, isFetching } = useQuery({
    queryKey: ['project-archives', order.id, extraPages],
    queryFn: () => loadOrderArchives(order.id, named, MAX_PAGES + extraPages),
    placeholderData: (prev) => prev,
  });
  const archives = data?.archives;
  const truncated = data?.truncated ?? false;

  const byId = new Map((archives ?? []).map((archive) => [archive.id, archive]));
  const pick = (ids: number[]): Archive[] =>
    ids.map((id) => byId.get(id)).filter((archive): archive is Archive => archive != null);

  const groups: Group[] = [];
  const claimed = new Set<number>();

  for (const line of order.lines) {
    // ``?? []``: a backend older than pass-2 Task 1 (or a cached response) has no archive_ids yet.
    const items = pick(line.archive_ids ?? []);
    for (const item of items) claimed.add(item.id);
    if (items.length > 0) {
      groups.push({
        key: `line-${line.id}`,
        testId: `prints-line-${line.id}`,
        title: `${line.product_name} × ${line.quantity}`,
        archives: items,
      });
    }
  }

  const other = pick(order.other_archive_ids ?? []);
  for (const item of other) claimed.add(item.id);
  if (other.length > 0) {
    groups.push({
      key: 'other',
      testId: 'prints-other',
      title: t('orders.prints.otherPrints'),
      archives: other,
    });
  }

  const unlisted = (archives ?? []).filter((archive) => !claimed.has(archive.id));
  if (unlisted.length > 0) {
    groups.push({
      key: 'unlisted',
      testId: 'prints-unlisted',
      title: t('orders.prints.unlisted'),
      archives: unlisted,
    });
  }

  return (
    <section className="space-y-3">
      <h2 className="text-lg font-semibold text-white flex items-center gap-2">
        <Package className="w-5 h-5" />
        {t('orders.prints.title')}
      </h2>

      {truncated && (
        <button
          type="button"
          data-testid="prints-load-older"
          onClick={() => setExtraPages((pages) => pages + 1)}
          disabled={isFetching}
          className="rounded-lg border border-bambu-dark-tertiary px-3 py-1.5 text-xs text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary transition-colors disabled:opacity-50"
        >
          {t('orders.prints.loadOlder')}
        </button>
      )}

      {isLoading ? (
        <LoadingBlock label={t('common.loading')} className="py-6 text-bambu-gray" />
      ) : groups.length === 0 ? (
        <p className="text-sm text-bambu-gray/70 italic">{t('orders.prints.empty')}</p>
      ) : (
        groups.map((group) => (
          <div key={group.key} data-testid={group.testId} className="space-y-2">
            <h3 className="text-sm text-bambu-gray">{group.title}</h3>
            <div className="grid gap-2 grid-cols-[repeat(auto-fill,minmax(240px,1fr))]">
              {group.archives.map((archive) => (
                <ArchiveCard
                  key={`${group.key}-${archive.id}`}
                  archive={archive}
                  order={order}
                  lines={order.lines}
                  canEdit={canEdit}
                />
              ))}
            </div>
          </div>
        ))
      )}
    </section>
  );
}

interface ArchiveCardProps {
  archive: Archive;
  order: Order;
  lines: ProjectLine[];
  canEdit: boolean;
}

/** One print: what it was, how it ended, and which line it answers to. */
function ArchiveCard({ archive, order, lines, canEdit }: ArchiveCardProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const [menuOpen, setMenuOpen] = useState(false);
  const [pickingLine, setPickingLine] = useState(false);

  // Filing a print under a line, or taking it off the order, moves the order
  // cards' roll-up and the customer tiles as well as this page — and the
  // print may have just left ANOTHER order and another customer, which is why
  // every key is a prefix. One decision, in `utils/queryInvalidation.ts`.
  const refresh = () => invalidateOrderViews(queryClient, { orderId: order.id });

  // ⚠️ `project_id` travels with the line: the server rejects (400) a line
  // that belongs to another order, and a bare line change on an archive whose
  // order is being re-stated is exactly that case.
  const fileUnder = useMutation({
    mutationFn: (lineId: number | null) =>
      api.updateArchive(archive.id, { project_id: order.id, project_line_id: lineId }),
    onSuccess: () => {
      refresh();
      setPickingLine(false);
      setMenuOpen(false);
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const remove = useMutation({
    mutationFn: () => api.removeArchivesFromProject(order.id, [archive.id]),
    onSuccess: () => {
      refresh();
      setMenuOpen(false);
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const badge = getArchiveStatusBadge(archive.status);
  const name = archive.print_name || archive.filename;
  const when = archive.completed_at || archive.started_at || archive.created_at;
  const lineName = lines.find((line) => line.id === archive.project_line_id)?.product_name;

  return (
    <div className="relative rounded-lg bg-bambu-dark-secondary border border-bambu-dark-tertiary p-2 flex gap-2">
      {/* ⚠️ `fileName`, not `search` — ArchivesPage reads `printer`, `file` and
          `fileName` off the URL and nothing else, so the `?search=` this was
          copied with never reached the page at all. And only `file` FILTERS:
          `fileName` merely LABELS the chip. Carrying it alone therefore opened
          an unfiltered archive list wearing this print's name, which reads as
          a filter that silently failed — without a library file id the link
          goes to the plain list instead. */}
      <Link
        to={
          archive.library_file_id != null
            ? `/archives?file=${archive.library_file_id}&fileName=${encodeURIComponent(archive.filename)}`
            : '/archives'
        }
        className="w-14 h-14 rounded bg-bambu-dark flex items-center justify-center overflow-hidden flex-shrink-0"
      >
        {archive.thumbnail_path ? (
          <img src={api.getArchiveThumbnail(archive.id)} alt="" className="w-full h-full object-contain" />
        ) : (
          <Package className="w-5 h-5 text-bambu-gray" />
        )}
      </Link>

      <div className="min-w-0 flex-1">
        <p className="text-sm text-white truncate" title={name}>
          {name}
        </p>
        <div className="flex items-center gap-1.5 flex-wrap mt-0.5">
          {badge && (
            <span className={`px-1.5 py-0.5 rounded text-[10px] ${badge.className}`}>{t(badge.labelKey)}</span>
          )}
          <span
            className={`px-1.5 py-0.5 rounded text-[10px] ${
              archive.project_line_id != null
                ? 'bg-bambu-green/20 text-bambu-green'
                : 'bg-bambu-dark-tertiary text-bambu-gray'
            }`}
            title={lineName}
          >
            {archive.project_line_id != null ? t('orders.prints.explicit') : t('orders.prints.attributed')}
          </span>
        </div>
        {when && <p className="text-[11px] text-bambu-gray/70 mt-0.5">{formatDateOnly(when)}</p>}
      </div>

      {canEdit && (
        <div className="relative flex-shrink-0">
          <button
            type="button"
            onClick={() => setMenuOpen((open) => !open)}
            aria-label={t('orders.prints.actions')}
            className="p-1 rounded text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary transition-colors"
          >
            <MoreVertical className="w-4 h-4" />
          </button>

          {menuOpen && (
            <>
              <div
                className="fixed inset-0 z-10"
                onClick={() => {
                  setMenuOpen(false);
                  setPickingLine(false);
                }}
              />
              <div className="absolute right-0 top-7 z-20 w-56 rounded-lg bg-bambu-dark-secondary border border-bambu-dark-tertiary shadow-lg p-1">
                {pickingLine ? (
                  <div className="p-2 space-y-2">
                    <OrderLinePicker
                      orderId={order.id}
                      value={archive.project_line_id}
                      onChange={(lineId) => fileUnder.mutate(lineId)}
                      disabled={fileUnder.isPending}
                    />
                  </div>
                ) : (
                  <>
                    <button
                      type="button"
                      onClick={() => setPickingLine(true)}
                      className="w-full text-left px-3 py-2 text-sm text-white rounded hover:bg-bambu-dark-tertiary"
                    >
                      {t('orders.prints.fileUnderLine')}
                    </button>
                    <button
                      type="button"
                      onClick={() => remove.mutate()}
                      disabled={remove.isPending}
                      className="w-full flex items-center gap-2 text-left px-3 py-2 text-sm text-status-error rounded hover:bg-bambu-dark-tertiary disabled:opacity-50"
                    >
                      <Unlink className="w-4 h-4" />
                      {t('orders.prints.removeFromOrder')}
                    </button>
                  </>
                )}
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
