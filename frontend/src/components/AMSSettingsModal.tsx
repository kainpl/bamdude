import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { X, Loader2 } from 'lucide-react';

import { useAmsSettings } from '../hooks/useAmsSettings';
import { useToast } from '../contexts/ToastContext';
import type { AmsSettingsPostBody, AmsSystemSettingState } from '../api/client';
import { Button } from './Button';

interface Props {
  isOpen: boolean;
  onClose: () => void;
  printerId: number;
}

type LocalState = AmsSystemSettingState;

export function AMSSettingsModal({ isOpen, onClose, printerId }: Props) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const { data, isLoading, mutate } = useAmsSettings(printerId, isOpen);
  const [localState, setLocalState] = useState<LocalState | null>(null);
  const [selectedAmsId, setSelectedAmsId] = useState<number | null>(null);
  const [reorderConfirm, setReorderConfirm] = useState(false);
  const [fwSwitchConfirm, setFwSwitchConfirm] = useState<number | null>(null);
  const [pendingFlag, setPendingFlag] = useState<string | null>(null);

  // Sync server state into local copy once it lands / refreshes.
  useEffect(() => {
    if (data?.state) setLocalState(data.state);
  }, [data?.state]);

  useEffect(() => {
    if (selectedAmsId == null && data?.ams_units?.length) {
      setSelectedAmsId(data.ams_units[0].ams_id);
    }
  }, [data?.ams_units, selectedAmsId]);

  if (!isOpen) return null;

  const supports = data?.supports;
  const s = localState ?? data?.state ?? null;

  const submit = async (
    body: AmsSettingsPostBody,
    optimistic: Partial<LocalState>,
    flagKey?: string,
  ) => {
    if (!localState) return;
    const prev = localState;
    setLocalState({ ...localState, ...optimistic });
    setPendingFlag(flagKey ?? body.action);
    try {
      await mutate(body);
    } catch (e) {
      setLocalState(prev);
      showToast((e as Error)?.message ?? t('amsSettings.requestFailed'), 'error');
    } finally {
      setPendingFlag(null);
    }
  };

  const onToggleInsertion = (next: boolean) => {
    if (!s) return;
    submit(
      {
        action: 'user_setting',
        tray_read_option: next,
        startup_read_option: !!s.power_on_update,
        calibrate_remain_flag: !!s.remain_capacity,
      },
      { insertion_update: next },
      'insertion_update',
    );
  };
  const onTogglePowerOn = (next: boolean) => {
    if (!s) return;
    submit(
      {
        action: 'user_setting',
        tray_read_option: !!s.insertion_update,
        startup_read_option: next,
        calibrate_remain_flag: !!s.remain_capacity,
      },
      { power_on_update: next },
      'power_on_update',
    );
  };
  const onToggleRemain = (next: boolean) => {
    if (!s) return;
    submit(
      {
        action: 'user_setting',
        tray_read_option: !!s.insertion_update,
        startup_read_option: !!s.power_on_update,
        calibrate_remain_flag: next,
      },
      { remain_capacity: next },
      'remain_capacity',
    );
  };
  const onToggleBackup = (next: boolean) =>
    submit({ action: 'auto_switch_filament', enabled: next }, { auto_switch_filament: next });
  const onToggleAirPrint = (next: boolean) =>
    submit({ action: 'air_print_detect', enabled: next }, { air_print_detect: next });

  const onCalibrate = async () => {
    if (selectedAmsId == null) return;
    try {
      await mutate({ action: 'calibrate', ams_id: selectedAmsId });
    } catch (e) {
      showToast((e as Error)?.message ?? t('amsSettings.requestFailed'), 'error');
    }
  };

  const onConfirmFwSwitch = async () => {
    if (fwSwitchConfirm == null) return;
    try {
      await mutate({ action: 'firmware_switch', firmware_idx: fwSwitchConfirm });
    } catch (e) {
      showToast((e as Error)?.message ?? t('amsSettings.requestFailed'), 'error');
    }
    setFwSwitchConfirm(null);
  };

  const onConfirmReorder = async () => {
    try {
      await mutate({ action: 'reorder' });
    } catch (e) {
      showToast((e as Error)?.message ?? t('amsSettings.requestFailed'), 'error');
    }
    setReorderConfirm(false);
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
      <div className="bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-xl shadow-2xl w-full max-w-md mx-4 max-h-[90vh] overflow-y-auto">
        <div className="flex justify-between items-center p-4 border-b border-bambu-dark-tertiary">
          <h2 className="text-lg font-semibold text-white">
            {t('amsSettings.title')}
          </h2>
          <button
            onClick={onClose}
            aria-label={t('amsSettings.cancel')}
            className="p-1 text-bambu-gray hover:text-white rounded transition-colors"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        {isLoading || !s || !supports ? (
          <div className="p-6 space-y-3">
            {[0, 1, 2, 3].map((i) => (
              <div
                key={i}
                className="animate-pulse h-12 bg-bambu-dark rounded"
              />
            ))}
          </div>
        ) : (
          <div className="p-4 space-y-4">
            {supports.insertion_update && (
              <CheckRow
                title={t('amsSettings.insertionUpdate')}
                tip={
                  s.insertion_update
                    ? `${t('amsSettings.insertionUpdateTipOn')} ${t('amsSettings.insertionUpdateTipNote')}`
                    : t('amsSettings.insertionUpdateTipOff')
                }
                checked={!!s.insertion_update}
                disabled={pendingFlag === 'insertion_update'}
                onChange={onToggleInsertion}
              />
            )}
            {supports.power_on_update && (
              <CheckRow
                title={t('amsSettings.powerOnUpdate')}
                tip={
                  s.power_on_update
                    ? t('amsSettings.powerOnTipOn')
                    : t('amsSettings.powerOnTipOff')
                }
                checked={!!s.power_on_update}
                disabled={pendingFlag === 'power_on_update'}
                onChange={onTogglePowerOn}
              />
            )}
            {supports.remain_capacity && (
              <CheckRow
                title={t('amsSettings.updateRemain')}
                tip={t('amsSettings.updateRemainTip')}
                checked={!!s.remain_capacity}
                disabled={pendingFlag === 'remain_capacity'}
                onChange={onToggleRemain}
              />
            )}
            {supports.auto_switch_filament && (
              <CheckRow
                title={t('amsSettings.filamentBackup')}
                tip={t('amsSettings.filamentBackupTip')}
                checked={!!s.auto_switch_filament}
                disabled={pendingFlag === 'auto_switch_filament'}
                onChange={onToggleBackup}
              />
            )}
            {supports.air_print_detect && (
              <CheckRow
                title={t('amsSettings.airPrintDetection')}
                tip={t('amsSettings.airPrintTip')}
                checked={!!s.air_print_detect}
                disabled={pendingFlag === 'air_print_detect'}
                onChange={onToggleAirPrint}
              />
            )}

            {supports.firmware_switch && data && (
              <div className="border-t border-bambu-dark-tertiary pt-3">
                <div className="font-medium text-white">
                  {t('amsSettings.amsType')}
                </div>
                <div className="mt-2 flex gap-2 items-center">
                  <select
                    className="bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1 text-white focus:border-bambu-green focus:outline-none"
                    value={s.firmware_idx_sel ?? data.firmware_options[0]?.idx ?? 0}
                    onChange={(e) => setFwSwitchConfirm(Number(e.target.value))}
                  >
                    {data.firmware_options.map((o) => (
                      <option key={o.idx} value={o.idx}>
                        {o.label}
                      </option>
                    ))}
                  </select>
                </div>
              </div>
            )}

            {supports.reorder && (
              <div className="border-t border-bambu-dark-tertiary pt-3">
                <div className="font-medium text-white">
                  {t('amsSettings.arrangeOrder')}
                </div>
                <p className="text-sm text-bambu-gray mt-1">
                  {t('amsSettings.arrangeNote')}
                </p>
                <Button
                  variant="secondary"
                  className="mt-2"
                  onClick={() => setReorderConfirm(true)}
                >
                  {t('amsSettings.reset')}
                </Button>
              </div>
            )}

            {(data?.ams_units?.length ?? 0) > 0 && (
              <div className="border-t border-bambu-dark-tertiary pt-3">
                <div className="font-medium text-white">
                  {t('amsSettings.calibrate')}
                </div>
                <div className="mt-2 flex gap-2 items-center">
                  <label className="text-sm text-bambu-gray">
                    {t('amsSettings.selectAmsForCalibrate')}:
                  </label>
                  <select
                    className="bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1 text-white focus:border-bambu-green focus:outline-none"
                    value={selectedAmsId ?? ''}
                    onChange={(e) => setSelectedAmsId(Number(e.target.value))}
                  >
                    {data!.ams_units.map((u) => (
                      <option key={u.ams_id} value={u.ams_id}>
                        {u.label}
                      </option>
                    ))}
                  </select>
                  <Button variant="secondary" onClick={onCalibrate}>
                    {t('amsSettings.calibrate')}
                  </Button>
                </div>
              </div>
            )}
          </div>
        )}

        {reorderConfirm && (
          <ConfirmDialog
            title={t('amsSettings.reorderTitle')}
            body={t('amsSettings.confirmReorder')}
            confirmLabel={t('amsSettings.confirm')}
            cancelLabel={t('amsSettings.cancel')}
            onConfirm={onConfirmReorder}
            onCancel={() => setReorderConfirm(false)}
          />
        )}

        {fwSwitchConfirm != null && (
          <ConfirmDialog
            title={t('amsSettings.amsType')}
            body={t('amsSettings.switchFirmwareConfirm')}
            confirmLabel={t('amsSettings.confirm')}
            cancelLabel={t('amsSettings.cancel')}
            onConfirm={onConfirmFwSwitch}
            onCancel={() => setFwSwitchConfirm(null)}
          />
        )}
      </div>
    </div>
  );
}

interface CheckRowProps {
  title: string;
  tip: string;
  checked: boolean;
  disabled?: boolean;
  onChange: (v: boolean) => void;
}

function CheckRow({ title, tip, checked, disabled, onChange }: CheckRowProps) {
  return (
    <label className={`flex items-start gap-3 ${disabled ? 'opacity-60' : 'cursor-pointer'}`}>
      <input
        type="checkbox"
        className="mt-1 h-4 w-4 accent-bambu-green"
        checked={checked}
        disabled={!!disabled}
        onChange={(e) => onChange(e.target.checked)}
        aria-label={title}
      />
      <div className="flex-1">
        <div className="font-medium text-white flex items-center gap-2">
          {title}
          {disabled && <Loader2 className="h-3 w-3 animate-spin text-bambu-gray" />}
        </div>
        <p className="text-sm text-bambu-gray">{tip}</p>
      </div>
    </label>
  );
}

interface ConfirmDialogProps {
  title: string;
  body: string;
  confirmLabel: string;
  cancelLabel: string;
  onConfirm: () => void;
  onCancel: () => void;
}

function ConfirmDialog({
  title,
  body,
  confirmLabel,
  cancelLabel,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  return (
    <div className="fixed inset-0 z-[60] flex items-center justify-center bg-black/60">
      <div className="bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-xl shadow-2xl p-4 max-w-sm mx-4">
        <h3 className="font-semibold text-white">{title}</h3>
        <p className="mt-2 text-sm text-bambu-gray">{body}</p>
        <div className="mt-4 flex justify-end gap-2">
          <Button variant="secondary" onClick={onCancel}>
            {cancelLabel}
          </Button>
          <Button onClick={onConfirm}>{confirmLabel}</Button>
        </div>
      </div>
    </div>
  );
}
