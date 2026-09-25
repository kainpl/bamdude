import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Loader2 } from 'lucide-react';
import { Button } from './Button';
import { Modal } from './Modal';
import type { ExtruderSlot } from '../api/client';

// Hotend ids as the firmware numbers them — BambuStudio's MAIN_EXTRUDER_ID /
// DEPUTY_EXTRUDER_ID.
const RIGHT_EXTRUDER = 0;
const LEFT_EXTRUDER = 1;

interface FeedDirectionModalProps {
  // Human-readable name of the slot being loaded, e.g. "A3".
  slotLabel: string;
  // The slot's own coordinates, to spot the hotend already holding it.
  amsId: number;
  slotId: number;
  extruderSlots: Record<string, ExtruderSlot>;
  isLoading?: boolean;
  onConfirm: (extruderId: number) => void;
  onCancel: () => void;
}

/**
 * Which hotend to feed a slot into, on a printer with a Filament Track Switch
 * (upstream 9500c046).
 *
 * Without a switch each AMS is wired to one hotend and the load names none.
 * With one, every AMS sits on a switch inlet and reaches both, and the firmware
 * drops a load that does not say which. BambuStudio asks the same question in
 * the same place (`FeedDirectionDialog`): neither side preselected, so a stray
 * Enter cannot feed the wrong one, and the hotend already holding filament
 * from this very slot greyed out.
 */
export function FeedDirectionModal({
  slotLabel,
  amsId,
  slotId,
  extruderSlots,
  isLoading = false,
  onConfirm,
  onCancel,
}: FeedDirectionModalProps) {
  const { t } = useTranslation();
  const [selected, setSelected] = useState<number | null>(null);

  // Studio counts a hotend as holding the slot only when it has filament in it
  // (HasFilamentInExt): a pointer left behind by an unload is not a load.
  const holdsThisSlot = (extruderId: number) => {
    const slot = extruderSlots[String(extruderId)];
    return !!slot?.has_filament && slot.ams_id === amsId && slot.slot_id === slotId;
  };

  const options = [
    { extruderId: LEFT_EXTRUDER, label: t('printers.ams.feedLeft'), taken: holdsThisSlot(LEFT_EXTRUDER) },
    { extruderId: RIGHT_EXTRUDER, label: t('printers.ams.feedRight'), taken: holdsThisSlot(RIGHT_EXTRUDER) },
  ];
  const selectedIsTaken = options.some((o) => o.extruderId === selected && o.taken);

  // Status keeps arriving while the dialog is open, so the picked hotend can
  // become the one holding this slot — loaded from the printer's own screen.
  // Drop the pick rather than leave Confirm armed on a disabled option.
  useEffect(() => {
    if (selectedIsTaken) setSelected(null);
  }, [selectedIsTaken]);

  return (
    <Modal onClose={onCancel} hideClose ariaLabel={t('printers.ams.feedTitle', { slot: slotLabel })} closeDisabled={isLoading} size="md">
      <div className="p-6">
        <h3 className="text-lg font-semibold text-white mb-2">{t('printers.ams.feedTitle', { slot: slotLabel })}</h3>
        <p className="text-bambu-gray text-sm">{t('printers.ams.feedPrompt')}</p>

        <div className="grid grid-cols-2 gap-3 mt-4">
          {options.map(({ extruderId, label, taken }) => {
            const isSelected = selected === extruderId;
            return (
              <button
                key={extruderId}
                type="button"
                onClick={() => setSelected(extruderId)}
                disabled={taken || isLoading}
                aria-pressed={isSelected}
                title={taken ? t('printers.ams.feedAlreadyLoaded') : undefined}
                className={`p-3 rounded-lg border text-sm transition-colors ${
                  taken
                    ? 'border-transparent bg-bambu-dark text-bambu-gray/50 cursor-not-allowed'
                    : isSelected
                      ? 'border-bambu-green/50 bg-bambu-green/10 text-white'
                      : 'border-transparent bg-bambu-dark text-white hover:bg-bambu-dark-tertiary'
                }`}
              >
                <div className="font-medium">{label}</div>
                {taken && <div className="text-xs mt-1">{t('printers.ams.feedAlreadyLoaded')}</div>}
              </button>
            );
          })}
        </div>

        <div className="flex gap-3 mt-6">
          <Button variant="secondary" onClick={onCancel} className="flex-1" disabled={isLoading}>
            {t('common.cancel')}
          </Button>
          <Button
            onClick={() => selected !== null && onConfirm(selected)}
            className="flex-1"
            disabled={selected === null || isLoading}
          >
            {isLoading ? (
              <>
                <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                {t('common.loading')}
              </>
            ) : (
              t('common.confirm')
            )}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
