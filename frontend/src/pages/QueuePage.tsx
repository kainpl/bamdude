import { useEffect, useState, useMemo } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Calendar, LayoutGrid, Loader2 } from 'lucide-react';
import { api } from '../api/client';
import type { PrinterQueue, PrintQueueItem } from '../api/client';
import { QueueCard } from '../components/QueueCard';
import { QueueStatsBar } from '../components/Queue/QueueStatsBar';
import { StaggerBanner } from '../components/Queue/StaggerBanner';
import { QueueTimelineView } from '../components/Queue/QueueTimelineView';
import { AutoQueuePanel } from '../components/Queue/AutoQueuePanel';
import { QueueToolbar } from '../components/Queue/QueueToolbar';
import { PrintModal } from '../components/PrintModal';

type ViewMode = 'expanded' | 'all' | 'timeline';
type SortOption = 'name' | 'status' | 'model' | 'location';

const VALID_VIEW_MODES: ViewMode[] = ['expanded', 'all', 'timeline'];

export function QueuePage() {
  const { t } = useTranslation();
  const [searchParams, setSearchParams] = useSearchParams();
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

  const [sortBy, setSortBy] = useState<SortOption>(() => {
    return (localStorage.getItem('queueSortBy') as SortOption) || 'name';
  });

  const [sortAsc, setSortAsc] = useState<boolean>(() => {
    return localStorage.getItem('queueSortAsc') !== 'false';
  });

  const [search, setSearch] = useState<string>(() => localStorage.getItem('queueSearch') || '');
  const [statusFilter, setStatusFilter] = useState<string>(() => localStorage.getItem('queueStatusFilter') || 'all');
  const [locationFilter, setLocationFilter] = useState<string>(() => localStorage.getItem('queueLocationFilter') || 'all');
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

  // Fetch all pending items - used by stats bar + "All" view + Timeline.
  const { data: allPendingItems } = useQuery({
    queryKey: ['queue', 'all', 'pending'],
    queryFn: () => api.getQueue(undefined, 'pending'),
    refetchInterval: 30000,
  });

  // Fetch all printing items (real + virtual external/direct) so Timeline
  // can lay out the "now" slot even for prints initiated outside BamDude.
  const { data: allPrintingItems } = useQuery({
    queryKey: ['queue', 'all', 'printing'],
    queryFn: () => api.getQueue(undefined, 'printing'),
    refetchInterval: 10000,
  });

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

  const handleSortChange = (sort: SortOption) => {
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

  // Grid classes for the cards view
  const gridClasses = 'grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-3';

  // Distinct printer locations for the filter dropdown
  const availableLocations = useMemo(() => {
    if (!queues) return [] as string[];
    const set = new Set<string>();
    queues.forEach(q => { if (q.printer_location) set.add(q.printer_location); });
    return Array.from(set).sort();
  }, [queues]);

  // Filter + sort queues
  const sortedQueues = useMemo(() => {
    if (!queues) return [];
    const term = search.trim().toLowerCase();
    const filtered = queues.filter(q => {
      if (statusFilter !== 'all' && q.status !== statusFilter) return false;
      if (locationFilter !== 'all' && (q.printer_location || '') !== locationFilter) return false;
      if (term) {
        const name = (q.printer_name || '').toLowerCase();
        const model = (q.printer_model || '').toLowerCase();
        const loc = (q.printer_location || '').toLowerCase();
        if (!name.includes(term) && !model.includes(term) && !loc.includes(term)) return false;
      }
      if (hideOffline) {
        const status = queryClient.getQueryData<{ connected: boolean }>(['printerStatus', q.printer_id]);
        if (!status?.connected) return false;
      }
      return true;
    });

    const statusOrder: Record<string, number> = { printing: 0, error: 1, paused: 2, idle: 3 };

    switch (sortBy) {
      case 'name':
        filtered.sort((a, b) => (a.printer_name || '').localeCompare(b.printer_name || ''));
        break;
      case 'status':
        filtered.sort((a, b) => {
          const aO = statusOrder[a.status] ?? 4;
          const bO = statusOrder[b.status] ?? 4;
          if (aO !== bO) return aO - bO;
          if (b.pending_count !== a.pending_count) return b.pending_count - a.pending_count;
          return (a.printer_name || '').localeCompare(b.printer_name || '');
        });
        break;
      case 'model':
        filtered.sort((a, b) => (a.printer_model || '').localeCompare(b.printer_model || ''));
        break;
      case 'location':
        filtered.sort((a, b) => (a.printer_location || '').localeCompare(b.printer_location || ''));
        break;
    }

    if (!sortAsc) filtered.reverse();
    return filtered;
    // eslint-disable-next-line react-hooks/exhaustive-deps -- statusCacheVersion is intentional: it forces recompute when WS / poll updates printer status cache; queryClient is stable
  }, [queues, search, statusFilter, locationFilter, hideOffline, sortBy, sortAsc, statusCacheVersion]);

  const hasActiveFilters = search.trim() !== '' || statusFilter !== 'all' || locationFilter !== 'all';

  // Group queues by location (when sorted by location)
  const groupedQueues = useMemo(() => {
    if (sortBy !== 'location') return null;
    const groups: Record<string, PrinterQueue[]> = {};
    sortedQueues.forEach(q => {
      const loc = q.printer_location || t('queueCard.ungrouped');
      if (!groups[loc]) groups[loc] = [];
      groups[loc].push(q);
    });
    return groups;
  }, [sortBy, sortedQueues, t]);

  const renderGrid = (items: PrinterQueue[]) => (
    <div className={`grid gap-4 items-start ${gridClasses}`}>
      {items.map((queue) => (
        <QueueCard key={queue.id} queue={queue} onEditItem={setEditingItem} />
      ))}
    </div>
  );

  return (
    <div className="p-4 md:p-6">
      {/* Header: title + inline toolbar (search / filters / view modes) */}
      <div className="space-y-3 mb-6">
        <h1 className="text-2xl font-bold text-white flex items-center gap-3">
          <Calendar className="w-7 h-7 text-bambu-green" />
          {t('queue.title')}
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
            viewMode={viewMode}
            onViewModeChange={handleViewChange}
            hideOffline={hideOffline}
            onHideOfflineToggle={toggleHideOffline}
          />
        )}
      </div>

      {/* Stats bar */}
      {!isLoading && queues && queues.length > 0 && (
        <QueueStatsBar queues={queues} pendingItems={allPendingItems} />
      )}

      {/* Auto-queue router items (sits above per-printer queues). Hidden when empty. */}
      <AutoQueuePanel />

      {/* Electrical-load diagnostic banner (stagger). Hidden when stagger is disabled. */}
      <StaggerBanner />

      {/* Loading */}
      {isLoading && (
        <div className="flex justify-center items-center py-20">
          <Loader2 className="w-8 h-8 text-bambu-green animate-spin" />
        </div>
      )}

      {/* Empty state */}
      {!isLoading && (!queues || queues.length === 0) && (
        <div className="text-center py-20">
          <LayoutGrid className="w-12 h-12 text-bambu-gray mx-auto mb-3 opacity-50" />
          <p className="text-bambu-gray">{t('queueCard.noQueues')}</p>
          <p className="text-sm text-bambu-gray mt-1">{t('queueCard.noQueuesHint')}</p>
        </div>
      )}

      {/* No search/filter results (S / M / Timeline) */}
      {!isLoading && queues && queues.length > 0 && viewMode !== 'all' && sortedQueues.length === 0 && hasActiveFilters && (
        <div className="text-center py-12 text-bambu-gray">
          {t('printers.noSearchResults')}
        </div>
      )}

      {/* Card grid (S and M modes) */}
      {!isLoading && queues && queues.length > 0 && viewMode !== 'all' && viewMode !== 'timeline' && sortedQueues.length > 0 && (
        groupedQueues ? (
          // Grouped by location
          <div className="space-y-6">
            {Object.entries(groupedQueues).map(([location, locationQueues]) => (
              <div key={location}>
                <h2 className="text-lg font-semibold text-white mb-3 flex items-center gap-2">
                  <span className="w-2 h-2 rounded-full bg-bambu-green" />
                  {location}
                  <span className="text-sm font-normal text-bambu-gray">({locationQueues.length})</span>
                </h2>
                {renderGrid(locationQueues)}
              </div>
            ))}
          </div>
        ) : (
          renderGrid(sortedQueues)
        )
      )}

      {/* Timeline view */}
      {!isLoading && queues && queues.length > 0 && viewMode === 'timeline' && sortedQueues.length > 0 && (
        <QueueTimelineView
          queues={sortedQueues}
          items={allTimelineItems}
          onEditItem={setEditingItem}
        />
      )}

      {/* All view - flat list: active prints first (real + virtual
          external/direct), then pending items numbered #1, #2, … */}
      {!isLoading && viewMode === 'all' && (
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
                          <span className="ml-2 text-amber-400">
                            · {t(`queue.source.${item.source}`)}
                          </span>
                        )}
                      </p>
                    </div>
                    <span className="text-xs px-1.5 py-0.5 bg-blue-400/20 text-blue-400 rounded shrink-0">
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
                          <span className="ml-2 text-yellow-400">· {item.waiting_reason}</span>
                        )}
                      </p>
                    </div>
                    {item.manual_start && (
                      <span className="text-xs px-1.5 py-0.5 bg-yellow-400/20 text-yellow-400 rounded shrink-0">
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
