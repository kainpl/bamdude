import { ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight } from 'lucide-react';
import { useTranslation } from 'react-i18next';

export interface PaginationBarProps {
  /** Current page, 1-based — the number the bar itself shows. */
  page: number;
  totalPages: number;
  /** Rows per page; `-1` means "all", which hides the page controls. */
  perPage: number;
  /** Rows across every page, not on this one. */
  total: number;
  onPageChange: (page: number) => void;
  onPerPageChange: (perPage: number) => void;
  /** The noun for the count ("spools", "archives"), already pluralised by the
   *  caller — only the caller knows which i18n key names its own rows. */
  items: string;
  /** `card` — a footer inside a bordered card, for table views. `bare` — under
   *  a grid of cards, which has no card of its own to sit in. */
  variant?: 'card' | 'bare';
  perPageOptions?: number[];
}

const DEFAULT_PER_PAGE_OPTIONS = [12, 24, 48, 96];

/**
 * How many rows per page, and which page — in one control, under the rows.
 *
 * The two questions were separate on more than one page: page size up in the
 * filters and the arrows somewhere near the title, so changing one meant
 * looking in the other place to see what it did. They are the same question
 * asked twice, so they get one bar, and the bar sits where the rows end —
 * which is where you are looking when you run out of them.
 *
 * **The bar stays when there is only one page**, showing the count and the size
 * selector without the arrows. Hiding it whole is what makes "24 of 24" a dead
 * end: the only control that could ask for more is the one that just
 * disappeared.
 */
export function PaginationBar({
  page,
  totalPages,
  perPage,
  total,
  onPageChange,
  onPerPageChange,
  items,
  variant = 'card',
  perPageOptions = DEFAULT_PER_PAGE_OPTIONS,
}: PaginationBarProps) {
  const { t } = useTranslation();

  // Nothing to page through and nothing to count — the empty state says it.
  if (total === 0) return null;

  const isShowAll = perPage === -1;
  const from = isShowAll ? 1 : (page - 1) * perPage + 1;
  const to = isShowAll ? total : Math.min(page * perPage, total);
  const atFirst = page <= 1;
  const atLast = page >= totalPages;

  // Extra right padding keeps the last-page button clear of the fixed
  // bottom-right bug-report bubble (BugReportBubble, ~64px corner footprint).
  const wrapper =
    variant === 'card'
      ? 'py-3 pl-4 pr-14 bg-bambu-dark-tertiary/50 border-t border-bambu-dark-tertiary'
      : 'pt-2 pr-14';

  const arrow =
    'p-1.5 rounded text-bambu-gray hover:text-white disabled:opacity-30 disabled:cursor-not-allowed transition-colors';

  return (
    <div data-pagination className={`flex flex-wrap items-center justify-between gap-2 text-sm ${wrapper}`}>
      <span className="text-bambu-gray">
        {isShowAll ? `${total} ${items}` : t('common.showingRangeItems', { from, to, total, items })}
      </span>

      <div className="flex items-center gap-2">
        <span className="text-bambu-gray">{t('common.show')}</span>
        <select
          value={perPage}
          onChange={(e) => onPerPageChange(Number(e.target.value))}
          aria-label={t('common.show')}
          className="px-2 py-1 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded text-white text-sm focus:outline-none focus:border-bambu-green"
        >
          {perPageOptions.map((n) => (
            <option key={n} value={n}>
              {n}
            </option>
          ))}
          <option value={-1}>{t('common.all')}</option>
        </select>

        {!isShowAll && totalPages > 1 && (
          <>
            <button
              type="button"
              onClick={() => onPageChange(1)}
              disabled={atFirst}
              className={arrow}
              title={t('common.firstPage')}
              aria-label={t('common.firstPage')}
            >
              <ChevronsLeft className="w-4 h-4" />
            </button>
            <button
              type="button"
              onClick={() => onPageChange(Math.max(1, page - 1))}
              disabled={atFirst}
              className={arrow}
              title={t('common.previousPage')}
              aria-label={t('common.previousPage')}
            >
              <ChevronLeft className="w-4 h-4" />
            </button>
            <span className="text-bambu-gray px-2 whitespace-nowrap">
              {t('common.pageOf', { page, total: totalPages })}
            </span>
            <button
              type="button"
              onClick={() => onPageChange(Math.min(totalPages, page + 1))}
              disabled={atLast}
              className={arrow}
              title={t('common.nextPage')}
              aria-label={t('common.nextPage')}
            >
              <ChevronRight className="w-4 h-4" />
            </button>
            <button
              type="button"
              onClick={() => onPageChange(totalPages)}
              disabled={atLast}
              className={arrow}
              title={t('common.lastPage')}
              aria-label={t('common.lastPage')}
            >
              <ChevronsRight className="w-4 h-4" />
            </button>
          </>
        )}
      </div>
    </div>
  );
}
