import { useState } from 'react';
import { Pencil, Trash2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../../api/client';
import { PrinterTagChip } from '../PrinterTagChip';
import { TAG_PALETTE } from '../../utils/tagColors';
import { byLocationName } from '../../utils/locationOrder';

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
  const [colorPickerId, setColorPickerId] = useState<number | null>(null);

  const { data } = useQuery({ queryKey: ['printer-tags'], queryFn: api.getPrinterTags });
  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['printer-tags'] });
    // Printers carry the resolved names, so a rename or a removal has to reach
    // their cache too or the old label lingers until something else refetches.
    queryClient.invalidateQueries({ queryKey: ['printers'] });
  };

  const create = useMutation({
    mutationFn: () => api.createPrinterTag(name.trim()),
    onSuccess: () => {
      setName('');
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
      setColorPickerId(null);
      setError(null);
      invalidate();
    },
    onError: (e: Error) => setError(e.message || t('printers.tags.nameTaken')),
  });

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
                  <div className="min-w-0">
                    <span className="flex items-center gap-2 text-white truncate">
                      {/* The swatch is both the state and the control: it shows
                          the colour the tag wears and opens the palette. */}
                      <button
                        type="button"
                        aria-label={t('printers.tags.colorOf', { name: tag.name })}
                        onClick={() => setColorPickerId(colorPickerId === tag.id ? null : tag.id)}
                        className="w-4 h-4 rounded-full border border-bambu-dark-tertiary shrink-0"
                        style={{ backgroundColor: tag.color ?? 'transparent' }}
                      />
                      <PrinterTagChip tag={tag} />
                      {tag.is_stagger_group && (
                        <span className="text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded bg-amber-500/15 text-amber-500">
                          {t('printers.tags.staggerGroup')}
                        </span>
                      )}
                    </span>
                    {colorPickerId === tag.id && (
                      <div
                        role="group"
                        aria-label={t('printers.tags.pickColor')}
                        className="mt-2 flex flex-wrap items-center gap-1.5 p-2 rounded-lg bg-bambu-dark border border-bambu-dark-tertiary"
                      >
                        {TAG_PALETTE.map((swatch) => (
                          <button
                            key={swatch.hex}
                            type="button"
                            aria-label={t(`printers.tags.colors.${swatch.nameKey}`)}
                            title={t(`printers.tags.colors.${swatch.nameKey}`)}
                            onClick={() => recolor.mutate({ id: tag.id, name: tag.name, color: swatch.hex })}
                            className={`w-5 h-5 rounded-full border-2 ${tag.color === swatch.hex ? 'border-white' : 'border-transparent'}`}
                            style={{ backgroundColor: swatch.hex }}
                          />
                        ))}
                        <button
                          type="button"
                          onClick={() => recolor.mutate({ id: tag.id, name: tag.name, color: null })}
                          className="text-xs text-bambu-gray hover:text-white px-1"
                        >
                          {t('printers.tags.noColor')}
                        </button>
                      </div>
                    )}
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
                  onClick={() => {
                    setEditingId(tag.id);
                    setEditingName(tag.name);
                  }}
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

      <div className="flex gap-2">
        <input
          className="flex-1 px-3 py-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
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
