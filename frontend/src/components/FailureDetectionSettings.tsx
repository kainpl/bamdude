import { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { useQuery } from '@tanstack/react-query';
import { api } from '../api/client';
import { Card, CardContent } from './Card';
import { Select } from './Select';
import { ObicoSettings } from './ObicoSettings';
import { OctoEverywhereSettings } from './OctoEverywhereSettings';

type Provider = 'obico' | 'octoeverywhere';

/** The Failure Detection tab: pick the provider whose panel is shown (upstream Bambuddy).
 *
 * Each panel has its own on/off switch; turning one provider on switches the other off on the
 * server, so the farm runs at most one.
 */
export function FailureDetectionSettings() {
  const { t } = useTranslation();
  const [selectedProvider, setSelectedProvider] = useState<Provider | null>(null);
  const { data: settings } = useQuery({
    queryKey: ['settings'],
    queryFn: api.getSettings,
  });
  const provider = selectedProvider ?? (settings?.octoeverywhere_enabled ? 'octoeverywhere' : 'obico');

  useEffect(() => {
    if (!settings) return;
    // Keep the selected panel visible when its provider is disabled or settings
    // refresh after a save. Only the first load follows the active provider.
    setSelectedProvider((current) => current ?? (settings.octoeverywhere_enabled ? 'octoeverywhere' : 'obico'));
  }, [settings]);

  return (
    <div className="space-y-4">
      <Card id="card-fd-provider">
        <CardContent>
          <label htmlFor="fd-provider" className="block text-sm text-bambu-gray mb-1">
            {t('failureDetection.provider')}
          </label>
          <Select
            id="fd-provider"
            className="w-full lg:max-w-xl"
            value={provider}
            onChange={(e) => setSelectedProvider(e.target.value as Provider)}
          >
            <option value="obico">{t('failureDetection.providerObico')}</option>
            <option value="octoeverywhere">{t('failureDetection.providerOctoEverywhere')}</option>
          </Select>
        </CardContent>
      </Card>
      {provider === 'octoeverywhere' ? <OctoEverywhereSettings /> : <ObicoSettings />}
    </div>
  );
}
