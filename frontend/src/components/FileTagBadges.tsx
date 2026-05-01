import { useTranslation } from 'react-i18next';
import { TAG_STYLES, UNKNOWN_TAG_BG, UNKNOWN_TAG_TEXT } from '../lib/fileTags';

// Composite badge row driven by ``LibraryFile.file_tags`` (m036). Replaces
// the three independent inline badges the FileManager used to render
// (primary file_type pill, MP for multi-plate, SWAP for swap-compatible)
// and the separate provenance ``SourceBadge`` component. Backend computes
// the tag list at every write site via ``compute_file_tags`` so this
// component only renders — no derivation.
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
}

export function FileTagBadges({ tags, compact = false }: FileTagBadgesProps) {
  const { t } = useTranslation();
  if (!tags || tags.length === 0) return null;
  const sizing = compact ? 'text-[10px] px-1 py-0.5' : 'text-xs px-1.5 py-0.5';
  return (
    <div className="flex items-center gap-1 flex-wrap">
      {tags.map((tag) => {
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
