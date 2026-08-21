import { useState } from 'react';
import { Pencil, Trash2 } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../../api/client';
import { byLocationName } from '../../utils/locationOrder';

/**
 * Manage the places printers and sensors stand in.
 *
 * Deleting is refused while anything holds a location — printers, sensors,
 * queued work, or other locations — and the refusal is explained rather than
 * shown as a raw error: the queued items are the reason, since nulling their
 * target would send that work somewhere nobody chose.
 *
 * Moving a location to another parent is here for the same reason the cycle
 * check exists on the backend: without it a mistaken parent could only be
 * fixed by deleting, and deleting is refused while anything stands in the
 * place.
 */
export function PrinterLocationsCard() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [name, setName] = useState('');
  const [parentId, setParentId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editingName, setEditingName] = useState('');

  const { data } = useQuery({ queryKey: ['printer-locations'], queryFn: api.getPrinterLocations });
  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['printer-locations'] });
    // Printers carry the resolved name, so a rename or a removal has to reach
    // their cache too or the old name lingers until something else refetches.
    queryClient.invalidateQueries({ queryKey: ['printers'] });
  };

  const create = useMutation({
    mutationFn: () => api.createPrinterLocation(name.trim(), parentId),
    onSuccess: () => {
      setName('');
      setError(null);
      invalidate();
    },
    // The backend's own sentence: a cycle and a fourth level each say what is
    // wrong, and "name taken" would be a guess that is usually incorrect.
    onError: (e: Error) => setError(e.message || t('printers.locations.nameTaken')),
  });

  const move = useMutation({
    mutationFn: ({ id, parent_id }: { id: number; parent_id: number | null }) =>
      api.updatePrinterLocation(id, { parent_id }),
    onSuccess: () => {
      setError(null);
      invalidate();
    },
    onError: (e: Error) => setError(e.message),
  });

  /**
   * Rename in place.
   *
   * ⚠️ Sent only when the name actually changed and is not empty — the field
   * saves on blur, and a blur is how you leave a row you opened by mistake.
   * A PATCH there would be a write nobody asked for, and an empty one would be
   * refused by the backend in a sentence about a field the operator never
   * meant to touch.
   */
  const rename = useMutation({
    mutationFn: ({ id, name: next }: { id: number; name: string }) =>
      api.updatePrinterLocation(id, { name: next }),
    onSuccess: () => {
      setEditingId(null);
      setError(null);
      invalidate();
    },
    onError: (e: Error) => setError(e.message || t('printers.locations.nameTaken')),
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
    mutationFn: (id: number) => api.deletePrinterLocation(id),
    onSuccess: () => {
      setError(null);
      invalidate();
    },
    onError: () => setError(t('printers.locations.inUse')),
  });

  // By PATH, so a parent leads its own children. Sorted here rather than
  // trusted from the server: SQLite orders text by byte, which puts Ґ/Є/І/Ї
  // before А and every lowercase name last.
  const locations = [...(data?.locations ?? [])].sort(byLocationName((loc) => loc.path));

  return (
    // No heading and no card chrome of its own: the page wraps this in a Card
    // that already carries the title, the way ArchivedPrintersPanel is wrapped.
    // Keeping one here printed "Locations" twice, one line apart.
    <div>
      {locations.length === 0 && <p className="text-sm text-bambu-gray mb-3">{t('printers.locations.empty')}</p>}

      {/* ⚠️ One grid for the whole list, not a flex row per item.
          Every column here is content-sized — the name, the parent picker, the
          counts, Delete — and under `flex justify-between` each row sized its
          own, so a row whose parent reads "Top level" pushed its picker wider
          and further left than the row below it. Four ragged columns, and the
          list read as broken rather than as a table.
          Grid tracks are shared BETWEEN rows, which is the whole point: the
          picker column is as wide as the widest picker, once. `contents` on the
          `li` is what lets the cells participate in the parent's grid while the
          list keeps its markup. */}
      {/* ⚠️ The rule is drawn per CELL, not per row: `contents` means the `li`
          has no box of its own to put a border on. Painting the bottom edge of
          every cell gives one unbroken line across the row, and `last:*` cannot
          reach it — there are four last cells, one per column — so the final
          row is cleared by index instead. Same weight as Saved print profiles. */}
      <ul className="grid grid-cols-[minmax(0,1fr)_auto_auto_auto] items-center gap-x-3 mb-3">
        {locations.map((loc, index) => {
          // One rule under the whole row, drawn by every cell in it. The last
          // row does not get one — a line under the final entry reads as a
          // separator from the Add form below, which it is not.
          const cell = `py-2 min-w-0${index < locations.length - 1 ? ' border-b border-bambu-dark-tertiary' : ''}`;
          return (
          <li key={loc.id} className="contents">
            {/* Indented by depth: the tree is the point, and a flat list of
                paths repeats every parent on every row. */}
            {/* The indent lives on the CELL, so whatever sits inside it — the
                name or the rename field — simply fills what is left. Put on the
                field itself it was a margin outside a `w-full` box, i.e. the
                cell's width plus the indent, and the field slid under the
                picker. */}
            <div className={cell} style={{ paddingLeft: (loc.depth - 1) * 16 }}>
            {editingId === loc.id ? (
              // ⚠️ Enter commits, Escape abandons, and leaving the field
              // commits too — a rename you have finished typing and clicked
              // away from is finished, and losing it there is the behaviour
              // people complain about in every file manager that does it.
              <input
                autoFocus
                aria-label={`${t('common.edit')} ${loc.name}`}
                // ⚠️ No `w-full` here, deliberately. A grid item already
                // stretches to its track, and stretching subtracts the margin;
                // `w-full` does not — it is 100% of the track and the indent is
                // then added OUTSIDE it, so the field overflowed by exactly the
                // indent and slid under the parent picker.
                className="w-full min-w-0 bg-bambu-dark border border-bambu-green rounded-lg px-2 py-1.5 text-white text-sm focus:outline-none"
                value={editingName}
                onChange={(e) => setEditingName(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') commitRename(loc.id, loc.name);
                  if (e.key === 'Escape') setEditingId(null);
                }}
                onBlur={() => commitRename(loc.id, loc.name)}
              />
            ) : (
              <span className="block text-white truncate" title={loc.path}>
                {loc.name}
              </span>
            )}
            </div>

            {/* ⚠️ `aria-label`, not a sibling `<label class="sr-only">`.
                `contents` on the `li` promotes every child to a grid item, and
                a visually-hidden label is still a child — it would take a
                column of its own and push the row one cell to the right. The
                accessible name is identical either way. */}
            <div className={cell}>
            <select
              id={`parent-of-${loc.id}`}
              aria-label={`${t('printers.locations.parent')} ${loc.name}`}
              // `w-full` so every picker fills the shared column instead of
              // sitting at its own content width inside it — the track is
              // already as wide as the widest one.
              // Same shape as every other control in Settings — `rounded-lg`,
              // white text, green focus ring. It was a smaller, greyer,
              // squarer thing than the picker eight lines below it in this
              // very card.
              className="w-full text-sm px-2 py-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
              value={loc.parent_id ?? ''}
              onChange={(e) => move.mutate({ id: loc.id, parent_id: e.target.value ? Number(e.target.value) : null })}
            >
              <option value="">{t('printers.locations.noParent')}</option>
              {locations
                // Itself excluded; the backend refuses the deeper cases and its
                // sentence is what gets shown.
                .filter((candidate) => candidate.id !== loc.id)
                .map((candidate) => (
                  <option key={candidate.id} value={candidate.id} title={candidate.path}>
                    {' '.repeat((candidate.depth - 1) * 3)}
                    {candidate.name}
                  </option>
                ))}
            </select>
            </div>

            <div className={cell}>
            <span className="text-xs text-bambu-gray whitespace-nowrap">
              {t('printers.locations.counts', {
                printers: loc.printer_count,
                sensors: loc.sensor_count,
                queued: loc.queued_count,
              })}
            </span>
            </div>
            {/* ⚠️ One cell, two buttons — the row hands its cells to the list's
                grid via `contents`, so a second sibling here would take a
                column of its own and shift every row. Same icon pair and the
                same classes as Saved print profiles: this is the second list in
                Settings that offers rename-and-delete per row, and two
                different shapes for one pair of actions is how a settings page
                stops looking like one page. */}
            <div className={`${cell} flex items-center gap-1 justify-self-end`}>
              <button
                type="button"
                onClick={() => {
                  setEditingId(loc.id);
                  setEditingName(loc.name);
                }}
                className="p-1.5 text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary rounded"
                title={t('common.edit')}
              >
                <Pencil className="w-4 h-4" />
              </button>
              <button
                type="button"
                onClick={() => remove.mutate(loc.id)}
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
          placeholder={t('printers.modal.locationPlaceholder')}
        />
        <label className="sr-only" htmlFor="new-location-parent">
          {t('printers.locations.parent')}
        </label>
        <select
          id="new-location-parent"
          className="px-2 py-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
          value={parentId ?? ''}
          onChange={(e) => setParentId(e.target.value ? Number(e.target.value) : null)}
        >
          <option value="">{t('printers.locations.noParent')}</option>
          {locations.map((loc) => (
            <option key={loc.id} value={loc.id} title={loc.path}>
              {' '.repeat((loc.depth - 1) * 3)}
              {loc.name}
            </option>
          ))}
        </select>
        <button
          type="button"
          className="px-3 py-1.5 bg-bambu-green rounded-lg text-white disabled:opacity-50"
          disabled={!name.trim() || create.isPending}
          onClick={() => create.mutate()}
        >
          {t('printers.locations.add')}
        </button>
      </div>

      {error && <p className="text-sm text-status-error mt-2">{error}</p>}
    </div>
  );
}
