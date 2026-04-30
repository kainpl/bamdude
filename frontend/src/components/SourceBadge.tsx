import { useTranslation } from 'react-i18next';
import { MakerWorldIcon } from './BrandIcons';

interface Props {
  sourceType: string | null | undefined;
  sourceUrl: string | null | undefined;
  /**
   * Visual variant:
   * - `card`  — square glyph badge sized to match the type/SWAP/MP pills on
   *             a file card (top-right of the thumbnail).
   * - `row`   — slightly smaller glyph for the list view's badge cluster.
   */
  variant?: 'card' | 'row';
}

/**
 * Provenance badge for library files. Currently surfaces only the
 * ``source_type === 'makerworld'`` case — slicer output (``'sliced'``) is the
 * majority and would add visual noise on almost every row, plain uploads have
 * no source. When ``source_url`` is present the badge becomes a link that
 * opens the original page in a new tab.
 */
export function SourceBadge({ sourceType, sourceUrl, variant = 'card' }: Props) {
  const { t } = useTranslation();

  if (sourceType !== 'makerworld') return null;

  const sizeCls = variant === 'card' ? 'w-4 h-4' : 'w-3.5 h-3.5';
  const label = t('fileManager.source.makerworldImported', { defaultValue: 'Imported from MakerWorld' });
  const linkLabel = sourceUrl
    ? t('fileManager.source.openOriginal', { defaultValue: 'Open on MakerWorld' })
    : label;

  if (sourceUrl) {
    return (
      <a
        href={sourceUrl}
        target="_blank"
        rel="noopener noreferrer"
        onClick={(e) => e.stopPropagation()}
        title={linkLabel}
        aria-label={linkLabel}
        className="inline-flex items-center justify-center rounded text-bambu-green hover:text-white hover:bg-bambu-green transition-colors p-0.5"
      >
        <MakerWorldIcon className={sizeCls} />
      </a>
    );
  }

  return (
    <span
      title={label}
      aria-label={label}
      className="inline-flex items-center justify-center text-bambu-green p-0.5"
    >
      <MakerWorldIcon className={sizeCls} />
    </span>
  );
}
