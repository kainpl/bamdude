import { useTranslation } from 'react-i18next';
import type { SecretAction } from '../api/client';

export function SecretInput({ value, action = 'keep', configured = false, disabled = false, onChange, onActionChange }: {
  value: string;
  action?: SecretAction;
  configured?: boolean;
  disabled?: boolean;
  onChange: (value: string) => void;
  onActionChange: (action: SecretAction) => void;
}) {
  const { t } = useTranslation();
  return <div className="space-y-2">
    <p className="text-xs text-bambu-gray">{t(configured ? 'settings.credentials.configured' : 'settings.credentials.notConfigured')}</p>
    <select aria-label={t('settings.credentials.action')} value={action} disabled={disabled}
      onChange={event => { onActionChange(event.target.value as SecretAction); onChange(''); }}
      className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white disabled:opacity-50">
      <option value="keep">{t('settings.credentials.keep')}</option>
      <option value="replace">{t('settings.credentials.replace')}</option>
      <option value="clear">{t('settings.credentials.clear')}</option>
    </select>
    {action === 'replace' && <input type="password" autoComplete="new-password" aria-label={t('settings.credentials.replacement')}
      value={value} disabled={disabled} onChange={event => onChange(event.target.value)}
      className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white disabled:opacity-50" />}
  </div>;
}
