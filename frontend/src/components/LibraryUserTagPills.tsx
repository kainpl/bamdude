/**
 * The user-tag half of the File Manager's tag filter row.
 *
 * A catalogue of a hundred tags used to render as a hundred chips: a dozen rows
 * that left a strip of the file list — and, on the widths where the filter
 * panel is sticky, a wall taller than the window that could not be scrolled
 * away. So the chips are clamped to two rows; "Show all" opens a box that
 * scrolls inside itself and stays a bounded share of the window.
 *
 * - Selected tags lead, so an active filter is never hidden by the clamp.
 * - Whether there IS anything to show is measured, not guessed from a count:
 *   chips are as wide as their names.
 * - From `SEARCH_FROM` tags up, a box narrows the unselected ones by name
 *   (selected ones stay — a filter you cannot see is a filter you cannot lift).
 * - Filtering itself is untouched: every chip still toggles the same id the
 *   server AND-filters on.
 */
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type FocusEvent } from 'react';
import { useTranslation } from 'react-i18next';
import { Search, X } from 'lucide-react';

/** More user tags than this and the row grows a search box. */
export const SEARCH_FROM = 12;

interface TagPill {
  id: number;
  name: string;
}

interface Props {
  tags: TagPill[];
  selectedIds: number[];
  onToggle: (id: number) => void;
}

export function LibraryUserTagPills({ tags, selectedIds, onToggle }: Props) {
  const { t } = useTranslation();
  const boxRef = useRef<HTMLDivElement>(null);
  const [expanded, setExpanded] = useState(false);
  const [overflowing, setOverflowing] = useState(false);
  const [query, setQuery] = useState('');

  const searchable = tags.length > SEARCH_FROM;
  const needle = searchable ? query.trim().toLocaleLowerCase() : '';
  const visible = useMemo(() => {
    const selected = tags.filter((tag) => selectedIds.includes(tag.id));
    const rest = tags.filter(
      (tag) => !selectedIds.includes(tag.id) && (!needle || tag.name.toLocaleLowerCase().includes(needle)),
    );
    return [...selected, ...rest];
  }, [tags, selectedIds, needle]);

  // Does the clamp hide anything? Asked of the box itself, after every change
  // of what it holds and whenever its width changes.
  useLayoutEffect(() => {
    const box = boxRef.current;
    if (!box) return;
    const measure = () => setOverflowing(box.scrollHeight > box.clientHeight + 1);
    measure();
    if (typeof ResizeObserver === 'undefined') {
      window.addEventListener('resize', measure);
      return () => window.removeEventListener('resize', measure);
    }
    const observer = new ResizeObserver(measure);
    observer.observe(box);
    return () => observer.disconnect();
  }, [visible, expanded]);

  // A collapsed box that was scrolled (by Tab into a hidden chip, or while it
  // was open) would show its middle rows.
  useEffect(() => {
    if (!expanded && boxRef.current) boxRef.current.scrollTop = 0;
  }, [expanded]);

  // Tab reaches chips the clamp hides; show where the focus went.
  const onFocus = (e: FocusEvent<HTMLDivElement>) => {
    const box = boxRef.current;
    if (expanded || !box || e.target === box) return;
    if (e.target.getBoundingClientRect().bottom > box.getBoundingClientRect().bottom + 1) setExpanded(true);
  };

  const showToggle = expanded || overflowing;

  return (
    <div className="flex min-w-[min(100%,16rem)] flex-1 flex-col gap-1.5">
      <div
        ref={boxRef}
        data-testid="user-tag-pills"
        onFocus={onFocus}
        className={`flex flex-wrap items-center gap-1.5 ${
          expanded ? 'max-h-[40vh] overflow-y-auto sm:max-lg:max-h-[25vh]' : 'max-h-[3.25rem] overflow-hidden'
        }`}
      >
        {visible.map((tag) => {
          const active = selectedIds.includes(tag.id);
          return (
            <button
              key={tag.id}
              type="button"
              onClick={() => onToggle(tag.id)}
              title={tag.name}
              className={`text-xs px-2 py-0.5 rounded-full font-medium transition-colors inline-flex max-w-[14rem] items-center gap-1 ${
                active
                  ? 'bg-bambu-green text-white'
                  : 'bg-bambu-dark border border-bambu-dark-tertiary text-bambu-gray hover:text-white hover:border-bambu-green/60'
              }`}
            >
              <span className="truncate">{tag.name}</span>
              {active && <X className="w-3 h-3 shrink-0" />}
            </button>
          );
        })}
        {needle && visible.length === 0 && (
          <span className="text-xs text-bambu-gray">{t('fileManager.tags.noMatch')}</span>
        )}
      </div>
      {(searchable || showToggle) && (
        <div className="flex flex-wrap items-center gap-2">
          {searchable && (
            <div className="relative h-7 w-48 max-w-full">
              <Search className="absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-bambu-gray/50" />
              <input
                type="search"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder={t('fileManager.tags.search')}
                aria-label={t('fileManager.tags.search')}
                className="h-7 w-full rounded-lg border border-bambu-dark-tertiary bg-bambu-dark pl-7 pr-2 text-xs text-white placeholder:text-bambu-gray/50 focus:border-bambu-green focus:outline-none"
              />
            </div>
          )}
          {showToggle && (
            <button
              type="button"
              aria-expanded={expanded}
              onClick={() => setExpanded((open) => !open)}
              className="rounded px-2 py-0.5 text-xs text-bambu-gray transition-colors hover:bg-bambu-dark-tertiary hover:text-white"
            >
              {expanded ? t('fileManager.tags.showLess') : t('fileManager.tags.showAll', { count: tags.length })}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
