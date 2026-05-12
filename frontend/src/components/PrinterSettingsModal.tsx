import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { X } from 'lucide-react';

import { useToast } from '../contexts/ToastContext';
import { usePrinterSettings } from '../hooks/usePrinterSettings';
import { PrintOptionsTab } from './PrintOptionsTab';
import { PrinterPartsTab } from './PrinterPartsTab';
import type { PrinterSettingsPostBody } from '../api/client';

type TabId = 'print_options' | 'parts';

interface Props {
  isOpen: boolean;
  onClose: () => void;
  printerId: number;
}

export function PrinterSettingsModal({ isOpen, onClose, printerId }: Props) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const { data, isLoading, mutate, refetch } = usePrinterSettings(printerId, isOpen);
  const [activeTab, setActiveTab] = useState<TabId>('print_options');

  if (!isOpen) return null;

  const onSubmit = async (body: PrinterSettingsPostBody) => {
    try {
      await mutate(body);
    } catch (e) {
      showToast((e as Error)?.message ?? t('printerSettings.requestFailed'), 'error');
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
      <div className="bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-xl shadow-2xl w-full max-w-2xl mx-4 max-h-[90vh] overflow-y-auto">
        <div className="flex justify-between items-center p-4 border-b border-bambu-dark-tertiary">
          <h2 className="text-lg font-semibold text-white">{t('printerSettings.title')}</h2>
          <button
            onClick={onClose}
            aria-label="Close"
            className="p-1 text-bambu-gray hover:text-white rounded transition-colors"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        <div className="px-4 pt-3">
          <div className="inline-flex gap-1 rounded-lg p-1 bg-bambu-dark">
            <TabBtn id="print_options" active={activeTab} onClick={setActiveTab}>
              {t('printerSettings.tab.printOptions')}
            </TabBtn>
            <TabBtn id="parts" active={activeTab} onClick={setActiveTab}>
              {t('printerSettings.tab.parts')}
            </TabBtn>
          </div>
        </div>

        <div className="p-4">
          {isLoading || !data ? (
            <div className="space-y-3">
              {[0, 1, 2, 3].map((i) => (
                <div key={i} className="animate-pulse h-10 bg-bambu-dark rounded" />
              ))}
            </div>
          ) : activeTab === 'print_options' ? (
            <PrintOptionsTab data={data} onSubmit={onSubmit} />
          ) : (
            <PrinterPartsTab data={data} onRefetch={() => refetch()} />
          )}
        </div>
      </div>
    </div>
  );
}

function TabBtn({
  id, active, onClick, children,
}: { id: TabId; active: TabId; onClick: (id: TabId) => void; children: React.ReactNode }) {
  const isActive = id === active;
  return (
    <button
      type="button"
      onClick={() => onClick(id)}
      className={`px-3 py-1.5 text-sm rounded-md transition-colors ${
        isActive ? 'bg-bambu-green text-white' : 'text-bambu-gray hover:text-white'
      }`}
    >
      {children}
    </button>
  );
}
