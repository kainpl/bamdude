/**
 * Draw your own page of stickers: the paper, the grid, the margins and the gaps.
 *
 * ⚠️ **A sheet is paper, not a design.** It states a cell size and nothing about
 * what goes in one — printing takes a sheet plus a design that fits the cell.
 * The tempting shape, "this sheet holds that label", makes the design
 * undeletable while a sheet looks at it and welds one paper geometry to one
 * layout forever.
 *
 * ⚠️ **The picture is a server render, like the design editor's.** A grid drawn
 * in the browser would be a second implementation of the layout, and the two
 * would disagree — here, about the thing you came to check.
 */

import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Copy, Plus, Trash2 } from 'lucide-react';

import { api, type LabelPageSize, type LabelSheet, type LabelSheetInput } from '../../api/client';
import { Button } from '../Button';
import { LoadingBlock } from '../LoadingBlock';
import { useToast } from '../../contexts/ToastContext';
import { useAuth } from '../../contexts/AuthContext';

const PAGE_SIZES: LabelPageSize[] = ['A4', 'A5', 'letter'];

/** Avery 5160 without the Avery — a starting point that fits A4. */
const NEW_SHEET: Omit<LabelSheetInput, 'name'> = {
  page_size: 'A4',
  cell_width_mm: 63.5,
  cell_height_mm: 38.1,
  cols: 3,
  rows: 7,
  margin_top_mm: 15,
  margin_left_mm: 7,
  gap_x_mm: 2.5,
  gap_y_mm: 0,
};

const asInput = (sheet: LabelSheet): LabelSheetInput => ({
  name: sheet.name,
  page_size: sheet.page_size,
  cell_width_mm: sheet.cell_width_mm,
  cell_height_mm: sheet.cell_height_mm,
  cols: sheet.cols,
  rows: sheet.rows,
  margin_top_mm: sheet.margin_top_mm,
  margin_left_mm: sheet.margin_left_mm,
  gap_x_mm: sheet.gap_x_mm,
  gap_y_mm: sheet.gap_y_mm,
});

/** The numeric fields, in the order they are asked about. */
const FIELDS: Array<{ key: keyof LabelSheetInput; step: number; integer?: boolean }> = [
  { key: 'cols', step: 1, integer: true },
  { key: 'rows', step: 1, integer: true },
  { key: 'cell_width_mm', step: 0.1 },
  { key: 'cell_height_mm', step: 0.1 },
  { key: 'margin_left_mm', step: 0.1 },
  { key: 'margin_top_mm', step: 0.1 },
  { key: 'gap_x_mm', step: 0.1 },
  { key: 'gap_y_mm', step: 0.1 },
];

export function LabelSheetEditor() {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const { hasPermission } = useAuth();
  const queryClient = useQueryClient();

  const canWrite = hasPermission('label_templates:write');
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [draft, setDraft] = useState<LabelSheetInput | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [previewWarnings, setPreviewWarnings] = useState<string[]>([]);
  const [designId, setDesignId] = useState<number | null>(null);

  const { data: sheets, isLoading } = useQuery({ queryKey: ['label-sheets'], queryFn: api.getLabelSheets });
  const { data: designs } = useQuery({ queryKey: ['label-templates'], queryFn: api.getLabelTemplates });

  const selected = useMemo(
    () => sheets?.find((row) => row.id === selectedId) ?? null,
    [sheets, selectedId],
  );
  const readOnly = !canWrite || (selected?.is_builtin ?? false);

  // Follow the selection, and pick something on first load so the panel is
  // never an empty frame with controls that act on nothing.
  useEffect(() => {
    if (!sheets?.length) return;
    if (selectedId === null) setSelectedId(sheets[0].id);
  }, [sheets, selectedId]);

  useEffect(() => {
    setDraft(selected ? asInput(selected) : null);
  }, [selected]);

  useEffect(() => {
    if (designId === null && designs?.length) setDesignId(designs[0].id);
  }, [designs, designId]);

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['label-sheets'] });

  const save = useMutation({
    mutationFn: () => api.updateLabelSheet(selectedId!, draft!),
    onSuccess: () => {
      showToast(t('labelSheets.saved'));
      invalidate();
    },
    onError: (error: Error) => showToast(error.message, 'error'),
  });

  const create = useMutation({
    mutationFn: () => api.createLabelSheet({ name: t('labelSheets.newName'), ...NEW_SHEET }),
    onSuccess: (row) => {
      setSelectedId(row.id);
      invalidate();
    },
    onError: (error: Error) => showToast(error.message, 'error'),
  });

  const duplicate = useMutation({
    mutationFn: () => api.duplicateLabelSheet(selectedId!),
    onSuccess: (row) => {
      setSelectedId(row.id);
      invalidate();
    },
    onError: (error: Error) => showToast(error.message, 'error'),
  });

  const remove = useMutation({
    mutationFn: () => api.deleteLabelSheet(selectedId!),
    onSuccess: () => {
      setSelectedId(null);
      invalidate();
    },
    onError: (error: Error) => showToast(error.message, 'error'),
  });

  /**
   * ⚠️ On demand, not on every keystroke. A page render is a whole PDF, and the
   * design editor's live preview is affordable only because it draws one label.
   */
  const preview = useMutation({
    mutationFn: () => api.previewLabelSheet({ sheet: draft!, template_id: designId! }),
    onSuccess: ({ blob, warnings }) => {
      setPreviewUrl((old) => {
        if (old) URL.revokeObjectURL(old);
        return URL.createObjectURL(blob);
      });
      setPreviewWarnings(warnings);
    },
    onError: (error: Error) => showToast(error.message, 'error'),
  });

  // The object URL outlives the component otherwise — one leaked page render
  // per visit, and a PDF is not small.
  useEffect(() => () => { if (previewUrl) URL.revokeObjectURL(previewUrl); }, [previewUrl]);

  if (isLoading) return <LoadingBlock label={t('common.loading')} className="py-12 text-bambu-gray" />;

  const set = (key: keyof LabelSheetInput, value: string) => {
    if (!draft) return;
    const numeric = Number(value);
    setDraft({ ...draft, [key]: Number.isFinite(numeric) ? numeric : 0 });
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3 className="text-base font-semibold text-white">{t('labelSheets.title')}</h3>
          <p className="text-xs text-bambu-gray">{t('labelSheets.subtitle')}</p>
        </div>
        <div className="flex items-center gap-2">
          <Button size="sm" variant="secondary" disabled={!canWrite} onClick={() => create.mutate()}>
            <Plus className="w-4 h-4" />
            {t('labelSheets.new')}
          </Button>
          <Button size="sm" variant="secondary" disabled={!canWrite || !selectedId} onClick={() => duplicate.mutate()}>
            <Copy className="w-4 h-4" />
            {t('common.duplicate')}
          </Button>
          <Button
            size="sm"
            variant="secondary"
            disabled={readOnly || !selectedId}
            onClick={() => remove.mutate()}
          >
            <Trash2 className="w-4 h-4" />
            {t('common.delete')}
          </Button>
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-[16rem_1fr] gap-4">
        <ul className="space-y-1">
          {(sheets ?? []).map((row) => (
            <li key={row.id}>
              <button
                type="button"
                onClick={() => setSelectedId(row.id)}
                className={`w-full text-left px-3 py-2 rounded-lg text-sm transition-colors ${
                  row.id === selectedId ? 'bg-bambu-green/20 text-bambu-green' : 'text-bambu-gray hover:bg-bambu-dark-tertiary'
                }`}
              >
                <span className="block truncate">{row.name}</span>
                <span className="block text-xs opacity-70">
                  {row.page_size} · {row.cols}×{row.rows}
                  {row.is_builtin ? ` · ${t('labelSheets.builtin')}` : ''}
                </span>
                {/* A geometry can stop fitting without anyone editing it, so the
                    list says so rather than the editor alone. */}
                {row.overflow.length > 0 && (
                  <span className="block text-xs text-amber-600 dark:text-amber-400">{t('labelSheets.doesNotFit')}</span>
                )}
              </button>
            </li>
          ))}
        </ul>

        {draft && (
          <div className="space-y-3">
            <label className="block text-xs text-bambu-gray">
              {t('labelSheets.name')}
              <input
                value={draft.name}
                disabled={readOnly}
                onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                className="mt-1 w-full px-2 py-1.5 text-sm bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
              />
            </label>

            <label className="block text-xs text-bambu-gray">
              {t('labelSheets.pageSize')}
              <select
                value={draft.page_size}
                disabled={readOnly}
                onChange={(e) => setDraft({ ...draft, page_size: e.target.value as LabelPageSize })}
                className="mt-1 w-full px-2 py-1.5 text-sm bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
              >
                {PAGE_SIZES.map((size) => (
                  <option key={size} value={size}>{t(`labelSheets.page.${size}`)}</option>
                ))}
              </select>
            </label>

            <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
              {FIELDS.map(({ key, step, integer }) => (
                <label key={key} className="block text-xs text-bambu-gray">
                  {t(`labelSheets.field.${key}`)}
                  <input
                    type="number"
                    step={step}
                    min={integer ? 1 : 0}
                    value={String(draft[key])}
                    disabled={readOnly}
                    onChange={(e) => set(key, e.target.value)}
                    className="mt-1 w-full px-2 py-1.5 text-sm bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
                  />
                </label>
              ))}
            </div>

            {selected?.is_builtin && (
              <p className="text-xs text-bambu-gray">{t('labelSheets.builtinHint')}</p>
            )}
            {selected && selected.overflow.length > 0 && (
              <ul className="text-xs text-amber-600 dark:text-amber-400 space-y-1">
                {selected.overflow.map((line) => <li key={line}>{line}</li>)}
              </ul>
            )}

            <div className="flex flex-wrap items-center gap-2">
              <Button size="sm" disabled={readOnly || save.isPending} onClick={() => save.mutate()}>
                {t('common.save')}
              </Button>
              <select
                value={designId ?? ''}
                onChange={(e) => setDesignId(Number(e.target.value))}
                className="px-2 py-1.5 text-sm bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
              >
                {(designs ?? []).map((row) => (
                  <option key={row.id} value={row.id}>{row.name}</option>
                ))}
              </select>
              <Button
                size="sm"
                variant="secondary"
                disabled={!designId || preview.isPending}
                onClick={() => preview.mutate()}
              >
                {t('labelSheets.preview')}
              </Button>
            </div>

            {previewWarnings.length > 0 && (
              <ul className="text-xs text-amber-600 dark:text-amber-400 space-y-1">
                {previewWarnings.map((line) => <li key={line}>{line}</li>)}
              </ul>
            )}
            {previewUrl && (
              <iframe
                title={t('labelSheets.preview')}
                src={previewUrl}
                className="w-full h-[28rem] rounded-lg border border-bambu-dark-tertiary bg-white"
              />
            )}
          </div>
        )}
      </div>
    </div>
  );
}
