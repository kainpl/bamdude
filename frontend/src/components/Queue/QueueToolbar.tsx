import { useEffect, useLayoutEffect, useRef, useState, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import {
  Search, X, ArrowUpNarrowWide, ArrowDownWideNarrow,
  LayoutGrid, List, Activity, Filter, SlidersHorizontal,
} from 'lucide-react';

type ViewMode = 'expanded' | 'all' | 'timeline';
type SortOption = 'name' | 'status' | 'model' | 'location';

interface QueueToolbarProps {
  search: string;
  onSearchChange: (value: string) => void;

  statusFilter: string;
  onStatusFilterChange: (value: string) => void;

  locationFilter: string;
  onLocationFilterChange: (value: string) => void;
  availableLocations: string[];

  sortBy: SortOption;
  onSortByChange: (value: SortOption) => void;
  sortAsc: boolean;
  onSortDirectionToggle: () => void;

  viewMode: ViewMode;
  onViewModeChange: (value: ViewMode) => void;

  hideOffline: boolean;
  onHideOfflineToggle: () => void;
}

const VIEW_BUTTONS: { mode: ViewMode; labelKey: string; icon: typeof List }[] = [
  { mode: 'expanded', labelKey: 'queue.view.cards', icon: LayoutGrid },
  { mode: 'all', labelKey: 'queue.view.list', icon: List },
  { mode: 'timeline', labelKey: 'queue.view.timeline', icon: Activity },
];

/** Local overflow popover (same shell as PrintersPage's ToolbarMenu). */
function OverflowMenu({
  label,
  icon,
  children,
}: {
  label: string;
  icon: React.ReactNode;
  children: React.ReactNode;
}) {
  const [isOpen, setIsOpen] = useState(false);
  return (
    <div className="relative">
      <button
        type="button"
        onClick={() => setIsOpen((v) => !v)}
        className="h-8 w-8 rounded-lg border bg-bambu-dark border-bambu-dark-tertiary text-white hover:bg-bambu-dark-tertiary transition-colors flex items-center justify-center"
        aria-label={label}
        title={label}
      >
        {icon}
      </button>
      {isOpen && (
        <>
          <div className="fixed inset-0 z-10" onClick={() => setIsOpen(false)} />
          <div
            className="absolute right-0 top-full z-20 mt-1 min-w-48 rounded-lg border border-bambu-dark-tertiary bg-bambu-dark-secondary p-2 shadow-xl"
            onClick={() => setIsOpen(false)}
          >
            {children}
          </div>
        </>
      )}
    </div>
  );
}

/**
 * In-header toolbar for QueuePage — same single-row inline pattern PrintersPage
 * uses (search input takes remaining space, control groups separated by thin
 * vertical dividers on the right). On narrow viewports, the inline ribbon
 * collapses to two overflow icon menus (Filters / View) decided by a
 * ResizeObserver against a hidden mirror of the expanded controls.
 *
 * Hosts: search, status filter, location filter, hide-offline toggle, sort
 * dropdown + direction, view modes (Cards / List / Timeline). Location filter
 * auto-collapses when no queue has a location set.
 */
export function QueueToolbar({
  search,
  onSearchChange,
  statusFilter,
  onStatusFilterChange,
  locationFilter,
  onLocationFilterChange,
  availableLocations,
  sortBy,
  onSortByChange,
  sortAsc,
  onSortDirectionToggle,
  viewMode,
  onViewModeChange,
  hideOffline,
  onHideOfflineToggle,
}: QueueToolbarProps) {
  const { t } = useTranslation();

  const wrapperRef = useRef<HTMLDivElement | null>(null);
  const expandedRef = useRef<HTMLDivElement | null>(null);
  const [compact, setCompact] = useState(false);

  const measure = useCallback(() => {
    const wrap = wrapperRef.current;
    const expanded = expandedRef.current;
    if (!wrap || !expanded) return;
    // Reserve ~240 px for the search input (the only flex-1 element); the
    // remainder is what the right-side controls have to fit into.
    const needed = expanded.scrollWidth;
    const available = wrap.clientWidth - 240;
    setCompact(needed > available);
  }, []);

  useLayoutEffect(() => {
    measure();
  }, [measure, availableLocations.length]);

  useEffect(() => {
    const wrap = wrapperRef.current;
    if (!wrap) return;
    const ro = new ResizeObserver(() => measure());
    ro.observe(wrap);
    window.addEventListener('resize', measure);
    return () => {
      ro.disconnect();
      window.removeEventListener('resize', measure);
    };
  }, [measure]);

  const renderFilterControls = (inMenu = false) => {
    const fullWidth = inMenu ? 'w-full' : '';
    return (
      <>
        <select
          value={statusFilter}
          onChange={(e) => onStatusFilterChange(e.target.value)}
          className={`h-8 text-sm bg-bambu-dark border border-bambu-dark-tertiary rounded-lg px-2 text-white focus:border-bambu-green focus:outline-none ${fullWidth}`}
        >
          <option value="all">{t('printers.filter.allStatuses')}</option>
          <option value="printing">{t('printers.status.printing')}</option>
          <option value="paused">{t('printers.status.paused')}</option>
          <option value="idle">{t('printers.status.idle')}</option>
          <option value="error">{t('printers.status.error')}</option>
        </select>

        {availableLocations.length > 0 && (
          <select
            value={locationFilter}
            onChange={(e) => onLocationFilterChange(e.target.value)}
            className={`h-8 text-sm bg-bambu-dark border border-bambu-dark-tertiary rounded-lg px-2 text-white focus:border-bambu-green focus:outline-none ${fullWidth}`}
          >
            <option value="all">{t('printers.filter.allLocations')}</option>
            {availableLocations.map((loc) => (
              <option key={loc} value={loc}>{loc}</option>
            ))}
          </select>
        )}

        <button
          type="button"
          onClick={onHideOfflineToggle}
          aria-pressed={hideOffline}
          className={`h-8 px-2 rounded-lg border text-sm font-medium transition-colors ${fullWidth} ${
            hideOffline
              ? 'bg-bambu-green border-bambu-green text-white'
              : 'bg-bambu-dark border-bambu-dark-tertiary text-white hover:bg-bambu-dark-tertiary'
          }`}
        >
          {t('queue.hideOffline')}
        </button>
      </>
    );
  };

  const renderViewControls = (inMenu = false) => {
    const fullWidth = inMenu ? 'w-full' : '';
    return (
      <>
        <div className={`flex items-center gap-1 ${fullWidth}`}>
          <select
            value={sortBy}
            onChange={(e) => onSortByChange(e.target.value as SortOption)}
            className={`h-8 text-sm bg-bambu-dark border border-bambu-dark-tertiary rounded-lg px-2 text-white focus:border-bambu-green focus:outline-none ${inMenu ? 'flex-1' : ''}`}
          >
            <option value="name">{t('printers.sort.name')}</option>
            <option value="status">{t('printers.sort.status')}</option>
            <option value="model">{t('printers.sort.model')}</option>
            <option value="location">{t('printers.sort.location')}</option>
          </select>
          <button
            type="button"
            onClick={onSortDirectionToggle}
            className="h-8 w-8 shrink-0 flex items-center justify-center bg-bambu-dark border border-bambu-dark-tertiary rounded-lg hover:border-bambu-green transition-colors"
            title={sortAsc ? t('printers.sort.descending') : t('printers.sort.ascending')}
          >
            {sortAsc
              ? <ArrowUpNarrowWide className="w-4 h-4 text-white" />
              : <ArrowDownWideNarrow className="w-4 h-4 text-white" />}
          </button>
        </div>

        {inMenu ? (
          /* In overflow popover — stack as separate full-width buttons so
             "Картки / Список / Таймлайн" labels fit without horizontal cropping. */
          <div className="flex flex-col gap-1 w-full">
            {VIEW_BUTTONS.map(({ mode, labelKey, icon: Icon }) => {
              const isSelected = viewMode === mode;
              const label = t(labelKey);
              return (
                <button
                  key={mode}
                  type="button"
                  onClick={() => onViewModeChange(mode)}
                  className={`h-8 px-2 rounded-lg border text-sm font-medium transition-colors flex items-center gap-2 ${
                    isSelected
                      ? 'bg-bambu-green border-bambu-green text-white'
                      : 'bg-bambu-dark border-bambu-dark-tertiary text-white hover:bg-bambu-dark-tertiary'
                  }`}
                >
                  <Icon className="w-3.5 h-3.5" />
                  {label}
                </button>
              );
            })}
          </div>
        ) : (
          /* Inline — horizontal segmented control. */
          <div className="flex h-8 items-center bg-bambu-dark rounded-lg border border-bambu-dark-tertiary">
            {VIEW_BUTTONS.map(({ mode, labelKey, icon: Icon }, index) => {
              const isSelected = viewMode === mode;
              const label = t(labelKey);
              return (
                <button
                  key={mode}
                  type="button"
                  onClick={() => onViewModeChange(mode)}
                  className={`h-full px-3 text-xs font-medium transition-colors flex items-center justify-center gap-1.5 ${
                    index === 0 ? 'rounded-l-lg' : ''
                  } ${
                    index === VIEW_BUTTONS.length - 1 ? 'rounded-r-lg' : ''
                  } ${
                    isSelected
                      ? 'bg-bambu-green text-white'
                      : 'text-bambu-gray hover:bg-bambu-dark-tertiary hover:text-white'
                  }`}
                  title={label}
                  aria-label={label}
                >
                  <Icon className="w-3.5 h-3.5" />
                  {label}
                </button>
              );
            })}
          </div>
        )}
      </>
    );
  };

  return (
    <div ref={wrapperRef} className="relative flex items-center gap-2">
      {/* Search — always inline, takes remaining width */}
      <div className="relative min-w-0 flex-1">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-bambu-gray/50" />
        <input
          type="search"
          autoComplete="off"
          value={search}
          onChange={(e) => onSearchChange(e.target.value)}
          placeholder={t('queue.search.placeholder')}
          aria-label={t('queue.search.placeholder')}
          className="w-full h-8 pl-9 pr-8 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white text-sm placeholder:text-bambu-gray/50 focus:outline-none focus:border-bambu-green"
        />
        {search && (
          <button
            type="button"
            aria-label={t('common.clear')}
            onClick={() => onSearchChange('')}
            className="absolute right-2 top-1/2 -translate-y-1/2 text-bambu-gray hover:text-white"
          >
            <X className="w-4 h-4" />
          </button>
        )}
      </div>

      {/* Expanded inline controls — visible when wide enough; otherwise kept
          mounted off-screen so its scrollWidth stays measurable. ``inert``
          keeps it out of the focus / AT tree when hidden. */}
      <div
        ref={expandedRef}
        aria-hidden={compact}
        // @ts-expect-error -- `inert` is a valid HTML attribute (DOM-level), TS lib.dom hasn't picked it up universally
        inert={compact ? '' : undefined}
        className={`${compact ? 'absolute -left-[9999px] top-0 flex w-max pointer-events-none opacity-0' : 'flex'} ml-auto items-center gap-2 flex-nowrap [&>*]:shrink-0`}
      >
        <div className="flex items-center gap-2">{renderFilterControls()}</div>
        <div className="h-6 w-px bg-bambu-dark-tertiary" />
        <div className="flex items-center gap-2">{renderViewControls()}</div>
      </div>

      {/* Compact overflow menus — shown when measure() flagged the inline
          ribbon as too wide. Two grouped icons: Filters, View. Each opens
          a popover with the same controls re-rendered full-width. */}
      {compact && (
        <div className="ml-auto flex items-center gap-1">
          <OverflowMenu label={t('queue.toolbar.filters')} icon={<Filter className="w-4 h-4" />}>
            <div className="flex w-48 flex-col gap-2">{renderFilterControls(true)}</div>
          </OverflowMenu>
          <OverflowMenu label={t('queue.toolbar.view')} icon={<SlidersHorizontal className="w-4 h-4" />}>
            <div className="flex w-48 flex-col gap-2">{renderViewControls(true)}</div>
          </OverflowMenu>
        </div>
      )}
    </div>
  );
}
