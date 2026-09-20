import { useEffect, useRef, useState } from 'react';
import { Pencil, Trash2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../../api/client';
import { PrinterTagChip } from '../PrinterTagChip';
import { TAG_PALETTE } from '../../utils/tagColors';
import { byLocationName } from '../../utils/locationOrder';

interface ColorPaletteProps {
  /** The colour worn now: the swatch shows it, and the palette rings it. */
  value: string | null;
  open: boolean;
  /** The swatch was clicked — the CARD decides which palette that leaves open. */
  onToggle: () => void;
  onPick: (color: string | null) => void;
  /** Escape or a click outside: dismissal, as opposed to a pick. */
  onClose: () => void;
  /** Accessible name of the swatch — the only thing saying whose colour it is. */
  anchorLabel: string;
}

/**
 * The swatch that opens the ten-colour palette, and the palette itself.
 *
 * One component for both callers — a tag's row and the create row ask the very
 * same question, and a second copy of this markup is exactly where the wiring
 * below (`aria-expanded`, Escape returning focus, the outside click) would have
 * been left out.
 *
 * ⚠️ WHICH palette is open belongs to the card, not here: only one may be open
 * at a time, and a rename taking a row over has to close one it does not own.
 * ⚠️ The popover is `w-full order-last` inside the caller's `flex-wrap` row, so
 * it drops onto a line of its own below whatever sits beside the swatch rather
 * than stretching the row it opened from.
 */
function ColorPalette({ value, open, onToggle, onPick, onClose, anchorLabel }: ColorPaletteProps) {
  const { t } = useTranslation();
  // Bound to this palette alone, and only the open one listens at all.
  const anchorRef = useRef<HTMLButtonElement | null>(null);
  const paletteRef = useRef<HTMLDivElement | null>(null);

  /**
   * Dismiss the open palette the two ways a popover is dismissed.
   *
   * Same `mousedown` + ref shape as `components/printers/TagFilterMenu.tsx`.
   * Escape additionally returns focus to the swatch that opened it — closing a
   * popover that stole focus and leaving it on `document.body` strands a
   * keyboard user at the top of the page.
   */
  useEffect(() => {
    if (!open) return;
    const onMouseDown = (e: MouseEvent) => {
      const target = e.target as Node;
      if (paletteRef.current?.contains(target) || anchorRef.current?.contains(target)) return;
      onClose();
    };
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      onClose();
      // Still attached: the ref is only detached on the re-render this schedules.
      anchorRef.current?.focus();
    };
    document.addEventListener('mousedown', onMouseDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('mousedown', onMouseDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open, onClose]);

  return (
    <>
      {/* The swatch is both the state and the control: it shows the colour worn
          and it opens the palette. */}
      <button
        type="button"
        ref={anchorRef}
        aria-label={anchorLabel}
        aria-expanded={open}
        aria-haspopup="true"
        onClick={onToggle}
        className="w-4 h-4 rounded-full border border-bambu-dark-tertiary shrink-0"
        style={{ backgroundColor: value ?? 'transparent' }}
      />
      {open && (
        <div
          ref={paletteRef}
          role="group"
          aria-label={t('printers.tags.pickColor')}
          className="w-full order-last flex flex-wrap items-center gap-1.5 p-2 rounded-lg bg-bambu-dark border border-bambu-dark-tertiary"
        >
          {TAG_PALETTE.map((swatch) => (
            <button
              key={swatch.hex}
              type="button"
              aria-label={t(`printers.tags.colors.${swatch.nameKey}`)}
              title={t(`printers.tags.colors.${swatch.nameKey}`)}
              onClick={() => onPick(swatch.hex)}
              className={`w-5 h-5 rounded-full border-2 ${value === swatch.hex ? 'border-white' : 'border-transparent'}`}
              style={{ backgroundColor: swatch.hex }}
            />
          ))}
          <button type="button" onClick={() => onPick(null)} className="text-xs text-bambu-gray hover:text-white px-1">
            {t('printers.tags.noColor')}
          </button>
        </div>
      )}
    </>
  );
}

/**
 * Manage the labels printers carry.
 *
 * Flat — a tag has no parent — so this is the locations card minus its parent
 * picker. Deleting is refused by the backend while the tag is a staggered-start
 * group, and that refusal is shown as the sentence it is rather than a raw
 * error: the operator has to un-pick it in Staggered start first.
 */
export function PrinterTagsCard() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [name, setName] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editingName, setEditingName] = useState('');
  // Which palette is open — a tag's row, the create row ('new'), or none. One
  // at a time across the whole card: two open palettes are two answers to the
  // same question on screen.
  const [openPaletteFor, setOpenPaletteFor] = useState<number | 'new' | null>(null);
  // The colour the create row is holding. A tag that does not exist yet has
  // nowhere to keep one but the request that creates it.
  const [newColor, setNewColor] = useState<string | null>(null);

  const { data } = useQuery({ queryKey: ['printer-tags'], queryFn: api.getPrinterTags });
  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['printer-tags'] });
    // Printers carry the resolved names, so a rename or a removal has to reach
    // their cache too or the old label lingers until something else refetches.
    queryClient.invalidateQueries({ queryKey: ['printers'] });
  };

  const create = useMutation({
    mutationFn: () => api.createPrinterTag(name.trim(), newColor),
    onSuccess: () => {
      setName('');
      // The row is for the NEXT tag: a colour left behind would be inherited by
      // whatever is typed after it.
      setNewColor(null);
      setError(null);
      invalidate();
    },
    onError: (e: Error) => setError(e.message || t('printers.tags.nameTaken')),
  });

  /**
   * Rename in place.
   *
   * ⚠️ Sent only when the name actually changed and is not empty — the field
   * saves on blur, and a blur is how you leave a row you opened by mistake.
   */
  const rename = useMutation({
    mutationFn: ({ id, name: next }: { id: number; name: string }) => api.updatePrinterTag(id, { name: next }),
    onSuccess: () => {
      setEditingId(null);
      setError(null);
      invalidate();
    },
    onError: (e: Error) => setError(e.message || t('printers.tags.nameTaken')),
  });

  /**
   * Recolour, from the swatch grid.
   *
   * ⚠️ The CURRENT name travels with the colour: the backend's patch schema
   * inherits the create one, so `name` is required and a `{color}`-only body is
   * a 422. "No colour" is an explicit `null` — an empty string is refused too.
   */
  const recolor = useMutation({
    mutationFn: ({ id, name: current, color }: { id: number; name: string; color: string | null }) =>
      api.updatePrinterTag(id, { name: current, color }),
    onSuccess: () => {
      setOpenPaletteFor(null);
      setError(null);
      invalidate();
    },
    /**
     * ⚠️ The server's sentence is deliberately NOT shown here, unlike create and
     * rename. Those carry a refusal the operator can act on (the name is taken);
     * a recolour cannot — the palette only ever sends a known-good hex beside the
     * name the row already has, so every failure is the server's, not the click's.
     * What reaches this handler is either a sentence about a name nobody typed or
     * the client's bare `HTTP 500` placeholder. Neither is worth showing.
     */
    onError: () => setError(t('printers.tags.colorFailed')),
  });

  /** Opening a rename takes the row over; a palette left open under it is stale chrome. */
  const startRename = (id: number, current: string) => {
    setOpenPaletteFor(null);
    setEditingId(id);
    setEditingName(current);
  };

  const commitRename = (id: number, current: string) => {
    const next = editingName.trim();
    if (!next || next === current) {
      setEditingId(null);
      return;
    }
    rename.mutate({ id, name: next });
  };

  const remove = useMutation({
    mutationFn: (id: number) => api.deletePrinterTag(id),
    onSuccess: () => {
      // The row that owned the palette is gone; the id would outlive it and
      // re-open the palette on whichever tag lands on that id next.
      setOpenPaletteFor(null);
      setError(null);
      invalidate();
    },
    // The backend's own sentence when it is a stagger group; the generic line otherwise.
    onError: (e: Error) => setError(e.message || t('printers.tags.inUse')),
  });

  // Sorted here rather than trusted from the server: SQLite orders text by
  // byte, which puts Ґ/Є/І/Ї before А and every lowercase name last.
  const tags = [...(data?.tags ?? [])].sort(byLocationName((tag) => tag.name));

  return (
    // No heading and no card chrome of its own: the page wraps this in a Card
    // that already carries the title, the way PrinterLocationsCard is wrapped.
    <div>
      {tags.length === 0 && <p className="text-sm text-bambu-gray mb-3">{t('printers.tags.empty')}</p>}

      {/* One grid for the whole list, `contents` on each `li`, and the rule
          drawn per CELL — same shape as the locations card, for the same
          reasons written out there. */}
      <ul className="grid grid-cols-[minmax(0,1fr)_auto_auto] items-center gap-x-3 mb-3">
        {tags.map((tag, index) => {
          const cell = `py-2 min-w-0${index < tags.length - 1 ? ' border-b border-bambu-dark-tertiary' : ''}`;
          return (
            <li key={tag.id} className="contents">
              <div className={cell}>
                {editingId === tag.id ? (
                  <input
                    autoFocus
                    aria-label={`${t('common.edit')} ${tag.name}`}
                    className="w-full min-w-0 bg-bambu-dark border border-bambu-green rounded-lg px-2 py-1.5 text-white text-sm focus:outline-none"
                    value={editingName}
                    onChange={(e) => setEditingName(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') commitRename(tag.id, tag.name);
                      if (e.key === 'Escape') setEditingId(null);
                    }}
                    onBlur={() => commitRename(tag.id, tag.name)}
                  />
                ) : (
                  <div className="min-w-0 flex flex-wrap items-center gap-2">
                    {/* Wrapping, so the palette can take a line of its own under
                        the name rather than stretching it. */}
                    <ColorPalette
                      value={tag.color ?? null}
                      open={openPaletteFor === tag.id}
                      onToggle={() => setOpenPaletteFor(openPaletteFor === tag.id ? null : tag.id)}
                      onPick={(color) => recolor.mutate({ id: tag.id, name: tag.name, color })}
                      onClose={() => setOpenPaletteFor(null)}
                      anchorLabel={t('printers.tags.colorOf', { name: tag.name })}
                    />
                    <span className="flex items-center gap-2 text-white truncate min-w-0">
                      <PrinterTagChip tag={tag} />
                      {tag.is_stagger_group && (
                        <span className="text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded bg-amber-500/15 text-amber-500">
                          {t('printers.tags.staggerGroup')}
                        </span>
                      )}
                    </span>
                  </div>
                )}
              </div>
              <div className={cell}>
                <span className="text-xs text-bambu-gray whitespace-nowrap">
                  {t('printers.tags.counts', { count: tag.printer_count })}
                </span>
              </div>
              <div className={`${cell} flex items-center gap-1 justify-self-end`}>
                <button
                  type="button"
                  onClick={() => startRename(tag.id, tag.name)}
                  className="p-1.5 text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary rounded"
                  title={t('common.edit')}
                >
                  <Pencil className="w-4 h-4" />
                </button>
                <button
                  type="button"
                  onClick={() => {
                    // Only when somebody wears it: removing a tag unpins it from
                    // every printer at once, and that is not visible from here.
                    // A tag nobody wears loses nothing, so it goes without a prompt.
                    if (
                      tag.printer_count > 0 &&
                      !window.confirm(t('printers.tags.confirmDelete', { name: tag.name, count: tag.printer_count }))
                    ) {
                      return;
                    }
                    remove.mutate(tag.id);
                  }}
                  className="p-1.5 text-bambu-gray hover:text-red-600 dark:hover:text-red-400 hover:bg-bambu-dark-tertiary rounded"
                  title={t('common.delete')}
                >
                  <Trash2 className="w-4 h-4" />
                </button>
              </div>
            </li>
          );
        })}
      </ul>

      <div className="flex flex-wrap items-center gap-2">
        <ColorPalette
          value={newColor}
          open={openPaletteFor === 'new'}
          onToggle={() => setOpenPaletteFor(openPaletteFor === 'new' ? null : 'new')}
          // Held, not sent: there is no tag to PATCH yet, so the pick travels
          // with the POST that creates one.
          onPick={(color) => {
            setNewColor(color);
            setOpenPaletteFor(null);
          }}
          onClose={() => setOpenPaletteFor(null)}
          anchorLabel={t('printers.tags.colorOfNew')}
        />
        <input
          className="flex-1 min-w-0 px-3 py-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder={t('printers.tags.add')}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && name.trim()) create.mutate();
          }}
        />
        <button
          type="button"
          className="px-3 py-1.5 bg-bambu-green rounded-lg text-white disabled:opacity-50"
          disabled={!name.trim() || create.isPending}
          onClick={() => create.mutate()}
        >
          {t('printers.tags.add')}
        </button>
      </div>

      {error && <p className="text-sm text-status-error mt-2">{error}</p>}
    </div>
  );
}
