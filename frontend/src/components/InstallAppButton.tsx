import { useState } from 'react';
import { Download } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useToast } from '../contexts/ToastContext';
import { useInstallPrompt } from '../hooks/useInstallPrompt';

/**
 * Sidebar-footer button that installs BamDude as a PWA.
 *
 * Chrome for Android removed the automatic install banner in Chrome 108, so
 * without an in-app trigger the only install path on Android is a buried
 * browser-menu item (#1460). This button re-fires the captured
 * `beforeinstallprompt` event on click. It renders nothing when the browser has
 * no pending prompt - already installed, unsupported browser, or iOS Safari
 * (which has no programmatic install at all).
 *
 * The capture itself lives in `useInstallPrompt` because the sidebar asks the
 * same question when laying out its icon rows.
 */
export function InstallAppButton() {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const promptEvent = useInstallPrompt();
  const [used, setUsed] = useState(false);

  if (!promptEvent || used) {
    return null;
  }

  const handleInstall = async () => {
    await promptEvent.prompt();
    const { outcome } = await promptEvent.userChoice;
    // A captured prompt can only be used once; hide either way until the
    // browser fires a fresh beforeinstallprompt.
    setUsed(true);
    if (outcome === 'accepted') {
      showToast(t('nav.installAppSuccess'), 'success');
    }
  };

  return (
    <button
      onClick={handleInstall}
      className="p-2 rounded-lg hover:bg-bambu-dark-tertiary transition-colors text-bambu-gray-light hover:text-white"
      title={t('nav.installApp')}
      aria-label={t('nav.installApp')}
    >
      <Download className="w-5 h-5" />
    </button>
  );
}
