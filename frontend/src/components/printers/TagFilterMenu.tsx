import { useEffect, useRef, useState } from 'react';
import { Tag as TagIcon } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { PrinterTag } from '../../api/client';
import { PrinterTagChip } from '../PrinterTagChip';

interface Props {
  tags: PrinterTag[];
  selected: number[];
  onChange: (ids: number[]) => void;
  fullWidth?: boolean;
}

/**
 * The Printers-page tag filter: a button with a count, opening a list of tag
 * checkboxes. ALL selected tags must be worn — stacking narrows, like adding a
 * second word to a search — which is why it is a checkbox list and not a
 * single-value dropdown like the location filter.
 */
export function TagFilterMenu({ tags, selected, onChange, fullWidth }: Props) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const button = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent) => {
      if (root.current && !root.current.contains(e.target as Node)) setOpen(false);
    };
    // Escape is the other way out of a popover — and it has to hand focus back
    // to the button, or a keyboard user closes the list onto `document.body`
    // and starts again from the top of the page.
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      setOpen(false);
      button.current?.focus();
    };
    document.addEventListener('mousedown', close);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', close);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  const toggle = (id: number) => onChange(selected.includes(id) ? selected.filter((v) => v !== id) : [...selected, id]);

  return (
    <div ref={root} className={`relative ${fullWidth ? 'w-full' : ''}`}>
      <button
        type="button"
        ref={button}
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-haspopup="true"
        className={`h-8 px-2 rounded-lg border text-sm font-medium transition-colors inline-flex items-center gap-1.5 ${fullWidth ? 'w-full' : ''} ${
          selected.length > 0
            ? 'bg-bambu-green border-bambu-green text-white'
            : 'bg-bambu-dark border-bambu-dark-tertiary text-white hover:bg-bambu-dark-tertiary'
        }`}
      >
        <TagIcon className="w-4 h-4" />
        {t('printers.filter.tags')}
        {selected.length > 0 && <span className="text-xs opacity-90">({selected.length})</span>}
      </button>
      {open && (
        <div role="group" aria-label={t('printers.filter.tags')} className="absolute z-20 mt-1 min-w-[12rem] p-2 rounded-lg bg-bambu-dark-secondary border border-bambu-dark-tertiary shadow-lg space-y-1">
          {tags.map((tag) => (
            <label key={tag.id} className="flex items-center gap-2 text-sm text-white cursor-pointer">
              <input type="checkbox" checked={selected.includes(tag.id)} onChange={() => toggle(tag.id)} aria-label={tag.name} />
              <PrinterTagChip tag={tag} />
            </label>
          ))}
          {selected.length > 0 && (
            <button type="button" onClick={() => onChange([])} className="text-xs text-bambu-gray hover:text-white pt-1">
              {t('printers.filter.clearTags')}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
