/**
 * Designing a label with the mouse.
 *
 * Three columns: what exists, what it looks like, and what the selected box
 * says. The middle one is a picture the server rendered — see LabelCanvas for
 * why the browser is not allowed to draw it.
 *
 * Lives inside Settings → Filament → Marking rather than as a page of its own:
 * a label is a thing you print about a spool, and it belongs beside the spools
 * rather than beside the printers.
 *
 * ⚠️ **A built-in is read-only.** Its name is a contract the print API accepts,
 * so an automation must not start printing something else because somebody
 * dragged a box. Opening one shows the design and offers Duplicate; the API
 * would refuse a save anyway, and finding that out at the save button after
 * twenty minutes of work would be worse.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  AlignCenter,
  AlignEndHorizontal,
  AlignStartHorizontal,
  Copy,
  Loader2,
  Lock,
  Plus,
  Printer,
  Redo2,
  Save,
  Trash2,
  Undo2,
} from 'lucide-react';
import {
  api,
  type LabelDevice,
  type LabelTemplate,
  type LabelTemplateElement,
  type LabelTemplateInput,
} from '../../api/client';
import { useToast } from '../../contexts/ToastContext';
import { Button } from '../Button';
import { Card, CardContent, CardHeader } from '../Card';
import { LabelCanvas } from './LabelCanvas';
import { ElementInspector } from './ElementInspector';
import {
  alignBox,
  boxOf,
  clampToLabel,
  NUDGE_COARSE_MM,
  NUDGE_MM,
  newElement,
  roundMm,
  type Alignment,
  type Box,
} from './labelGeometry';

/** How long the editor waits after a change before asking the server to draw it.
 *  ⚠️ Long enough that typing a name is one render rather than fifteen; short
 *  enough that dragging a box still feels answered. */
const PREVIEW_DEBOUNCE_MS = 350;

/** What the picture is rendered at. 8 dots/mm is 203 dpi — the device the
 *  faults worth seeing (a QR whose modules merge) actually appear on. */
const PREVIEW_DOTS_PER_MM = 8;

const asInput = (template: LabelTemplate): LabelTemplateInput => ({
  name: template.name,
  width_mm: template.width_mm,
  height_mm: template.height_mm,
  shape: template.shape,
  elements: template.elements,
});

export function LabelTemplateEditor() {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const qc = useQueryClient();

  const { data: templates, isLoading } = useQuery({
    queryKey: ['label-templates'],
    queryFn: api.getLabelTemplates,
  });

  const [openId, setOpenId] = useState<number | null>(null);
  const [testDeviceId, setTestDeviceId] = useState<number | null>(null);
  const [draft, setDraft] = useState<LabelTemplateInput | null>(null);
  const [selected, setSelected] = useState<number | null>(null);

  // ⚠️ Whole states, not diffs. A template is a few kilobytes and the history
  // is bounded; a diff engine here would be machinery in service of nothing.
  const [history, setHistory] = useState<LabelTemplateInput[]>([]);
  const [future, setFuture] = useState<LabelTemplateInput[]>([]);

  const open = templates?.find((row) => row.id === openId) ?? null;
  const readOnly = open?.is_builtin ?? false;

  // Seed the draft when a different template is opened. Deliberately not
  // reactive to `templates` afterwards: a background refetch must not throw
  // away what somebody is in the middle of drawing.
  useEffect(() => {
    if (!open) return;
    setDraft(asInput(open));
    setSelected(null);
    setHistory([]);
    setFuture([]);
  }, [openId]); // eslint-disable-line react-hooks/exhaustive-deps

  const commit = useCallback(
    (next: LabelTemplateInput) => {
      setDraft((current) => {
        if (current) setHistory((past) => [...past.slice(-49), current]);
        return next;
      });
      setFuture([]);
    },
    [],
  );

  const undo = useCallback(() => {
    setHistory((past) => {
      if (past.length === 0) return past;
      const previous = past[past.length - 1];
      setDraft((current) => {
        if (current) setFuture((ahead) => [current, ...ahead]);
        return previous;
      });
      return past.slice(0, -1);
    });
  }, []);

  const redo = useCallback(() => {
    setFuture((ahead) => {
      if (ahead.length === 0) return ahead;
      const [next, ...rest] = ahead;
      setDraft((current) => {
        if (current) setHistory((past) => [...past, current]);
        return next;
      });
      return rest;
    });
  }, []);

  // ── The preview ────────────────────────────────────────────────────────────

  const [previewUrl, setPreviewUrl] = useState<string | undefined>();
  const [warnings, setWarnings] = useState<string[]>([]);
  const urlRef = useRef<string | undefined>(undefined);

  useEffect(() => {
    if (!draft) return;
    let cancelled = false;
    const timer = window.setTimeout(async () => {
      try {
        const { blob, warnings: found } = await api.previewLabelTemplate({
          template: draft,
          dots_per_mm: PREVIEW_DOTS_PER_MM,
        });
        if (cancelled) return;
        const url = URL.createObjectURL(blob);
        // ⚠️ Revoke the one this replaces. A preview is redrawn on every edit,
        // so keeping them would hold one PNG per keystroke for the life of the
        // tab.
        if (urlRef.current) URL.revokeObjectURL(urlRef.current);
        urlRef.current = url;
        setPreviewUrl(url);
        setWarnings(found);
      } catch (error) {
        if (!cancelled) setWarnings([error instanceof Error ? error.message : String(error)]);
      }
    }, PREVIEW_DEBOUNCE_MS);

    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [draft]);

  useEffect(
    () => () => {
      if (urlRef.current) URL.revokeObjectURL(urlRef.current);
    },
    [],
  );

  // ── Geometry helpers ───────────────────────────────────────────────────────

  const scale = useMemo(() => {
    if (!draft) return 8;
    // Fill the panel without exceeding it, and never magnify past 16 px/mm —
    // beyond that the backdrop is visibly interpolated and reads as blur.
    const usable = 520;
    return Math.min(16, Math.max(4, usable / draft.width_mm));
  }, [draft]);

  const setBox = (index: number, box: Box) => {
    if (!draft || readOnly) return;
    const elements = draft.elements.map((element, i) =>
      i === index ? ({ ...element, ...box } as LabelTemplateElement) : element,
    );
    commit({ ...draft, elements });
  };

  const setElement = (index: number, next: LabelTemplateElement) => {
    if (!draft || readOnly) return;
    commit({ ...draft, elements: draft.elements.map((element, i) => (i === index ? next : element)) });
  };

  const addElement = (type: LabelTemplateElement['type']) => {
    if (!draft || readOnly) return;
    const element = newElement(type, draft.width_mm, draft.height_mm);
    commit({ ...draft, elements: [...draft.elements, element] });
    setSelected(draft.elements.length);
  };

  const removeElement = (index: number) => {
    if (!draft || readOnly) return;
    commit({ ...draft, elements: draft.elements.filter((_, i) => i !== index) });
    setSelected(null);
  };

  const align = (how: Alignment) => {
    if (!draft || readOnly || selected === null) return;
    const element = draft.elements[selected];
    setBox(selected, alignBox(boxOf(element), how, draft.width_mm, draft.height_mm));
  };

  // ⚠️ A canvas has no focus and cannot receive key events, so the keyboard
  // lives on the window while a template is open.
  useEffect(() => {
    if (!draft || readOnly) return;
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      // Never steal a keystroke from a field somebody is typing in.
      if (target && ['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName)) return;

      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'z') {
        event.preventDefault();
        if (event.shiftKey) redo();
        else undo();
        return;
      }
      if (selected === null) return;

      const step = event.shiftKey ? NUDGE_COARSE_MM : NUDGE_MM;
      const element = draft.elements[selected];
      const box = boxOf(element);
      const moved: Record<string, Box> = {
        ArrowLeft: { ...box, x_mm: roundMm(box.x_mm - step) },
        ArrowRight: { ...box, x_mm: roundMm(box.x_mm + step) },
        ArrowUp: { ...box, y_mm: roundMm(box.y_mm - step) },
        ArrowDown: { ...box, y_mm: roundMm(box.y_mm + step) },
      };
      if (moved[event.key]) {
        event.preventDefault();
        setBox(selected, clampToLabel(moved[event.key], draft.width_mm, draft.height_mm));
      } else if (event.key === 'Delete' || event.key === 'Backspace') {
        event.preventDefault();
        removeElement(selected);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [draft, readOnly, selected, undo, redo]); // eslint-disable-line react-hooks/exhaustive-deps

  // ── Mutations ──────────────────────────────────────────────────────────────

  const invalidate = () => qc.invalidateQueries({ queryKey: ['label-templates'] });

  const save = useMutation({
    mutationFn: () => api.updateLabelTemplate(openId as number, draft as LabelTemplateInput),
    onSuccess: () => {
      invalidate();
      setHistory([]);
      setFuture([]);
      showToast(t('labelEditor.saved'), 'success');
    },
    onError: (error: Error) => showToast(error.message, 'error'),
  });

  const duplicate = useMutation({
    mutationFn: (id: number) => api.duplicateLabelTemplate(id),
    onSuccess: (copy) => {
      invalidate();
      setOpenId(copy.id);
      showToast(t('labelEditor.duplicated'), 'success');
    },
    onError: (error: Error) => showToast(error.message, 'error'),
  });

  const create = useMutation({
    mutationFn: () =>
      api.createLabelTemplate({
        name: t('labelEditor.newName'),
        width_mm: 50,
        height_mm: 30,
        shape: 'rect',
        elements: [newElement('text', 50, 30)],
      }),
    onSuccess: (row) => {
      invalidate();
      setOpenId(row.id);
    },
    onError: (error: Error) => showToast(error.message, 'error'),
  });

  const remove = useMutation({
    mutationFn: (id: number) => api.deleteLabelTemplate(id),
    onSuccess: () => {
      invalidate();
      setOpenId(null);
      setDraft(null);
    },
    onError: (error: Error) => showToast(error.message, 'error'),
  });

  // Only adopted devices, and only when the subsystem is on — offering a test
  // print with nowhere to send it is a button that can only disappoint.
  const { data: settings } = useQuery({ queryKey: ['settings'], queryFn: api.getSettings });
  const { data: allDevices } = useQuery({
    queryKey: ['label-devices'],
    queryFn: api.getLabelDevices,
    enabled: Boolean(settings?.device_labels_enabled),
  });
  const devices = (allDevices ?? []).filter((device: LabelDevice) => device.enabled);
  const targetId = testDeviceId ?? devices[0]?.id ?? null;

  const testPrint = useMutation({
    mutationFn: () =>
      api.testPrintLabelTemplate({ device_id: targetId as number, template: draft as LabelTemplateInput }),
    onSuccess: (result) => {
      qc.invalidateQueries({ queryKey: ['label-jobs'] });
      // ⚠️ The server's complaints are shown even on success. A label that
      // printed with a barcode missing is not a failure the queue records, and
      // it is the whole reason somebody pressed this.
      showToast(
        result.warnings.length > 0 ? result.warnings.join(' · ') : t('labelEditor.testPrintQueued'),
        result.warnings.length > 0 ? 'error' : 'success',
      );
    },
    onError: (error: Error) => showToast(error.message, 'error'),
  });

  const dirty = Boolean(draft && open && JSON.stringify(draft) !== JSON.stringify(asInput(open)));

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-base font-semibold text-white">{t('labelEditor.title')}</h2>
        <Button onClick={() => create.mutate()} disabled={create.isPending}>
          <Plus className="w-4 h-4" />
          {t('labelEditor.newTemplate')}
        </Button>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-[220px_1fr_280px] gap-4 items-start">
        <Card>
          <CardHeader>
            <h2 className="text-sm font-semibold text-white">{t('labelEditor.designs')}</h2>
          </CardHeader>
          <CardContent className="space-y-1">
            {isLoading && <p className="text-sm text-bambu-gray">{t('common.loading')}</p>}
            {(templates ?? []).map((row: LabelTemplate) => (
              <button
                key={row.id}
                type="button"
                onClick={() => setOpenId(row.id)}
                className={`w-full text-left px-2 py-1.5 rounded text-sm flex items-center gap-2 ${
                  row.id === openId ? 'bg-bambu-green/15 text-white' : 'text-bambu-gray hover:bg-bambu-dark'
                }`}
              >
                {row.is_builtin && <Lock className="w-3 h-3 shrink-0" />}
                <span className="truncate">{row.name}</span>
                <span className="ml-auto text-xs shrink-0">
                  {row.width_mm}×{row.height_mm}
                </span>
              </button>
            ))}
          </CardContent>
        </Card>

        <Card>
          <CardContent className="space-y-3">
            {!draft && <p className="text-sm text-bambu-gray">{t('labelEditor.pickOne')}</p>}

            {draft && (
              <>
                {readOnly && (
                  <div className="flex items-center gap-2 p-2 rounded bg-amber-500/10 text-amber-600 dark:text-amber-400 text-xs">
                    <Lock className="w-3.5 h-3.5 shrink-0" />
                    <span>{t('labelEditor.builtinHint')}</span>
                  </div>
                )}

                <div className="flex flex-wrap items-center gap-2">
                  <input
                    value={draft.name}
                    disabled={readOnly}
                    onChange={(e) => commit({ ...draft, name: e.target.value })}
                    className="flex-1 min-w-40 px-2 py-1 text-sm bg-bambu-dark border border-bambu-dark-tertiary rounded"
                  />
                  {(['text', 'qr', 'barcode', 'swatch'] as const).map((type) => (
                    <Button key={type} variant="secondary" size="sm" disabled={readOnly} onClick={() => addElement(type)}>
                      {t(`labelEditor.elementType.${type}`)}
                    </Button>
                  ))}
                </div>

                <div className="flex flex-wrap items-center gap-1">
                  <Button variant="secondary" size="sm" disabled={history.length === 0} onClick={undo}>
                    <Undo2 className="w-4 h-4" />
                  </Button>
                  <Button variant="secondary" size="sm" disabled={future.length === 0} onClick={redo}>
                    <Redo2 className="w-4 h-4" />
                  </Button>
                  <span className="w-px h-5 bg-bambu-dark-tertiary mx-1" />
                  <Button variant="secondary" size="sm" disabled={readOnly || selected === null} onClick={() => align('left')}>
                    <AlignStartHorizontal className="w-4 h-4 rotate-90" />
                  </Button>
                  <Button variant="secondary" size="sm" disabled={readOnly || selected === null} onClick={() => align('hcenter')}>
                    <AlignCenter className="w-4 h-4" />
                  </Button>
                  <Button variant="secondary" size="sm" disabled={readOnly || selected === null} onClick={() => align('right')}>
                    <AlignEndHorizontal className="w-4 h-4 rotate-90" />
                  </Button>
                  <Button variant="secondary" size="sm" disabled={readOnly || selected === null} onClick={() => align('vcenter')}>
                    <AlignCenter className="w-4 h-4 rotate-90" />
                  </Button>
                </div>

                <div className="inline-block border border-bambu-dark-tertiary rounded overflow-auto max-w-full">
                  <LabelCanvas
                    widthMm={draft.width_mm}
                    heightMm={draft.height_mm}
                    elements={draft.elements}
                    selected={selected}
                    onSelect={setSelected}
                    onChange={setBox}
                    previewUrl={previewUrl}
                    scale={scale}
                  />
                </div>

                {warnings.length > 0 && (
                  <ul className="text-xs text-amber-600 dark:text-amber-400 space-y-0.5">
                    {warnings.map((warning, index) => (
                      <li key={index}>{warning}</li>
                    ))}
                  </ul>
                )}

                <div className="flex flex-wrap items-center gap-2">
                  <Button onClick={() => save.mutate()} disabled={readOnly || !dirty || save.isPending}>
                    {save.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
                    {t('common.save')}
                  </Button>
                  <Button variant="secondary" onClick={() => openId && duplicate.mutate(openId)} disabled={duplicate.isPending}>
                    <Copy className="w-4 h-4" />
                    {t('labelEditor.duplicate')}
                  </Button>
                  <Button
                    variant="secondary"
                    onClick={() => openId && remove.mutate(openId)}
                    disabled={readOnly || remove.isPending}
                  >
                    <Trash2 className="w-4 h-4 text-red-600 dark:text-red-400" />
                  </Button>
                  {/* ⚠️ Works on an UNSAVED design and on a built-in alike. The
                      point is to check before committing, and a built-in is
                      exactly what somebody duplicates after seeing it come out
                      wrong on their stock. */}
                  {devices.length > 0 && (
                    <>
                      {devices.length > 1 && (
                        <select
                          value={targetId ?? ''}
                          onChange={(e) => setTestDeviceId(Number(e.target.value))}
                          className="px-2 py-1 text-sm bg-bambu-dark border border-bambu-dark-tertiary rounded"
                        >
                          {devices.map((device: LabelDevice) => (
                            <option key={device.id} value={device.id}>
                              {device.name || device.model || device.installation_id}
                            </option>
                          ))}
                        </select>
                      )}
                      <Button
                        variant="secondary"
                        onClick={() => testPrint.mutate()}
                        disabled={targetId === null || testPrint.isPending}
                      >
                        {testPrint.isPending ? (
                          <Loader2 className="w-4 h-4 animate-spin" />
                        ) : (
                          <Printer className="w-4 h-4" />
                        )}
                        {t('labelEditor.testPrint')}
                      </Button>
                    </>
                  )}
                  {dirty && <span className="text-xs text-bambu-gray">{t('labelEditor.unsaved')}</span>}
                </div>
              </>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <h2 className="text-sm font-semibold text-white">{t('labelEditor.element')}</h2>
          </CardHeader>
          <CardContent>
            {draft && selected !== null && draft.elements[selected] ? (
              <ElementInspector
                element={draft.elements[selected]}
                widthMm={draft.width_mm}
                heightMm={draft.height_mm}
                disabled={readOnly}
                onChange={(next) => setElement(selected, next)}
                onDelete={() => removeElement(selected)}
              />
            ) : (
              <p className="text-sm text-bambu-gray">{t('labelEditor.pickElement')}</p>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
