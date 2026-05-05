/**
 * Settings → Print → Saved Print Options Profiles.
 *
 * Cross-user view of every saved (user, printer-model) preference row
 * created via the PrintModal toggles. Lets an admin add / edit / delete
 * any user's preference and copy a preference between users — handy when
 * onboarding a new operator who should inherit the same defaults as the
 * lead operator already calibrated.
 *
 * Lives inside an existing Card so it doesn't draw its own page chrome.
 */
import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Plus, Trash2, Pencil, Copy } from 'lucide-react';
import {
  api,
  type Printer,
  type PrintOptionsPreferenceAdminEntry,
  type PrintOptionsPreferenceData,
  type UserResponse,
} from '../../api/client';
import { useToast } from '../../contexts/ToastContext';

const DEFAULT_PRINT_OPTIONS: PrintOptionsPreferenceData = {
  print_options: {
    bed_levelling: true,
    flow_cali: true,
    layer_inspect: false,
    timelapse: false,
    mesh_mode_fast_check: true,
    gcode_injection: false,
  },
  swap_macros: {
    execute: true,
    events: ['swap_mode_start', 'swap_mode_change_table'],
  },
};

type DialogMode =
  | { kind: 'closed' }
  | { kind: 'edit'; entry: PrintOptionsPreferenceAdminEntry }
  | { kind: 'add' }
  | { kind: 'copy'; src: PrintOptionsPreferenceAdminEntry };

function formatDate(iso: string): string {
  return new Date(iso).toLocaleString();
}

// Swap-mode plate macros only make sense on the A1 family — the other
// printers don't have an external plate swapper. Anywhere else we force
// the swap_macros payload off so a stale "execute=true" can't slip in
// when an admin edits a P1S/X1C/H2D row.
const SWAP_ELIGIBLE_MODELS = new Set(['A1', 'A1 Mini']);
function isSwapEligibleModel(model: string): boolean {
  return SWAP_ELIGIBLE_MODELS.has(model);
}

export function PrintOptionsPreferencesPanel() {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();

  const [dialog, setDialog] = useState<DialogMode>({ kind: 'closed' });

  const { data: entries, isLoading: loadingEntries } = useQuery({
    queryKey: ['print-options-preferences-admin'],
    queryFn: api.listAllPrintOptionsPreferences,
  });

  const { data: users } = useQuery({
    queryKey: ['users'],
    queryFn: api.getUsers,
  });

  const { data: printers } = useQuery({
    queryKey: ['printers'],
    queryFn: api.getPrinters,
  });

  const availableModels = useMemo(() => {
    if (!printers) return [];
    return [...new Set(printers.map((p: Printer) => p.model).filter((m): m is string => !!m))].sort();
  }, [printers]);

  const deleteMutation = useMutation({
    mutationFn: ({ userId, model }: { userId: number; model: string }) =>
      api.adminDeletePrintOptionsPreference(userId, model),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['print-options-preferences-admin'] });
      // Also invalidate per-user query keys so an open PrintModal re-fetches.
      queryClient.invalidateQueries({ queryKey: ['print-options-preference'] });
      showToast(t('printOptionsPrefs.toast.deleted'));
    },
    onError: (err: Error) => {
      showToast(err.message || t('printOptionsPrefs.toast.deleteFailed'), 'error');
    },
  });

  const sortedEntries = useMemo(() => {
    if (!entries) return [];
    return [...entries].sort((a, b) => {
      if (a.username !== b.username) return a.username.localeCompare(b.username);
      return a.printer_model.localeCompare(b.printer_model);
    });
  }, [entries]);

  const handleDelete = (entry: PrintOptionsPreferenceAdminEntry) => {
    if (
      !window.confirm(
        t('printOptionsPrefs.confirmDelete', { user: entry.username, model: entry.printer_model }),
      )
    ) {
      return;
    }
    deleteMutation.mutate({ userId: entry.user_id, model: entry.printer_model });
  };

  return (
    <>
      <div className="flex items-center justify-between mb-3">
        <p className="text-xs text-bambu-gray flex-1 mr-3">
          {t('printOptionsPrefs.description')}
        </p>
        <button
          type="button"
          onClick={() => setDialog({ kind: 'add' })}
          className="px-3 py-1.5 bg-bambu-green hover:bg-bambu-green-dark text-black text-sm font-medium rounded-lg flex items-center gap-1.5 flex-shrink-0"
        >
          <Plus className="w-4 h-4" />
          {t('printOptionsPrefs.add')}
        </button>
      </div>

      {loadingEntries ? (
        <p className="text-sm text-bambu-gray">{t('common.loading')}</p>
      ) : sortedEntries.length === 0 ? (
        <p className="text-sm text-bambu-gray italic">{t('printOptionsPrefs.empty')}</p>
      ) : (
        <div className="overflow-x-auto -mx-4">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-xs text-bambu-gray uppercase border-b border-bambu-dark-tertiary">
                <th className="px-3 py-2 text-left">{t('printOptionsPrefs.col.user')}</th>
                <th className="px-3 py-2 text-left">{t('printOptionsPrefs.col.model')}</th>
                <th className="px-3 py-2 text-left">{t('printOptionsPrefs.col.toggles')}</th>
                <th className="px-3 py-2 text-left">{t('printOptionsPrefs.col.updated')}</th>
                <th className="px-3 py-2 text-right">{t('printOptionsPrefs.col.actions')}</th>
              </tr>
            </thead>
            <tbody>
              {sortedEntries.map((entry) => (
                <tr
                  key={`${entry.user_id}-${entry.printer_model}`}
                  className="border-b border-bambu-dark-tertiary hover:bg-bambu-dark-secondary/50"
                >
                  <td className="px-3 py-2 text-white">{entry.username}</td>
                  <td className="px-3 py-2 text-white">{entry.printer_model}</td>
                  <td className="px-3 py-2 text-bambu-gray text-xs">
                    {summariseOptions(entry.options, t)}
                  </td>
                  <td className="px-3 py-2 text-bambu-gray text-xs">
                    {formatDate(entry.updated_at)}
                  </td>
                  <td className="px-3 py-2 text-right whitespace-nowrap">
                    <button
                      type="button"
                      onClick={() => setDialog({ kind: 'edit', entry })}
                      className="p-1.5 text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary rounded"
                      title={t('common.edit')}
                    >
                      <Pencil className="w-4 h-4" />
                    </button>
                    <button
                      type="button"
                      onClick={() => setDialog({ kind: 'copy', src: entry })}
                      className="p-1.5 text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary rounded"
                      title={t('printOptionsPrefs.copyToUser')}
                    >
                      <Copy className="w-4 h-4" />
                    </button>
                    <button
                      type="button"
                      onClick={() => handleDelete(entry)}
                      className="p-1.5 text-bambu-gray hover:text-red-400 hover:bg-bambu-dark-tertiary rounded"
                      title={t('common.delete')}
                    >
                      <Trash2 className="w-4 h-4" />
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {(dialog.kind === 'edit' || dialog.kind === 'add') && users && (
        <EditDialog
          mode={dialog.kind}
          existingEntries={sortedEntries}
          users={users}
          availableModels={availableModels}
          initialEntry={dialog.kind === 'edit' ? dialog.entry : null}
          onClose={() => setDialog({ kind: 'closed' })}
        />
      )}

      {dialog.kind === 'copy' && users && (
        <CopyDialog
          src={dialog.src}
          users={users}
          availableModels={availableModels}
          onClose={() => setDialog({ kind: 'closed' })}
        />
      )}
    </>
  );
}

function summariseOptions(
  data: PrintOptionsPreferenceData,
  t: (k: string) => string,
): string {
  const enabled: string[] = [];
  if (data.print_options.bed_levelling) enabled.push(t('printModal.bedLeveling'));
  if (data.print_options.flow_cali) enabled.push(t('printModal.flowCalibration'));
  if (data.print_options.layer_inspect) enabled.push(t('printModal.layerInspection'));
  if (data.print_options.timelapse) enabled.push(t('printModal.timelapse'));
  if (data.print_options.mesh_mode_fast_check) enabled.push(t('printModal.meshModeFastCheck'));
  if (data.print_options.gcode_injection) enabled.push(t('printModal.gcodeInjection'));
  if (data.swap_macros.execute && data.swap_macros.events.length > 0) {
    enabled.push(`${t('printModal.swapMacros')} (${data.swap_macros.events.length})`);
  }
  if (enabled.length === 0) return t('printOptionsPrefs.allOff');
  return enabled.join(', ');
}

interface EditDialogProps {
  mode: 'edit' | 'add';
  existingEntries: PrintOptionsPreferenceAdminEntry[];
  users: UserResponse[];
  availableModels: string[];
  initialEntry: PrintOptionsPreferenceAdminEntry | null;
  onClose: () => void;
}

function EditDialog({ mode, existingEntries, users, availableModels, initialEntry, onClose }: EditDialogProps) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();

  const [userId, setUserId] = useState<number>(
    initialEntry?.user_id ?? users[0]?.id ?? 0,
  );
  const [printerModel, setPrinterModel] = useState<string>(
    initialEntry?.printer_model ?? availableModels[0] ?? '',
  );
  const [data, setData] = useState<PrintOptionsPreferenceData>(
    initialEntry?.options ?? DEFAULT_PRINT_OPTIONS,
  );

  const editingExisting = mode === 'edit';
  const collidesWithExisting = useMemo(() => {
    if (editingExisting) return false;
    return existingEntries.some(
      (e) => e.user_id === userId && e.printer_model.trim() === printerModel.trim(),
    );
  }, [editingExisting, existingEntries, userId, printerModel]);

  const swapEligible = isSwapEligibleModel(printerModel);

  const upsertMutation = useMutation({
    mutationFn: () => {
      // Strip swap_macros for non-A1 models so the row never carries a
      // misleading "execute=true" — the panel hides the controls in that
      // case, but a row edited after the model was changed could otherwise
      // retain stale swap state.
      const payload: PrintOptionsPreferenceData = swapEligible
        ? data
        : { ...data, swap_macros: { execute: false, events: [] } };
      return api.adminUpsertPrintOptionsPreference(userId, printerModel.trim(), payload);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['print-options-preferences-admin'] });
      queryClient.invalidateQueries({ queryKey: ['print-options-preference'] });
      showToast(t('printOptionsPrefs.toast.saved'));
      onClose();
    },
    onError: (err: Error) => {
      showToast(err.message || t('printOptionsPrefs.toast.saveFailed'), 'error');
    },
  });

  const canSave =
    userId > 0 && printerModel.trim().length > 0 && !collidesWithExisting && !upsertMutation.isPending;

  const togglePrintOption = (key: keyof PrintOptionsPreferenceData['print_options']) => {
    setData((prev) => ({
      ...prev,
      print_options: { ...prev.print_options, [key]: !prev.print_options[key] },
    }));
  };

  const toggleSwapEvent = (event: string) => {
    setData((prev) => {
      const has = prev.swap_macros.events.includes(event);
      return {
        ...prev,
        swap_macros: {
          ...prev.swap_macros,
          events: has
            ? prev.swap_macros.events.filter((e) => e !== event)
            : [...prev.swap_macros.events, event],
        },
      };
    });
  };

  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4" onClick={onClose}>
      <div
        className="bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg p-5 w-full max-w-md max-h-[90vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="text-lg font-semibold text-white mb-4">
          {editingExisting ? t('printOptionsPrefs.editTitle') : t('printOptionsPrefs.addTitle')}
        </h3>

        <div className="space-y-3 mb-4">
          <div>
            <label className="block text-xs text-bambu-gray mb-1">
              {t('printOptionsPrefs.col.user')}
            </label>
            <select
              value={userId}
              disabled={editingExisting}
              onChange={(e) => setUserId(Number(e.target.value))}
              className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:outline-none focus:border-bambu-green disabled:opacity-60"
            >
              {users.map((u) => (
                <option key={u.id} value={u.id}>
                  {u.username}
                </option>
              ))}
            </select>
          </div>

          <div>
            <label className="block text-xs text-bambu-gray mb-1">
              {t('printOptionsPrefs.col.model')}
            </label>
            {editingExisting ? (
              <input
                type="text"
                value={printerModel}
                disabled
                className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:outline-none disabled:opacity-60"
              />
            ) : availableModels.length === 0 ? (
              <p className="text-sm text-bambu-gray italic">
                {t('printOptionsPrefs.noModelsAvailable')}
              </p>
            ) : (
              <select
                value={printerModel}
                onChange={(e) => setPrinterModel(e.target.value)}
                className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:outline-none focus:border-bambu-green"
              >
                {availableModels.map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </select>
            )}
            {collidesWithExisting && (
              <p className="text-xs text-red-400 mt-1">{t('printOptionsPrefs.alreadyExists')}</p>
            )}
          </div>
        </div>

        <div className="border-t border-bambu-dark-tertiary pt-3 mb-4">
          <h4 className="text-sm font-medium text-white mb-2">{t('printModal.printOptions')}</h4>
          <div className="space-y-1.5">
            {(
              [
                ['bed_levelling', 'printModal.bedLeveling'],
                ['flow_cali', 'printModal.flowCalibration'],
                ['layer_inspect', 'printModal.layerInspection'],
                ['timelapse', 'printModal.timelapse'],
                ['mesh_mode_fast_check', 'printModal.meshModeFastCheck'],
                ['gcode_injection', 'printModal.gcodeInjection'],
              ] as const
            ).map(([key, labelKey]) => (
              <label key={key} className="flex items-center gap-2 cursor-pointer">
                <input
                  type="checkbox"
                  checked={data.print_options[key]}
                  onChange={() => togglePrintOption(key)}
                  className="accent-bambu-green"
                />
                <span className="text-sm text-white">{t(labelKey)}</span>
              </label>
            ))}
          </div>
        </div>

        {swapEligible && (
          <div className="border-t border-bambu-dark-tertiary pt-3 mb-4">
            <h4 className="text-sm font-medium text-white mb-2">{t('printModal.swapMacros')}</h4>
            <label className="flex items-center gap-2 cursor-pointer mb-2">
              <input
                type="checkbox"
                checked={data.swap_macros.execute}
                onChange={() =>
                  setData((prev) => ({
                    ...prev,
                    swap_macros: { ...prev.swap_macros, execute: !prev.swap_macros.execute },
                  }))
                }
                className="accent-bambu-green"
              />
              <span className="text-sm text-white">{t('printOptionsPrefs.swapExecute')}</span>
            </label>
            {data.swap_macros.execute && (
              <div className="ml-5 space-y-1">
                {(['swap_mode_start', 'swap_mode_change_table'] as const).map((event) => (
                  <label key={event} className="flex items-center gap-2 cursor-pointer">
                    <input
                      type="checkbox"
                      checked={data.swap_macros.events.includes(event)}
                      onChange={() => toggleSwapEvent(event)}
                      className="accent-bambu-green"
                    />
                    <span className="text-xs text-bambu-gray">{event}</span>
                  </label>
                ))}
              </div>
            )}
          </div>
        )}

        <div className="flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="px-3 py-1.5 text-sm text-bambu-gray hover:text-white"
          >
            {t('common.cancel')}
          </button>
          <button
            type="button"
            disabled={!canSave}
            onClick={() => upsertMutation.mutate()}
            className="px-4 py-1.5 bg-bambu-green hover:bg-bambu-green-dark text-black text-sm font-medium rounded-lg disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {t('common.save')}
          </button>
        </div>
      </div>
    </div>
  );
}

interface CopyDialogProps {
  src: PrintOptionsPreferenceAdminEntry;
  users: UserResponse[];
  availableModels: string[];
  onClose: () => void;
}

function CopyDialog({ src, users, availableModels, onClose }: CopyDialogProps) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();

  const otherUsers = users.filter((u) => u.id !== src.user_id);
  const [dstUserId, setDstUserId] = useState<number>(otherUsers[0]?.id ?? 0);
  // The source's model may no longer be among the system's printers (printer
  // removed since the profile was saved). Default to it anyway when present
  // so the common "same model" case stays one click.
  const modelChoices = useMemo(
    () =>
      availableModels.includes(src.printer_model)
        ? availableModels
        : [src.printer_model, ...availableModels],
    [availableModels, src.printer_model],
  );
  const [dstModel, setDstModel] = useState<string>(src.printer_model);

  const copyMutation = useMutation({
    mutationFn: () =>
      api.adminCopyPrintOptionsPreference({
        src_user_id: src.user_id,
        src_printer_model: src.printer_model,
        dst_user_id: dstUserId,
        dst_printer_model: dstModel.trim() || undefined,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['print-options-preferences-admin'] });
      queryClient.invalidateQueries({ queryKey: ['print-options-preference'] });
      showToast(t('printOptionsPrefs.toast.copied'));
      onClose();
    },
    onError: (err: Error) => {
      showToast(err.message || t('printOptionsPrefs.toast.copyFailed'), 'error');
    },
  });

  const canCopy = dstUserId > 0 && dstModel.trim().length > 0 && !copyMutation.isPending;

  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4" onClick={onClose}>
      <div
        className="bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg p-5 w-full max-w-md"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="text-lg font-semibold text-white mb-1">
          {t('printOptionsPrefs.copyTitle')}
        </h3>
        <p className="text-xs text-bambu-gray mb-4">
          {t('printOptionsPrefs.copyFrom', { user: src.username, model: src.printer_model })}
        </p>

        <div className="space-y-3 mb-5">
          <div>
            <label className="block text-xs text-bambu-gray mb-1">
              {t('printOptionsPrefs.copyDstUser')}
            </label>
            <select
              value={dstUserId}
              onChange={(e) => setDstUserId(Number(e.target.value))}
              className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:outline-none focus:border-bambu-green"
            >
              {otherUsers.map((u) => (
                <option key={u.id} value={u.id}>
                  {u.username}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="block text-xs text-bambu-gray mb-1">
              {t('printOptionsPrefs.copyDstModel')}
            </label>
            <select
              value={dstModel}
              onChange={(e) => setDstModel(e.target.value)}
              className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:outline-none focus:border-bambu-green"
            >
              {modelChoices.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
            <p className="text-xs text-bambu-gray mt-1">
              {t('printOptionsPrefs.copyDstModelHint')}
            </p>
          </div>
        </div>

        <div className="flex justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            className="px-3 py-1.5 text-sm text-bambu-gray hover:text-white"
          >
            {t('common.cancel')}
          </button>
          <button
            type="button"
            disabled={!canCopy}
            onClick={() => copyMutation.mutate()}
            className="px-4 py-1.5 bg-bambu-green hover:bg-bambu-green-dark text-black text-sm font-medium rounded-lg disabled:opacity-50 disabled:cursor-not-allowed"
          >
            {t('printOptionsPrefs.copyAction')}
          </button>
        </div>
      </div>
    </div>
  );
}
