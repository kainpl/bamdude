import { useTranslation } from 'react-i18next';
import { ArrowDownWideNarrow, ArrowUpNarrowWide } from 'lucide-react';
import { Select } from './Select';

export interface ListSortOption {
  /** The server's sort key — the part of `sort_by` before `-asc` / `-desc`. */
  key: string;
  label: string;
  /** Dates, counts and priority read best largest-first; a name reads best A→Z. */
  descFirst?: boolean;
}

/**
 * The toolbar sort of the projects-section lists: a key and a direction, the
 * archive's grid control (spec projects-lists-parity). It exists for the cards
 * view — a table sorts from its headers — and writes one `sort_by` value, the
 * same one the headers write, so the two can never disagree.
 */
export function ListSortControl({
  sort,
  options,
  onChange,
}: {
  /** The current `sort_by`, e.g. `name-asc`. */
  sort: string;
  options: ListSortOption[];
  onChange: (sortBy: string) => void;
}) {
  const { t } = useTranslation();
  const [key, dir] = sort.split(/-(?=asc$|desc$)/);
  const descending = dir === 'desc';

  return (
    <div className="flex items-center gap-1">
      <Select
        className="min-w-[7rem]"
        value={key}
        aria-label={t('list.sort.label')}
        onChange={(e) => {
          const picked = options.find((o) => o.key === e.target.value);
          onChange(`${e.target.value}-${picked?.descFirst ? 'desc' : 'asc'}`);
        }}
      >
        {options.map((o) => (
          <option key={o.key} value={o.key}>
            {o.label}
          </option>
        ))}
      </Select>
      <button
        type="button"
        onClick={() => onChange(`${key}-${descending ? 'asc' : 'desc'}`)}
        aria-label={descending ? t('list.sort.desc') : t('list.sort.asc')}
        title={descending ? t('list.sort.desc') : t('list.sort.asc')}
        className="h-9 w-9 flex items-center justify-center bg-bambu-dark border border-bambu-dark-tertiary rounded-lg hover:border-bambu-green transition-colors"
      >
        {descending ? (
          <ArrowDownWideNarrow className="w-4 h-4 text-bambu-gray" />
        ) : (
          <ArrowUpNarrowWide className="w-4 h-4 text-bambu-gray" />
        )}
      </button>
    </div>
  );
}
