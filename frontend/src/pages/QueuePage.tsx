import { useEffect, useState, useMemo } from 'react';
import { useSearchParams } from 'react-router';
import { useTranslation } from 'react-i18next';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Calendar, LayoutGrid } from 'lucide-react';
import { api } from '../api/client';
import { usePendingQueueItems, usePrintingQueueItems } from '../hooks/useQueueItems';
import { compareLocationNames } from '../utils/locationOrder';
import { readStoredQueueSort, sortQueues, type QueueSortOption } from '../utils/queueOrder';
import { forecastById, type EtaStatus } from '../utils/etaSort';
import { buildLocationIndex, readStoredLocationFilter } from '../utils/locationTree';
import { groupByLocation } from '../utils/locationGroups';
import { groupByTag } from '../utils/tagGroups';
import type { PrinterQueue, PrintQueueItem } from '../api/client';
import { QueueCard } from '../components/QueueCard';
import { LoadingBlock } from '../components/LoadingBlock';
import { QueueStatsBar } from '../components/Queue/QueueStatsBar';
import { StaggerBanner } from '../components/Queue/StaggerBanner';
import { QueueTimelineView } from '../components/Queue/QueueTimelineView';
import { AutoQueuePanel } from '../components/Queue/AutoQueuePanel';
import { QueueToolbar } from '../components/Queue/QueueToolbar';
import { readStoredCardSize } from '../utils/cardSize';
import { PrintModal } from '../components/PrintModal';
import { OpenMonitorButton } from '../features/monitor/OpenMonitorButton';
import { useMonitorTarget } from '../features/monitor/useMonitorTarget';
import { Button } from '../components/Button';
import { WindowVirtualGrid } from '../components/WindowVirtualGrid';
import { useMountedPrinterPriority } from '../hooks/useMountedPrinterPriority';
import { useProgressiveListLength } from '../hooks/useProgressiveListLength';

type ViewMode = 'expanded' | 'all' | 'timeline';

// 1 = S … 4 = XL. Columns only — every card is the same card at every size.
const QUEUE_GRID_CLASSES: Record<number, string> = {
  1: 'grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4',
  2: 'grid-cols-1 md:grid-cols-2 xl:grid-cols-3',
  3: 'grid-cols-1 lg:grid-cols-2',
  4: 'grid-cols-1',
};

const QUEUE_ROW_HEIGHT_ESTIMATE = 480;

function queueGridColumnCount(cardSize: number, viewportWidth: number): number {
  switch (cardSize) {
    case 1:
      return viewportWidth >= 1280 ? 4 : viewportWidth >= 1024 ? 3 : viewportWidth >= 640 ? 2 : 1;
    case 2:
      return viewportWidth >= 1024 ? 3 : viewportWidth >= 640 ? 2 : 1;
    case 3:
      return viewportWidth >= 1024 ? 2 : 1;
    default:
      return 1;
  }
}

const VALID_VIEW_MODES: ViewMode[] = ['expanded', 'all', 'timeline'];

export function QueuePage() {
  useMountedPrinterPriority('queues');
  const { t } = useTranslation();
  const [searchParams, setSearchParams] = useSearchParams();
  const [monitorTarget, clearMonitorTarget] = useMonitorTarget();
  const queryClient = useQueryClient();

  const [viewMode, setViewMode] = useState<ViewMode>(() => {
    const fromUrl = searchParams.get('view');
    if (fromUrl === 'compact') return 'expanded';
    if (fromUrl && VALID_VIEW_MODES.includes(fromUrl as ViewMode)) return fromUrl as ViewMode;
    const saved = localStorage.getItem('queueViewMode');
    if (saved === 'compact') return 'expanded';
    if (saved && VALID_VIEW_MODES.includes(saved as ViewMode)) return saved as ViewMode;
    return 'expanded';
  });

  const [editingItem, setEditingItem] = useState<PrintQueueItem | null>(null);
  const effectiveView = monitorTarget ? 'expanded' : viewMode;

  // Card size — the same S · M · L · XL scale as the Printers page. For now it
  // only decides how many queue cards share a row; the card itself does not
  // change yet (2026-09-09).
  const [cardSize, setCardSize] = useState<number>(() => readStoredCardSize('queueCardSize'));
  const [viewportWidth, setViewportWidth] = useState(() => window.innerWidth);
  const handleCardSizeChange = (size: number) => {
    setCardSize(size);
    localStorage.setItem('queueCardSize', String(size));
  };

  useEffect(() => {
    const onResize = () => setViewportWidth(window.innerWidth);
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);

  // Read once, from the one place that knows how these two keys are spelled —
  // the same helper the copy-queue dialog reads, so both are in step.
  const [storedSort] = useState(readStoredQueueSort);
  const [sortBy, setSortBy] = useState<QueueSortOption>(storedSort.sortBy);
  const [sortAsc, setSortAsc] = useState<boolean>(storedSort.sortAsc);

  const [search, setSearch] = useState<string>(() => localStorage.getItem('queueSearch') || '');
  const [statusFilter, setStatusFilter] = useState<string>(() => localStorage.getItem('queueStatusFilter') || 'all');
  const [locationFilter, setLocationFilter] = useState<string>(() => readStoredLocationFilter(localStorage.getItem('queueLocationFilter')));
  const [hideOffline, setHideOffline] = useState<boolean>(() => localStorage.getItem('queueHideOffline') === 'true');

  // Bumped on every printerStatus cache update so the offline filter recomputes
  // when WebSocket / poll-driven status data lands. Same pattern as PrintersPage.
  const [statusCacheVersion, setStatusCacheVersion] = useState(0);

  useEffect(() => { localStorage.setItem('queueSearch', search); }, [search]);
  useEffect(() => { localStorage.setItem('queueStatusFilter', statusFilter); }, [statusFilter]);
  useEffect(() => { localStorage.setItem('queueLocationFilter', locationFilter); }, [locationFilter]);

  const toggleHideOffline = () => {
    setHideOffline(prev => {
      const next = !prev;
      localStorage.setItem('queueHideOffline', String(next));
      return next;
    });
  };

  useEffect(() => {
    const cache = queryClient.getQueryCache();
    const unsubscribe = cache.subscribe((event) => {
      if (
        event.type === 'updated' &&
        Array.isArray(event.query.queryKey) &&
        event.query.queryKey[0] === 'printerStatus'
      ) {
        setStatusCacheVersion(v => v + 1);
      }
    });
    return unsubscribe;
  }, [queryClient]);

  // Fetch all printer queues
  const { data: queues, isLoading } = useQuery({
    queryKey: ['queues'],
    queryFn: api.getQueues,
    refetchInterval: 15000,
  });

  // Queue cards use the same printerStatus key as the Printers page. Start
  // the shared batch-100 read for the full fleet before virtual rows mount,
  // rather than leaving each newly scrolled card to start its own request.
  useEffect(() => {
    for (const queue of queues ?? []) {
      void queryClient.prefetchQuery({
        queryKey: ['printerStatus', queue.printer_id],
        queryFn: () => api.getPrinterStatus(queue.printer_id),
      });
    }
  }, [queues, queryClient]);

  // The server's per-printer «free at» — what the queue-aware sort orders by.
  // The stats bar holds the same query, so this is a second observer of one
  // fetch, not a second fetch.
  const { data: forecast } = useQuery({ queryKey: ['queue-forecast'], queryFn: api.getQueueForecast, refetchInterval: 30_000 });
  const forecastRows = useMemo(() => forecastById(forecast), [forecast]);

  // Fetch all pending items - used by stats bar + "All" view + Timeline.
  // Shared with the order page's queue panel through `useQueueItems`.
  const { data: allPendingItems } = usePendingQueueItems();

  // Auto-queue work still waiting to be routed. Shares its key with the nav
  // badge so TanStack serves both from one request.
  const { data: unassignedAutoItems } = useQuery({
    queryKey: ['auto-queue', 'pending'],
    queryFn: () => api.getAutoQueue('pending'),
    refetchInterval: 30000,
  });

  // Fetch all printing items (real + virtual external/direct) so Timeline
  // can lay out the "now" slot even for prints initiated outside BamDude.
  const { data: allPrintingItems } = usePrintingQueueItems();

  // Combined list for Timeline — pending + printing.  Printing items (real
  // + virtual) anchor each lane's "currently running" slot.
  const allTimelineItems = useMemo(
    () => [...(allPrintingItems ?? []), ...(allPendingItems ?? [])],
    [allPrintingItems, allPendingItems],
  );

  // Sync URL query param with viewMode so it survives reload + can be shared.
  useEffect(() => {
    const current = searchParams.get('view');
    if (current !== viewMode) {
      const next = new URLSearchParams(searchParams);
      next.set('view', viewMode);
      setSearchParams(next, { replace: true });
    }
  }, [viewMode, searchParams, setSearchParams]);

  const handleViewChange = (mode: ViewMode) => {
    setViewMode(mode);
    localStorage.setItem('queueViewMode', mode);
  };

  const handleSortChange = (sort: QueueSortOption) => {
    setSortBy(sort);
    localStorage.setItem('queueSortBy', sort);
  };

  const toggleSortDirection = () => {
    setSortAsc(prev => {
      const next = !prev;
      localStorage.setItem('queueSortAsc', String(next));
      return next;
    });
  };

  // Grid classes for the cards view, by card size — the Printers page's table
  // (its S row included), so the two fleet pages breathe the same way.
  const gridClasses = QUEUE_GRID_CLASSES[cardSize] ?? QUEUE_GRID_CLASSES[2];
  const gridColumns = queueGridColumnCount(cardSize, viewportWidth);

  // The locations themselves, not the distinct values on screen: a parent with
  // no queues directly on it has to be selectable, and a name stopped being an
  // identity the moment "Shelf 1" could exist under two workshops.
  const { data: locationRows } = useQuery({ queryKey: ['printer-locations'], queryFn: api.getPrinterLocations });
  const locationIndex = useMemo(() => buildLocationIndex(locationRows?.locations ?? []), [locationRows]);
  const availableLocations = useMemo(
    () =>
      [...(locationRows?.locations ?? [])]
        .sort((a, b) => compareLocationNames(a.path, b.path))
        .map((row) => ({ id: row.id, label: row.name, depth: row.depth, path: row.path })),
    [locationRows],
  );

  // Filter + sort queues
  const sortedQueues = useMemo(() => {
    if (!queues) return [];
    const term = search.trim().toLowerCase();
    const filtered = queues.filter(q => {
      if (monitorTarget) return q.printer_id === monitorTarget;
      if (statusFilter !== 'all' && q.status !== statusFilter) return false;
      // By id and by subtree: picking a workshop keeps everything beneath it,
      // and a name is no longer an identity now that it can exist twice.
      if (locationFilter !== 'all') {
        const wanted = locationIndex.descendantsOf(Number(locationFilter));
        if (!q.printer_location || !wanted.has(q.printer_location.id)) return false;
      }
      if (term) {
        const name = (q.printer_name || '').toLowerCase();
        const model = (q.printer_model || '').toLowerCase();
        const loc = (q.printer_location?.name || '').toLowerCase();
        if (!name.includes(term) && !model.includes(term) && !loc.includes(term)) return false;
      }
      if (hideOffline) {
        const status = queryClient.getQueryData<{ connected: boolean }>(['printerStatus', q.printer_id]);
        if (!status?.connected) return false;
      }
      return true;
    });

    return sortQueues(filtered, sortBy, sortAsc, {
      statusOf: (printerId) => queryClient.getQueryData<EtaStatus>(['printerStatus', printerId]),
      forecastOf: (printerId) => forecastRows.get(printerId),
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps -- statusCacheVersion is intentional: it forces recompute when WS / poll updates printer status cache; queryClient is stable
  }, [queues, monitorTarget, search, statusFilter, locationFilter, locationIndex, hideOffline, sortBy, sortAsc, statusCacheVersion, forecastRows]);

  const hasActiveFilters = search.trim() !== '' || statusFilter !== 'all' || locationFilter !== 'all';

  // Statuses have already been preloaded for the full farm above. The heavy
  // QueueCard DOM can therefore take the same 12 → +8 route as PrinterCard
  // without splitting its initial HTTP status batch.
  const progressivelyMountedQueueCount = useProgressiveListLength(sortedQueues.length);
  const progressivelyMountedQueueIds = useMemo(
    () => new Set(sortedQueues.slice(0, progressivelyMountedQueueCount).map(queue => queue.id)),
    [progressivelyMountedQueueCount, sortedQueues],
  );
  const mountedQueues = useMemo(
    () => sortedQueues.slice(0, progressivelyMountedQueueCount),
    [progressivelyMountedQueueCount, sortedQueues],
  );

  // Group queues by location or by tag, the printers page's two grouped orders.
  // An array, not an object keyed by id: integer-like object keys iterate in
  // ascending numeric order and would throw away the name sort applied above.
  // Both shapes are normalised to one so the renderer below stays a single
  // block; a tag group carries the tag's colour for its dot.
  const groupedQueues = useMemo(() => {
    if (sortBy === 'location') {
      return groupByLocation(sortedQueues, q => q.printer_location, t('queueCard.ungrouped')).map((g) => ({
        key: `location:${g.locationId ?? 'none'}`,
        label: g.label,
        color: null as string | null,
        items: g.items,
      }));
    }
    if (sortBy === 'tag') {
      const groups = groupByTag(sortedQueues, (q) => q.printer_tags, t('printers.noTag')).map((g) => ({
        key: `tag:${g.tagId ?? 'none'}`,
        label: g.label,
        color: g.color,
        items: g.items,
      }));
      // `groupByTag` always orders its groups by name with «No tag» last, so
      // the direction toggle is applied here — the location branch gets it for
      // free from the item order `sortedQueues` has already reversed.
      if (!sortAsc) groups.reverse();
      return groups;
    }
    return null;
  }, [sortBy, sortAsc, sortedQueues, t]);

  const renderGrid = (items: PrinterQueue[], forceVirtualized = false, debugId = 'queues') => (
    <WindowVirtualGrid
      items={items}
      columns={gridColumns}
      estimateRowHeight={QUEUE_ROW_HEIGHT_ESTIMATE}
      rowGap={16}
      className={`grid gap-4 items-start ${gridClasses}`}
      rowClassName={`grid items-start gap-x-4 ${gridClasses}`}
      getItemKey={(queue) => queue.id}
      renderItem={(queue, virtualized) => <QueueCard key={queue.id} queue={queue} onEditItem={setEditingItem} virtualized={virtualized} />}
      forceVirtualized={forceVirtualized}
      baseOverscan={cardSize === 1 ? 1 : 2}
      testId={debugId === 'queues' ? 'queue-card-grid' : `queue-card-grid-${debugId}`}
      debugId={debugId}
    />
  );

  return (
    <div className="p-4">
      {monitorTarget && <Button size="sm" variant="outline" onClick={clearMonitorTarget}>{t('monitor.clearTarget', { id: monitorTarget })}</Button>}
      {/* Header: title + inline toolbar (search / filters / view modes) */}
      <div className="space-y-3 mb-4">
        <h1 className="text-2xl font-bold text-white flex items-center gap-3">
          <Calendar className="w-6 h-6 text-bambu-green" />
          {t('queue.title')}
          <span className="ml-auto"><OpenMonitorButton view="queues" sort={sortBy} /></span>
        </h1>

        {!isLoading && queues && queues.length > 0 && (
          <QueueToolbar
            search={search}
            onSearchChange={setSearch}
            statusFilter={statusFilter}
            onStatusFilterChange={setStatusFilter}
            locationFilter={locationFilter}
            onLocationFilterChange={setLocationFilter}
            availableLocations={availableLocations}
            sortBy={sortBy}
            onSortByChange={handleSortChange}
            sortAsc={sortAsc}
            onSortDirectionToggle={toggleSortDirection}
            viewMode={effectiveView}
            onViewModeChange={handleViewChange}
            cardSize={cardSize}
            onCardSizeChange={handleCardSizeChange}
            hideOffline={hideOffline}
            onHideOfflineToggle={toggleHideOffline}
          />
        )}
      </div>

      {/* Stats bar */}
      {!isLoading && queues && queues.length > 0 && (
        <QueueStatsBar
          queues={queues}
          unassignedCount={unassignedAutoItems?.length ?? 0}
        />
      )}

      {/* Auto-queue router items (sits above per-printer queues). Hidden when empty. */}
      <AutoQueuePanel />

      {/* Electrical-load diagnostic banner (stagger). Hidden when stagger is disabled. */}
      <StaggerBanner />

      {/* Loading */}
      {isLoading && <LoadingBlock label={t('common.loading')} className="py-20 text-bambu-gray" />}

      {/* Empty state */}
      {!isLoading && (!queues || queues.length === 0) && (
        <div className="text-center py-20">
          <LayoutGrid className="w-12 h-12 text-bambu-gray mx-auto mb-3 opacity-50" />
          <p className="text-bambu-gray">{t('queueCard.noQueues')}</p>
          <p className="text-sm text-bambu-gray mt-1">{t('queueCard.noQueuesHint')}</p>
        </div>
      )}

      {/* No search/filter results (S / M / Timeline) */}
      {!isLoading && queues && queues.length > 0 && effectiveView !== 'all' && sortedQueues.length === 0 && (hasActiveFilters || monitorTarget) && (
        <div className="text-center py-12 text-bambu-gray">
          {t('printers.noSearchResults')}
        </div>
      )}

      {/* Card grid (S and M modes) */}
      {!isLoading && queues && queues.length > 0 && effectiveView !== 'all' && effectiveView !== 'timeline' && sortedQueues.length > 0 && (
        groupedQueues ? (
          // Grouped by location or by tag
          <div className="space-y-4">
            {groupedQueues.map((group) => (
              <div key={group.key}>
                <h2
                  className="text-lg font-semibold text-white mb-3 flex items-center gap-2 flex-wrap"
                  title={sortBy === 'tag' ? t('printers.tagGroupHint') : undefined}
                >
                  <span className="w-2 h-2 rounded-full bg-bambu-green" style={group.color ? { backgroundColor: group.color } : undefined} />
                  {group.label}
                  <span className="text-sm font-normal text-bambu-gray">({group.items.length})</span>
                </h2>
                {renderGrid(group.items.filter(queue => progressivelyMountedQueueIds.has(queue.id)), progressivelyMountedQueueCount > 18, `queues-group-${group.key}`)}
              </div>
            ))}
          </div>
        ) : (
          renderGrid(mountedQueues)
        )
      )}

      {/* Timeline view */}
      {!isLoading && queues && queues.length > 0 && effectiveView === 'timeline' && sortedQueues.length > 0 && (
        <QueueTimelineView
          queues={sortedQueues}
          items={allTimelineItems}
          onEditItem={setEditingItem}
        />
      )}

      {/* All view - flat list: active prints first (real + virtual
          external/direct), then pending items numbered #1, #2, … */}
      {!isLoading && effectiveView === 'all' && (
        <div className="space-y-2">
          {((allPrintingItems?.length ?? 0) === 0 && (allPendingItems?.length ?? 0) === 0) ? (
            <div className="text-center py-12">
              <p className="text-bambu-gray">{t('queueCard.noPending')}</p>
            </div>
          ) : (
            <>
              {(allPrintingItems ?? []).map((item) => {
                const queueInfo = queues?.find(q => q.id === item.queue_id);
                return (
                  <div
                    key={`printing-${item.id}`}
                    className="flex items-center gap-3 p-3 bg-blue-500/5 rounded-lg border border-blue-400/30"
                  >
                    <span className="w-6 shrink-0 flex items-center justify-center">
                      <span className="w-2 h-2 rounded-full bg-blue-400 animate-pulse" />
                    </span>
                    <div className="min-w-0 flex-1">
                      <p className="text-sm text-white truncate">
                        {item.archive_name || item.library_file_name || `#${item.id}`}
                      </p>
                      <p className="text-xs text-bambu-gray truncate">
                        {queueInfo?.printer_name || `Queue ${item.queue_id}`}
                        {item.source && item.source !== 'bamdude_queue' && (
                          <span className="ml-2 text-amber-700 dark:text-amber-400">
                            · {t(`queue.source.${item.source}`)}
                          </span>
                        )}
                      </p>
                    </div>
                    <span className="text-xs px-1.5 py-0.5 bg-blue-100 dark:bg-blue-400/20 text-blue-700 dark:text-blue-400 rounded shrink-0">
                      {t('queueCard.status.printing')}
                    </span>
                  </div>
                );
              })}
              {(allPendingItems ?? []).map((item, idx) => {
                const queueInfo = queues?.find(q => q.id === item.queue_id);
                return (
                  <div
                    key={item.id}
                    className="flex items-center gap-3 p-3 bg-bambu-dark rounded-lg border border-bambu-dark-tertiary"
                  >
                    <span className="text-xs text-bambu-gray w-6 text-center shrink-0">#{idx + 1}</span>
                    <div className="min-w-0 flex-1">
                      <p className="text-sm text-white truncate">
                        {item.archive_name || item.library_file_name || `#${item.id}`}
                      </p>
                      <p className="text-xs text-bambu-gray truncate">
                        {queueInfo?.printer_name || `Queue ${item.queue_id}`}
                        {item.waiting_reason && (
                          <span className="ml-2 text-yellow-700 dark:text-yellow-400">· {item.waiting_reason}</span>
                        )}
                      </p>
                    </div>
                    {item.manual_start && (
                      <span className="text-xs px-1.5 py-0.5 bg-yellow-100 dark:bg-yellow-400/20 text-yellow-700 dark:text-yellow-400 rounded shrink-0">
                        {t('queueCard.manualStart')}
                      </span>
                    )}
                  </div>
                );
              })}
            </>
          )}
        </div>
      )}

      {editingItem && (
        <PrintModal
          mode="edit-queue-item"
          archiveId={editingItem.archive_id ?? undefined}
          libraryFileId={editingItem.library_file_id ?? undefined}
          archiveName={editingItem.archive_name || editingItem.library_file_name || `#${editingItem.id}`}
          queueItem={editingItem}
          onClose={() => setEditingItem(null)}
        />
      )}
    </div>
  );
}
