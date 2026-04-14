import { useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { MessageSquare, MessageSquarePlus } from 'lucide-react';
import { LibraryFileNotesPopover } from './LibraryFileNotesPopover';

interface Props {
  fileId: number;
  /** Initial count from the file list response — kept in sync via onCountChange. */
  initialCount: number;
  /** Visual variant: overlay (positioned over a thumbnail) or inline (table cell). */
  variant?: 'overlay' | 'inline';
  /** Optional callback when the count changes locally (e.g. parent wants to refresh). */
  onCountChange?: (newCount: number) => void;
}

export function LibraryFileNotesButton({ fileId, initialCount, variant = 'inline', onCountChange }: Props) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [count, setCount] = useState(initialCount);
  const wrapperRef = useRef<HTMLDivElement | null>(null);

  const Icon = count > 0 ? MessageSquare : MessageSquarePlus;
  const label = count > 0
    ? t('libraryNotes.viewNotes', { count })
    : t('libraryNotes.addFirstNote');

  // Overlay variant — pill-style on top of a thumbnail.
  // Inline variant — a small action-row icon button.
  const buttonClass = variant === 'overlay'
    ? 'p-1.5 rounded-md bg-bambu-dark/80 backdrop-blur text-white hover:bg-bambu-dark transition-colors'
    : 'p-1.5 rounded text-bambu-gray hover:text-bambu-green hover:bg-bambu-dark-tertiary transition-colors';

  return (
    <div ref={wrapperRef} className="relative inline-block">
      <button
        type="button"
        aria-label={label}
        title={label}
        onClick={(e) => {
          e.stopPropagation();
          setOpen((v) => !v);
        }}
        className={buttonClass}
        data-testid={`library-file-notes-button-${fileId}`}
      >
        <Icon className="w-4 h-4" />
        {count > 0 && variant === 'overlay' && (
          <span className="ml-1 text-xs font-medium tabular-nums">{count}</span>
        )}
      </button>

      <LibraryFileNotesPopover
        fileId={fileId}
        open={open}
        anchorRef={wrapperRef}
        onClose={() => setOpen(false)}
        onCountChange={(newCount) => {
          setCount(newCount);
          onCountChange?.(newCount);
        }}
      />
    </div>
  );
}
