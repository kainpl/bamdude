import { useId } from 'react';
import { useTranslation } from 'react-i18next';
import type { VPNozzleDiameter, VPNozzleFlow } from '../api/client';
import { Select } from './Select';

export function VirtualPrinterNozzleFields({ diameter, flow, onDiameterChange, onFlowChange, disabled, mainOnly }: {
  diameter: VPNozzleDiameter;
  flow: VPNozzleFlow;
  onDiameterChange: (value: VPNozzleDiameter) => void;
  onFlowChange: (value: VPNozzleFlow) => void;
  disabled?: boolean;
  mainOnly?: boolean;
}) {
  const { t } = useTranslation();
  const id = useId();
  return (
    <div className="space-y-2">
      {mainOnly && <p className="text-sm text-white font-medium">{t('virtualPrinter.nozzle.main')}</p>}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <div>
          <label htmlFor={`${id}-diameter`} className="block text-sm text-white mb-1">{t('virtualPrinter.nozzle.diameter')}</label>
          <Select id={`${id}-diameter`} tone="raised" className="w-full" value={diameter} disabled={disabled}
            onChange={(e) => onDiameterChange(e.target.value as VPNozzleDiameter)}>
            {(['0.2', '0.4', '0.6', '0.8'] as const).map((value) => <option key={value} value={value}>{value} mm</option>)}
          </Select>
        </div>
        <div>
          <label htmlFor={`${id}-flow`} className="block text-sm text-white mb-1">{t('virtualPrinter.nozzle.flow')}</label>
          <Select id={`${id}-flow`} tone="raised" className="w-full" value={flow} disabled={disabled}
            onChange={(e) => onFlowChange(e.target.value as VPNozzleFlow)}>
            {(['standard', 'high_flow', 'tpu_high_flow', 'e3d_high_flow'] as const).map((value) => (
              <option key={value} value={value}>{t(`virtualPrinter.nozzle.${value}`)}</option>
            ))}
          </Select>
        </div>
      </div>
      <p className="text-xs text-bambu-gray">{t('virtualPrinter.nozzle.hint')}</p>
    </div>
  );
}
