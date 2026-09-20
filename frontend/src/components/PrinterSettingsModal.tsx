import { useState } from 'react';
import { useTranslation } from 'react-i18next';

import { useToast } from '../contexts/ToastContext';
import { usePrinterSettings } from '../hooks/usePrinterSettings';
import { PrintOptionsTab } from './PrintOptionsTab';
import { PrinterSafetyTab } from './PrinterSafetyTab';
import { PrinterPartsTab } from './PrinterPartsTab';
import { PrinterAddonsTab } from './PrinterAddonsTab';
import { Modal } from './Modal';
import type { PrinterSettingsPostBody } from '../api/client';

type TabId = 'print_options' | 'safety' | 'parts' | 'addons';

interface Props {
  isOpen: boolean;
  onClose: () => void;
  printerId: number;
}

export function PrinterSettingsModal({ isOpen, onClose, printerId }: Props) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const { data, isLoading, submit, isPending, refetch } = usePrinterSettings(printerId, isOpen);
  const [activeTab, setActiveTab] = useState<TabId>('print_options');

  if (!isOpen) return null;

  const onSubmit = async (body: PrinterSettingsPostBody) => {
    try {
      await submit(body);
    } catch (e) {
      showToast((e as Error)?.message ?? t('printerSettings.requestFailed'), 'error');
    }
  };

  return (
    <Modal onClose={onClose} title={t('printerSettings.title')} size="2xl">
      <div className="px-4 pt-3">
        <div className="inline-flex gap-1 rounded-lg p-1 bg-bambu-dark">
          <TabBtn id="print_options" active={activeTab} onClick={setActiveTab}>
            {t('printerSettings.tab.printOptions')}
          </TabBtn>
          {data?.supports.safety_tab && (
            <TabBtn id="safety" active={activeTab} onClick={setActiveTab}>
              {t('printerSettings.tab.safety')}
            </TabBtn>
          )}
          <TabBtn id="parts" active={activeTab} onClick={setActiveTab}>
            {t('printerSettings.tab.parts')}
          </TabBtn>
          <TabBtn id="addons" active={activeTab} onClick={setActiveTab}>
            {t('printerSettings.tab.addons')}
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
          <PrintOptionsTab data={data} onSubmit={onSubmit} isPending={isPending} />
        ) : activeTab === 'safety' ? (
          <PrinterSafetyTab data={data} onSubmit={onSubmit} isPending={isPending} />
        ) : activeTab === 'parts' ? (
          <PrinterPartsTab data={data} onRefetch={() => refetch()} />
        ) : (
          <PrinterAddonsTab data={data} onRefetch={() => refetch()} />
        )}
      </div>
    </Modal>
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
