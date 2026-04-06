import { useState, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { useQuery } from '@tanstack/react-query';
import { LayoutGrid, List, Loader2, ArrowUp, ArrowDown } from 'lucide-react';
import { api } from '../api/client';
import type { PrinterQueue } from '../api/client';
import { QueueCard } from '../components/QueueCard';

type ViewMode = 'compact' | 'expanded' | 'all';
type SortOption = 'name' | 'status' | 'model' | 'location';

const VIEW_LABELS: { mode: ViewMode; label: string }[] = [
  { mode: 'compact', label: 'S' },
  { mode: 'expanded', label: 'M' },
  { mode: 'all', label: 'All' },
];

export function QueuePage() {
  const { t } = useTranslation();

  const [viewMode, setViewMode] = useState<ViewMode>(() => {
    const saved = localStorage.getItem('queueViewMode');
    return (saved as ViewMode) || 'expanded';
  });

  const [sortBy, setSortBy] = useState<SortOption>(() => {
    return (localStorage.getItem('queueSortBy') as SortOption) || 'name';
  });

  const [sortAsc, setSortAsc] = useState<boolean>(() => {
    return localStorage.getItem('queueSortAsc') !== 'false';
  });

  // Fetch all printer queues
  const { data: queues, isLoading } = useQuery({
    queryKey: ['queues'],
    queryFn: api.getQueues,
    refetchInterval: 15000,
  });

  // Fetch all pending items for "All" view
  const { data: allPendingItems } = useQuery({
    queryKey: ['queue', 'all', 'pending'],
    queryFn: () => api.getQueue(undefined, 'pending'),
    refetchInterval: 30000,
    enabled: viewMode === 'all',
  });

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

  // Grid classes based on view mode
  const getGridClasses = () => {
    if (viewMode === 'compact') return 'grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6';
    return 'grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-3';
  };

  // Sort queues
  const sortedQueues = useMemo(() => {
    if (!queues) return [];
    const sorted = [...queues];

    const statusOrder: Record<string, number> = { printing: 0, error: 1, paused: 2, idle: 3 };

    switch (sortBy) {
      case 'name':
        sorted.sort((a, b) => (a.printer_name || '').localeCompare(b.printer_name || ''));
        break;
      case 'status':
        sorted.sort((a, b) => {
          const aO = statusOrder[a.status] ?? 4;
          const bO = statusOrder[b.status] ?? 4;
          if (aO !== bO) return aO - bO;
          if (b.pending_count !== a.pending_count) return b.pending_count - a.pending_count;
          return (a.printer_name || '').localeCompare(b.printer_name || '');
        });
        break;
      case 'model':
        sorted.sort((a, b) => (a.printer_model || '').localeCompare(b.printer_model || ''));
        break;
      case 'location':
        sorted.sort((a, b) => (a.printer_location || '').localeCompare(b.printer_location || ''));
        break;
    }

    if (!sortAsc) sorted.reverse();
    return sorted;
  }, [queues, sortBy, sortAsc]);

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

  // Total stats
  const totalPending = queues?.reduce((sum, q) => sum + q.pending_count, 0) || 0;
  const totalPrinting = queues?.filter(q => q.status === 'printing').length || 0;
  const totalError = queues?.filter(q => q.status === 'error').length || 0;

  const renderGrid = (items: PrinterQueue[]) => (
    <div className={`grid gap-4 ${getGridClasses()}`}>
      {items.map((queue) => (
        <QueueCard key={queue.id} queue={queue} compact={viewMode === 'compact'} />
      ))}
    </div>
  );

  return (
    <div className="p-4 md:p-6 max-w-[1600px] mx-auto">
      {/* Header */}
      <div className="flex flex-wrap items-center justify-between gap-3 mb-6">
        <div>
          <h1 className="text-2xl font-bold text-white">
            {t('queue.title')}
          </h1>
          {queues && (
            <p className="text-sm text-bambu-gray mt-1">
              {totalPrinting > 0 && <span className="text-blue-400">{t('queueCard.statusPrinting')} {totalPrinting}</span>}
              {totalPrinting > 0 && totalPending > 0 && <span className="mx-1.5">·</span>}
              {totalPending > 0 && <span>{totalPending} {t('queueCard.pending')}</span>}
              {totalError > 0 && <span className="ml-1.5">· <span className="text-red-400">{totalError} {t('queueCard.statusError')}</span></span>}
              {totalPrinting === 0 && totalPending === 0 && totalError === 0 && (
                <span>{t('queueCard.noPending')}</span>
              )}
            </p>
          )}
        </div>

        <div className="flex items-center gap-2 sm:gap-3 flex-wrap">
          {/* Sort dropdown */}
          {viewMode !== 'all' && (
            <div className="flex items-center gap-1">
              <select
                value={sortBy}
                onChange={(e) => handleSortChange(e.target.value as SortOption)}
                className="text-sm bg-bambu-dark border border-bambu-dark-tertiary rounded-lg px-2 py-1.5 text-white focus:border-bambu-green focus:outline-none"
              >
                <option value="status">{t('printers.sort.status')}</option>
                <option value="name">{t('printers.sort.name')}</option>
                <option value="model">{t('printers.sort.model')}</option>
                <option value="location">{t('printers.sort.location')}</option>
              </select>
              <button
                onClick={toggleSortDirection}
                className="p-1.5 rounded-lg hover:bg-bambu-dark-tertiary transition-colors"
                title={sortAsc ? t('printers.sort.descending') : t('printers.sort.ascending')}
              >
                {sortAsc ? <ArrowUp className="w-4 h-4 text-bambu-gray" /> : <ArrowDown className="w-4 h-4 text-bambu-gray" />}
              </button>
            </div>
          )}

          {/* View mode selector */}
          <div className="flex items-center bg-bambu-dark rounded-lg border border-bambu-dark-tertiary">
            {VIEW_LABELS.map(({ mode, label }, index) => {
              const isSelected = viewMode === mode;
              return (
                <button
                  key={mode}
                  onClick={() => handleViewChange(mode)}
                  className={`px-3 py-1.5 text-xs font-medium transition-colors ${
                    index === 0 ? 'rounded-l-lg' : ''
                  } ${
                    index === VIEW_LABELS.length - 1 ? 'rounded-r-lg' : ''
                  } ${
                    isSelected
                      ? 'bg-bambu-green text-white'
                      : 'text-bambu-gray hover:bg-bambu-dark-tertiary hover:text-white'
                  }`}
                  title={mode === 'compact' ? t('queueCard.viewCompact') : mode === 'expanded' ? t('queueCard.viewExpanded') : t('queueCard.viewAll')}
                >
                  {mode === 'all' ? (
                    <span className="flex items-center gap-1">
                      <List className="w-3.5 h-3.5" />
                      {label}
                    </span>
                  ) : label}
                </button>
              );
            })}
          </div>
        </div>
      </div>

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

      {/* Card grid (S and M modes) */}
      {!isLoading && queues && queues.length > 0 && viewMode !== 'all' && (
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

      {/* All view — flat list of all pending items */}
      {!isLoading && viewMode === 'all' && (
        <div className="space-y-2">
          {(!allPendingItems || allPendingItems.length === 0) ? (
            <div className="text-center py-12">
              <p className="text-bambu-gray">{t('queueCard.noPending')}</p>
            </div>
          ) : (
            allPendingItems.map((item) => {
              const queueInfo = queues?.find(q => q.id === item.queue_id);
              return (
                <div
                  key={item.id}
                  className="flex items-center gap-3 p-3 bg-bambu-dark rounded-lg border border-bambu-dark-tertiary"
                >
                  <span className="text-xs text-bambu-gray w-6 text-center shrink-0">#{item.position}</span>
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
            })
          )}
        </div>
      )}
    </div>
  );
}
