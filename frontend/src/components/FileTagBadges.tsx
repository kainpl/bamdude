import { useTranslation } from 'react-i18next';
import { TAG_STYLES, UNKNOWN_TAG_BG, UNKNOWN_TAG_TEXT, sortTagsForDisplay } from '../lib/fileTags';

// Composite badge row driven by ``LibraryFile.file_tags`` (m036). Replaces
// the three independent inline badges the FileManager used to render
// (primary file_type pill, MP for multi-plate, SWAP for swap-compatible)
// and folds in the provenance chip (orange ``MW`` for makerworld imports).
// Backend computes the tag list at every write site via ``compute_file_tags``
// so this component only renders — no derivation.
//
// Tag → visual mapping lives in ``lib/fileTags`` (single source of truth)
// so the chip-row filter on the toolbar can reuse the same labels and
// colours.

interface FileTagBadgesProps {
  tags: string[] | null | undefined;
  // Compact rendering shaves the badge to a smaller footprint for the
  // grid-card overlay (where horizontal space is tight). Default style
  // is used in the list view + detail panels.
  compact?: boolean;
  /**
   * Reading direction of the badge row.
   *
   * - ``rtl`` (default, used by the grid card overlay) — the format chip
   *   anchors the right edge so the eye lands on the file's identity
   *   first when scanning the corner of a card; broader context
   *   (provenance / modifiers) fans left.
   * - ``ltr`` (used by the list / table row) — the format chip leads
   *   from the left so the row scans naturally with the rest of the
   *   left-aligned columns; broader context trails right.
   *
   * Same precedence list either way — ``ltr`` is just the reverse.
   */
  direction?: 'rtl' | 'ltr';
}

export function FileTagBadges({ tags, compact = false, direction = 'rtl' }: FileTagBadgesProps) {
  const { t } = useTranslation();
  if (!tags || tags.length === 0) return null;
  const sizing = compact ? 'text-[10px] px-1 py-0.5' : 'text-xs px-1.5 py-0.5';
  // Project the backend's semantic-emission order onto the display
  // precedence in ``lib/fileTags`` so the row reads consistently
  // regardless of which write path produced the row. ``ltr`` flips the
  // sorted output so the format chip leads from the left.
  const sorted = sortTagsForDisplay(tags);
  const ordered = direction === 'ltr' ? [...sorted].reverse() : sorted;
  return (
    <div className="flex items-center gap-1 flex-wrap">
      {ordered.map((tag) => {
        const style = TAG_STYLES[tag];
        const label = style ? t(`library.tags.${tag}`, style.label) : tag.toUpperCase();
        const bg = style?.bg ?? UNKNOWN_TAG_BG;
        const text = style?.text ?? UNKNOWN_TAG_TEXT;
        // Tooltip describes what the tag means rather than echoing the
        // visible short label. ``library.tagTooltips.{tag}`` keys live in
        // both en + uk; falls back to the short label for unknown tags
        // (so the title isn't empty / undefined).
        const tooltip = t(`library.tagTooltips.${tag}`, { defaultValue: label });
        return (
          <span
            key={tag}
            className={`${sizing} ${bg} ${text} rounded font-medium tracking-tight`}
            title={tooltip}
          >
            {label}
          </span>
        );
      })}
    </div>
  );
}
