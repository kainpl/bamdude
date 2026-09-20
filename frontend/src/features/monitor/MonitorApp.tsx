import { useMemo } from 'react';
import { I18nextProvider } from 'react-i18next';
import i18n from '../../i18n';
import { ThemeProvider } from '../../contexts/ThemeContext';
import { ToastProvider } from '../../contexts/ToastContext';
import MonitorPage from '../../pages/MonitorPage';

export default function MonitorApp() {
  // Monitor language is local to this window; TV configuration must not write
  // the account/browser's ordinary language preference.
  const language = useMemo(() => i18n.cloneInstance({ detection: { caches: [] } }), []);
  return <I18nextProvider i18n={language}>
    <ThemeProvider syncServer={false}><ToastProvider><MonitorPage /></ToastProvider></ThemeProvider>
  </I18nextProvider>;
}
