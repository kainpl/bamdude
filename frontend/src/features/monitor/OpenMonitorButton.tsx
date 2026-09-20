import { useState } from 'react';
import { MonitorUp } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { Button } from '../../components/Button';
import { monitorUrl } from './location';
import type { MonitorView } from './types';

export function OpenMonitorButton({ view, sort }: { view: MonitorView; sort?: string }) {
  const { t } = useTranslation();
  const [blocked, setBlocked] = useState(false);
  const url = monitorUrl(view, sort);
  const open = () => {
    const child = window.open(url, '_blank', 'popup,width=1600,height=1000');
    if (child) child.opener = null;
    else setBlocked(true);
  };
  return <div className="flex items-center gap-2">
    <Button size="sm" variant="outline" onClick={open}><MonitorUp size={16} />{t('monitor.open')}</Button>
    {blocked && <a href={url} target="_blank" rel="noopener noreferrer" className="text-sm text-bambu-green underline">{t('monitor.popupFallback')}</a>}
  </div>;
}
