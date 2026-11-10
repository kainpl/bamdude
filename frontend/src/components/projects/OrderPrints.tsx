import { useEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Link } from 'react-router';
import { Package } from 'lucide-react';
import { api } from '../../api/client';
import type { Archive, Order, ProjectLine } from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import { canFileArchive } from '../../utils/workshopRights';
import { formatDateTime } from '../../utils/date';
import { getArchiveStatusBadge } from '../../utils/archiveStatus';
import { Button } from '../Button';
import { CardActionMenu, CardActionMenuItem } from '../CardActionMenu';
import { ActionConfirm } from '../workshop/ActionConfirm';
import { LoadingBlock } from '../LoadingBlock';
import { PaginationBar } from '../PaginationBar';
import { Select } from '../Select';
import { RefreshFailedNote } from '../workshop/RefreshFailedNote';
import { WorkshopDialog } from '../workshop/WorkshopDialog';
import { OrderPrintDefectsDialog } from './OrderPrintDefectsDialog';
import { invalidateOrderViews } from '../../utils/queryInvalidation';
import { lineConfigLabel } from './lineConfigLabel';
import { useUiPreferences } from '../../hooks/useUiPreferences';

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

/** A card's date and time: day, month and time, as the other order views write them —
 *  a print of this year does not need its year on every card. */
const PRINT_WHEN: Intl.DateTimeFormatOptions = { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' };
/** …and one of another year does, or a long order's old print reads as this year's
 *  (final review M6). */
const PRINT_WHEN_YEAR: Intl.DateTimeFormatOptions = { ...PRINT_WHEN, year: 'numeric' };

function printWhenOptions(value: string | null | undefined): Intl.DateTimeFormatOptions {
  const at = value ? new Date(value) : null;
  return at && !Number.isNaN(at.getTime()) && at.getFullYear() !== new Date().getFullYear() ? PRINT_WHEN_YEAR : PRINT_WHEN;
}

/** A group's page size until the operator picks another (WS-13 E4 F02). */
const GROUP_PAGE = 24;

/** What one page of `getProjectArchives` asks for.
 *
 *  ⚠️ It is also the endpoint's own ceiling: `routes/projects.py::
 *  list_project_archives` declares `limit: int = Query(default=100, ge=1,
 *  le=500)`, so asking for more is a 422 and not a bigger page. The two numbers
 *  move TOGETHER — raise one without the other and every read of this grid
 *  fails validation, which looks like an empty order rather than a bad request. */
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
  // ⚠️ **Keyed by the order, so a different order is a different component.**
  // Everything below that is per-order state — the `extraPages` cap, each group's
  // page — then starts at zero BEFORE the first render of the new order, not after
  // it. An effect could only reset it afterwards, and by then the render in between
  // had already asked for `['project-archives', the new order, the OLD cap]`.
  return <OrderPrintsOf key={order.id} order={order} canEdit={canEdit} />;
}

/** The title of a line's group — «<product> — × <quantity>», or «— parts» for a parts line. */
function lineTitle(line: ProjectLine, parts: string): string {
  return `${line.product_name} — ${line.mode === 'parts' ? parts : `× ${line.quantity}`}`;
}

function OrderPrintsOf({ order, canEdit }: OrderPrintsProps) {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();

  // Every archive the order NAMES — the walk's own finish line, and the same
  // set the figures above were computed from.
  const named = useMemo(() => {
    const ids = new Set<number>();
    for (const line of order.lines) for (const id of line.archive_ids ?? []) ids.add(id);
    for (const id of order.other_archive_ids ?? []) ids.add(id);
    return ids;
  }, [order]);

  // Pages bought by hand past the guard. In the key, so a click is a fetch —
  // `placeholderData` keeps the prints on screen while it runs.
  //
  // ⚠️ **This is a bigger CAP, not the next page.** Clicking "load older prints"
  // re-walks from offset 0 with `MAX_PAGES + extraPages` allowed; offset paging over
  // `created_at desc` shifts under a farm that is still printing, so resuming from
  // where the last walk stopped would skip whatever moved across the boundary.
  const [extraPages, setExtraPages] = useState(0);

  const { data, isLoading, isError, isFetching, refetch } = useQuery({
    queryKey: ['project-archives', order.id, extraPages],
    queryFn: () => loadOrderArchives(order.id, named, MAX_PAGES + extraPages),
    // ⚠️ Placeholder only from the SAME order: `(prev) => prev` would render the
    // previous order's prints under this order's headings until the fetch lands.
    placeholderData: (prev, prevQuery) => (prevQuery?.queryKey[1] === order.id ? prev : undefined),
  });
  const archives = data?.archives;
  const truncated = data?.truncated ?? false;

  // The printer's name on a card (F04, R03): the list the Archives page reads, archived
  // machines included — asked only with the right to read printers. Without it, while
  // it is read, when it fails or for an id it does not know, the name is simply left
  // out: it never hides a card and never becomes an error of the tab.
  const canSeePrinters = hasPermission('printers:read');
  const { data: printers } = useQuery({
    queryKey: ['printers', 'withArchived'],
    queryFn: api.getPrintersWithArchived,
    enabled: canSeePrinters,
  });
  const printerNames = useMemo(() => new Map((printers ?? []).map((p) => [p.id, p.name])), [printers]);
  const { data: settings } = useUiPreferences();

  // One page per group, by the group's key. Clamped on every render to the pages the
  // group has NOW — a print unlinked, moved or loaded changes that under the page.
  const [paging, setPaging] = useState<Record<string, { page: number; perPage: number }>>({});

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
        title: lineTitle(line, t('orders.prints.partsGroup')),
        archives: items,
      });
    }
  }

  const other = pick(order.other_archive_ids ?? []);
  for (const item of other) claimed.add(item.id);
  if (other.length > 0) {
    groups.push({ key: 'other', testId: 'prints-other', title: t('orders.prints.otherPrints'), archives: other });
  }

  const unlisted = (archives ?? []).filter((archive) => !claimed.has(archive.id));
  if (unlisted.length > 0) {
    groups.push({ key: 'unlisted', testId: 'prints-unlisted', title: t('orders.prints.unlisted'), archives: unlisted });
  }

  const setGroupPage = (key: string, next: { page: number; perPage: number }) =>
    setPaging((prev) => ({ ...prev, [key]: next }));

  // F02: a page past a group's end is clamped — and the clamp is KEPT, or the old page
  // came back by itself once a print was filed back in (final review M5). A group that
  // emptied is not drawn at all, so it is clamped to its first page by its KEY (Codex
  // review V03) — its page size stays, and so do the other groups' pages.
  const drawn = new Set(groups.map((group) => group.key));
  const clamps = [
    ...groups.flatMap((group) => {
      const state = paging[group.key];
      if (!state || state.perPage === -1) return [];
      const last = Math.max(1, Math.ceil(group.archives.length / state.perPage));
      return state.page > last ? [`${group.key}=${last}`] : [];
    }),
    ...(archives === undefined
      ? []
      : Object.entries(paging)
          .filter(([key, state]) => !drawn.has(key) && state.page > 1)
          .map(([key]) => `${key}=1`)),
  ];
  const clampKey = clamps.join('|');
  useEffect(() => {
    if (!clampKey) return;
    setPaging((prev) => {
      const next = { ...prev };
      for (const entry of clampKey.split('|')) {
        const [key, last] = entry.split('=');
        if (next[key]) next[key] = { ...next[key], page: Number(last) };
      }
      return next;
    });
  }, [clampKey]);

  return (
    // No heading of its own — the «Prints» tab names it (WS-13 E3 F05).
    <section className="space-y-3">
      {isError && data && <RefreshFailedNote onRetry={() => void refetch()} />}

      {truncated && (
        <div className="flex flex-wrap items-center gap-3">
          <p className="text-sm text-bambu-gray">{t('orders.prints.partial')}</p>
          <button
            type="button"
            data-testid="prints-load-older"
            onClick={() => setExtraPages((pages) => pages + 1)}
            disabled={isFetching}
            className="rounded-lg border border-bambu-dark-tertiary px-3 py-1.5 text-xs text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary transition-colors disabled:opacity-50"
          >
            {t('orders.prints.loadOlder')}
          </button>
        </div>
      )}

      {isLoading ? (
        <LoadingBlock label={t('common.loading')} className="py-4 text-bambu-gray" />
      ) : isError && !data ? (
        <div className="flex flex-wrap items-center gap-3 py-2 text-sm">
          <p className="text-red-600 dark:text-red-400">{t('orders.prints.loadFailed')}</p>
          <Button size="sm" variant="secondary" onClick={() => void refetch()}>
            {t('orders.prints.retry')}
          </Button>
        </div>
      ) : groups.length === 0 ? (
        <p className="py-6 text-center text-sm text-bambu-gray">{t('orders.prints.empty')}</p>
      ) : (
        groups.map((group) => {
          const state = paging[group.key] ?? { page: 1, perPage: GROUP_PAGE };
          const total = group.archives.length;
          const totalPages = state.perPage === -1 ? 1 : Math.max(1, Math.ceil(total / state.perPage));
          const page = Math.min(Math.max(1, state.page), totalPages);
          const shown =
            state.perPage === -1 ? group.archives : group.archives.slice((page - 1) * state.perPage, page * state.perPage);
          return (
            <div key={group.key} data-testid={group.testId}>
              <h3 className="mt-4 mb-2 text-sm font-semibold text-white">
                {group.title} <small className="ml-1 text-xs font-normal text-bambu-gray">{total}</small>
              </h3>
              <div className="grid gap-2.5 grid-cols-[repeat(auto-fill,minmax(min(320px,100%),1fr))]">
                {shown.map((archive) => (
                  <ArchiveCard
                    key={`${group.key}-${archive.id}`}
                    archive={archive}
                    order={order}
                    lines={order.lines}
                    canEdit={canEdit}
                    printerName={archive.printer_id != null ? printerNames.get(archive.printer_id) : undefined}
                    when={formatDateTime(
                      archive.completed_at || archive.started_at || archive.created_at,
                      settings?.time_format,
                      settings?.date_format,
                      printWhenOptions(archive.completed_at || archive.started_at || archive.created_at),
                    )}
                  />
                ))}
              </div>
              <PaginationBar
                variant="bare"
                allowAll
                partial={truncated}
                page={page}
                totalPages={totalPages}
                perPage={state.perPage}
                total={total}
                items={t('orders.prints.noun', { count: total })}
                onPageChange={(next) => setGroupPage(group.key, { ...state, page: next })}
                onPerPageChange={(perPage) => setGroupPage(group.key, { page: 1, perPage })}
              />
            </div>
          );
        })
      )}
    </section>
  );
}

interface ArchiveCardProps {
  archive: Archive;
  order: Order;
  lines: ProjectLine[];
  canEdit: boolean;
  printerName: string | undefined;
  when: string;
}

/** «File under a line» (WS-13 E4 F08, F10): the order's lines and «no line», the
 *  current one chosen; the order travels with the line, as the server demands. */
function AssignLineDialog({ archive, order, onClose }: { archive: Archive; order: Order; onClose: () => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [lineId, setLineId] = useState<number | null>(archive.project_line_id ?? null);
  const [error, setError] = useState<string | null>(null);

  // The order's own command (WS-13 E13 V01): `add-archives` with the line moves the print
  // between this order's lines (journaled as a re-line) under the Workshop's filing right.
  // One synchronous flag for the send and every way out (WS-13 E13 V02): a second press in
  // the same frame sends nothing, and the dialog does not close under the held request.
  const sent = useRef(false);
  const leave = () => {
    if (sent.current) return;
    onClose();
  };
  const save = useMutation({
    mutationFn: () => api.addArchivesToOrder(order.id, [archive.id], lineId),
    onSuccess: () => {
      invalidateOrderViews(queryClient, { orderId: order.id });
      onClose();
    },
    onError: (e: Error) => {
      sent.current = false;
      setError(e.message);
    },
  });

  // The line's configuration as every other view names it (`lineConfigLabel`), with
  // its quantity — so two lines of one product are told apart (final review M7).
  const label = (line: ProjectLine) => {
    if (line.mode === 'parts') return `${line.product_name} — ${t('orders.prints.partsGroup')}`;
    const config = lineConfigLabel(line.configuration, line.mode, t);
    return `${line.product_name} — ${config ? `${config} · ` : ''}× ${line.quantity}`;
  };

  return (
    <WorkshopDialog
      title={t('orders.prints.assign.title')}
      subtitle={printSubtitle(archive, t)}
      size="md"
      pending={save.isPending}
      error={error ?? undefined}
      onClose={leave}
      footer={
        <>
          <Button variant="secondary" onClick={leave} disabled={save.isPending}>
            {t('orders.prints.assign.cancel')}
          </Button>
          <Button
            onClick={() => {
              if (sent.current) return;
              sent.current = true;
              setError(null);
              save.mutate();
            }}
            disabled={save.isPending || lineId === (archive.project_line_id ?? null)}
          >
            {t('orders.prints.assign.submit')}
          </Button>
        </>
      }
    >
      <div className="flex flex-col gap-1">
        <label htmlFor={`print-${archive.id}-line`} className="text-sm text-bambu-gray-light">
          {t('orders.prints.assign.line')}
        </label>
        <Select
          id={`print-${archive.id}-line`}
          className="w-full"
          value={lineId ?? ''}
          onChange={(e) => setLineId(e.target.value ? Number(e.target.value) : null)}
        >
          <option value="">{t('orders.prints.assign.none')}</option>
          {order.lines.map((line) => (
            <option key={line.id} value={line.id}>
              {label(line)}
            </option>
          ))}
        </Select>
      </div>
    </WorkshopDialog>
  );
}

/** «<file> · plate N» — a dialog's subtitle naming the print. */
function printSubtitle(archive: Archive, t: ReturnType<typeof useTranslation>['t']): string {
  const name = archive.restricted ? t('orders.prints.restrictedPrint', { id: archive.id }) : archive.print_name || archive.filename;
  return (archive.plate_index ?? 0) > 0 ? `${name} · ${t('orders.prints.plate', { n: archive.plate_index })}` : name;
}

/** One print: what it was, where and when it ran, how it ended, and which line it answers to. */
function ArchiveCard({ archive, order, lines, canEdit, printerName, when }: ArchiveCardProps) {
  const { t } = useTranslation();
  const { canModify, hasPermission } = useAuth();
  const queryClient = useQueryClient();
  const [dialog, setDialog] = useState<'defects' | 'assign' | 'remove' | null>(null);

  const badge = getArchiveStatusBadge(archive.status);
  const name = archive.restricted ? t('orders.prints.restrictedPrint', { id: archive.id }) : archive.print_name || archive.filename;
  const lineName = lines.find((line) => line.id === archive.project_line_id)?.product_name;
  const completed = archive.status === 'completed';
  // F(print) (WS-13 E13 O21): the filing right files and unfiles any print; `orders:update`
  // only a print the archive right reaches — the server's own rule, offered only where it
  // would be allowed. The defects are the order's own contract and stay with `canEdit`.
  const canAssign = canFileArchive(hasPermission, canModify, archive.created_by_id);
  // Nothing new is filed under a closed order (409 `order_closed`) — a line change included;
  // a print may still leave it.
  const orderOpen = order.status !== 'completed' && order.status !== 'cancelled';
  const where = [
    (archive.plate_index ?? 0) > 0 ? t('orders.prints.plate', { n: archive.plate_index }) : null,
    printerName ?? null,
    when || null,
  ].filter(Boolean);

  const remove = useMutation({
    mutationFn: () => api.removeArchivesFromProject(order.id, [archive.id]),
    onSuccess: () => {
      invalidateOrderViews(queryClient, { orderId: order.id });
      setDialog(null);
    },
    // A refusal (403, 409 a print its order received) is the confirmation's to show,
    // in its own dialog (WS-13 E13 E02).
  });

  return (
    <div
      data-print-card
      data-status={archive.status}
      className="flex gap-3 items-start rounded-xl border border-bambu-dark-tertiary bg-bambu-dark-secondary p-3 text-[13px]"
    >
      {/* ⚠️ `fileName`, not `search` — ArchivesPage reads `printer`, `file` and
          `fileName` off the URL and nothing else, and only `file` FILTERS: without a
          library file id the link goes to the plain list instead. */}
      {archive.restricted ? (
        <div className="w-10 h-10 rounded-lg bg-bambu-dark-tertiary flex items-center justify-center flex-shrink-0">
          <Package className="w-[18px] h-[18px] text-bambu-gray" aria-hidden />
        </div>
      ) : (
      <Link
        to={
          archive.library_file_id != null
            ? `/archives?file=${archive.library_file_id}&fileName=${encodeURIComponent(archive.filename)}`
            : '/archives'
        }
        className="w-10 h-10 rounded-lg bg-bambu-dark-tertiary flex items-center justify-center overflow-hidden flex-shrink-0"
      >
        {archive.thumbnail_path ? (
          <img src={api.getArchiveThumbnail(archive.id)} alt="" className="w-full h-full object-contain" />
        ) : (
          <Package className="w-[18px] h-[18px] text-bambu-gray" aria-hidden />
        )}
      </Link>
      )}

      <div className="min-w-0 flex-1">
        <p className="font-semibold text-white [overflow-wrap:anywhere]">{name}</p>
        <small className="block mt-0.5 text-xs text-bambu-gray" data-testid={`print-where-${archive.id}`}>
          {where.join(' · ')}
        </small>
        <small className="flex flex-wrap items-center gap-x-1.5 gap-y-1 mt-1 text-xs text-bambu-gray">
          {badge && (
            <span
              className={`px-2 py-0.5 rounded font-medium ${
                archive.status === 'printing'
                  ? 'bg-bambu-green/20 text-bambu-green'
                  : archive.status === 'archived'
                    ? 'bg-gray-200 dark:bg-gray-500/20 text-gray-600 dark:text-gray-400'
                    : 'bg-red-500/20 text-red-600 dark:text-red-400'
              }`}
            >
              {t(badge.labelKey)}
            </span>
          )}
          {completed && (
            <span data-testid={`print-defects-${archive.id}`}>
              {t('orders.prints.pieces', { count: archive.quantity })}
              {(archive.defective_count ?? 0) > 0 && (
                <>
                  {' · '}
                  <span className="text-amber-700 dark:text-amber-400">
                    {t('orders.prints.defective', { count: archive.defective_count })}
                  </span>
                </>
              )}
            </span>
          )}
          <span aria-hidden>·</span>
          <span
            className={`px-2 py-0.5 rounded font-medium ${
              archive.project_line_id != null
                ? 'bg-blue-500/20 text-blue-700 dark:text-blue-400'
                : 'bg-gray-200 dark:bg-gray-500/20 text-gray-600 dark:text-gray-400'
            }`}
            title={lineName}
          >
            {archive.project_line_id != null ? t('orders.prints.explicit') : t('orders.prints.attributed')}
          </span>
        </small>
      </div>

      {((canEdit && completed) || canAssign) && (
        <div className="flex-shrink-0">
          <CardActionMenu
            label={t('orders.prints.actions')}
            testId={`print-menu-${archive.id}`}
            width="max-content"
          >
            {(close) => (
              <>
                {canEdit && completed && (
                  <CardActionMenuItem
                    onSelect={() => {
                      close();
                      setDialog('defects');
                    }}
                  >
                    {t('orders.prints.defects.action')}
                  </CardActionMenuItem>
                )}
                {canAssign && orderOpen && (
                  <CardActionMenuItem
                    onSelect={() => {
                      close();
                      setDialog('assign');
                    }}
                  >
                    {t('orders.prints.fileUnderLine')}
                  </CardActionMenuItem>
                )}
                {canAssign && (
                  <>
                    <div role="separator" className="my-1 border-t border-bambu-dark-tertiary" />
                    <CardActionMenuItem
                      danger
                      onSelect={() => {
                        close();
                        setDialog('remove');
                      }}
                    >
                      {t('orders.prints.removeFromOrder')}
                    </CardActionMenuItem>
                  </>
                )}
              </>
            )}
          </CardActionMenu>
        </div>
      )}

      {dialog === 'defects' && (
        <OrderPrintDefectsDialog orderId={order.id} archive={archive} onClose={() => setDialog(null)} />
      )}
      {dialog === 'assign' && <AssignLineDialog archive={archive} order={order} onClose={() => setDialog(null)} />}
      {dialog === 'remove' && (
        // ⚠️ One click, one request: ActionConfirm holds the second click while the first
        // unlink is in flight, so the same print is never unfiled twice.
        <ActionConfirm
          title={t('orders.prints.remove.title', { name })}
          body={<p className="text-sm text-bambu-gray">{t('orders.prints.remove.message')}</p>}
          primaryLabel={t('orders.prints.remove.confirm')}
          danger
          send={() => remove.mutateAsync()}
          onClose={() => setDialog(null)}
        />
      )}
    </div>
  );
}
