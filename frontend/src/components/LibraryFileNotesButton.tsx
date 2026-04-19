import { useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { MessageSquare, MessageSquarePlus } from 'lucide-react';
import { LibraryFileNotesPopover } from './LibraryFileNotesPopover';

interface Props {
  fileId: number;
  /** Initial count from the file list response - kept in sync via onCountChange. */
  initialCount: number;
  /**
   * Visual variant:
   * - `overlay` - pill over a thumbnail (card view). Has its own wrapper div for positioning.
   * - `inline`  - bare button that inherits its parent's flex layout (list view rows).
   *              No wrapper div, no opinionated styling beyond neutral padding/hover -
   *              matches the surrounding Print/Clock/Box action buttons.
   */
  variant?: 'overlay' | 'inline';
  /** Optional callback when the count changes locally (e.g. parent wants to refresh). */
  onCountChange?: (newCount: number) => void;
}

export function LibraryFileNotesButton({ fileId, initialCount, variant = 'inline', onCountChange }: Props) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [count, setCount] = useState(initialCount);
  // Button itself is the popover anchor - keeping the ref on the <button>
  // means we don't need a wrapper <div>, so the inline variant drops in
  // alongside plain action buttons (Printer/Clock/Box) in a flex row.
  const buttonRef = useRef<HTMLButtonElement | null>(null);

  const Icon = count > 0 ? MessageSquare : MessageSquarePlus;
  const label = count > 0
    ? t('libraryNotes.viewNotes', { count })
    : t('libraryNotes.addFirstNote');

  const buttonClass = variant === 'overlay'
    ? 'rounded-md bg-bambu-dark/80 backdrop-blur text-white hover:bg-bambu-dark transition-colors flex items-center'
    : 'p-1.5 rounded transition-colors text-bambu-gray hover:text-bambu-green hover:bg-bambu-dark flex items-center';

  const iconClass = variant === 'overlay' ? 'w-5 h-5' : 'w-4 h-4';

  // Overlay (card thumbnails) is anchored bottom-left; extend popover right.
  // Inline (list rows) lives at the right edge of actions; extend popover left.
  const align = variant === 'overlay' ? 'left' : 'right';

  const popover = (
    <LibraryFileNotesPopover
      fileId={fileId}
      open={open}
      anchorRef={buttonRef}
      align={align}
      onClose={() => setOpen(false)}
      onCountChange={(newCount) => {
        setCount(newCount);
        onCountChange?.(newCount);
      }}
    />
  );

  const btn = (
    <button
      ref={buttonRef}
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
      <Icon className={iconClass} />
      {count > 0 && (
        <span className="ml-1 text-xs font-medium tabular-nums">{count}</span>
      )}
    </button>
  );

  if (variant === 'inline') {
    // No wrapping div - the button drops directly into the parent flex row.
    // Popover is portal-rendered so it doesn't need a nearby positioned ancestor.
    return (
      <>
        {btn}
        {popover}
      </>
    );
  }

  // Overlay: keep the wrapper so absolute positioning from the caller works.
  return (
    <div className="relative inline-block">
      {btn}
      {popover}
    </div>
  );
}
