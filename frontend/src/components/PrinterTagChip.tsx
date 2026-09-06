import { X } from 'lucide-react';
import { tagChipStyle } from '../utils/tagColors';

interface Props {
  tag: { name: string; color?: string | null };
  /** Renders a remove cross; the label is the accessible name of that button. */
  onRemove?: { label: string; onClick: () => void };
  /** `xs` is the card strip (10px), `sm` the form and manager (12px). */
  size?: 'xs' | 'sm';
}

/** The one chip every tag renders through, so a colour picked once shows everywhere. */
export function PrinterTagChip({ tag, onRemove, size = 'sm' }: Props) {
  const style = tagChipStyle(tag.color);
  const dims = size === 'xs' ? 'px-1.5 py-0.5 text-[10px] leading-none' : 'px-2 py-0.5 text-xs';
  const neutral = size === 'xs' ? 'bg-bambu-dark-tertiary text-bambu-gray' : 'bg-bambu-dark-tertiary text-white';
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full border ${dims} ${style ? '' : `${neutral} border-transparent`}`}
      style={style}
    >
      {tag.name}
      {onRemove && (
        <button type="button" className="opacity-70 hover:opacity-100" aria-label={onRemove.label} onClick={onRemove.onClick}>
          <X className="w-3 h-3" />
        </button>
      )}
    </span>
  );
}
