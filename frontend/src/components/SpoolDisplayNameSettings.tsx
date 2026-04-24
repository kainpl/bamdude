import { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Loader2, Check } from 'lucide-react';

import { api } from '../api/client';
import type { InventorySpool } from '../api/client';
import { Card, CardContent, CardHeader } from './Card';
import { Button } from './Button';
import { useToast } from '../contexts/ToastContext';
import {
  DEFAULT_SPOOL_DISPLAY_TEMPLATE,
  SPOOL_PLACEHOLDERS,
  formatSpoolDisplayName,
} from '../utils/spoolName';

// Fallback preview spool used when the inventory is empty — every placeholder
// has a sensible value so every chip in the reference list renders a non-empty
// example when clicked.
const PREVIEW_FALLBACK: InventorySpool = {
  id: 0,
  material: 'PLA',
  subtype: 'Matte',
  color_name: 'Jade White',
  rgba: 'EAF5EAFF',
  brand: 'Polymaker',
  label_weight: 1000,
  core_weight: 250,
  core_weight_catalog_id: null,
  weight_used: 250,
  slicer_filament: null,
  slicer_filament_name: 'Polymaker PolyTerra PLA @Bambu Lab X1C',
  nozzle_temp_min: null,
  nozzle_temp_max: null,
  note: 'Kitchen shelf',
  added_full: null,
  last_used: null,
  encode_time: null,
  tag_uid: null,
  tray_uuid: null,
  data_origin: null,
  tag_type: null,
  archived_at: null,
  created_at: '2026-01-01T00:00:00',
  updated_at: '2026-01-01T00:00:00',
  cost_per_kg: 25,
  purchase_date: '2026-01-01T00:00:00',
  filament_diameter: '1.75',
  lot: 1,
  last_scale_weight: null,
  last_weighed_at: null,
};

export function SpoolDisplayNameSettings() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();

  const { data: settings, isLoading } = useQuery({
    queryKey: ['settings'],
    queryFn: api.getSettings,
  });

  // Preview uses a real inventory spool when available so the operator sees the
  // template applied to actual data (catches empty-value surprises early). Only
  // first active spool is fetched — the Filaments page already loads the full
  // list, so this query is a cheap cache hit in practice.
  const { data: spools } = useQuery({
    queryKey: ['spools'],
    queryFn: () => api.getSpools(false),
  });
  const previewSpool: InventorySpool = spools?.[0] ?? PREVIEW_FALLBACK;

  const [localTemplate, setLocalTemplate] = useState('');

  useEffect(() => {
    if (settings) {
      setLocalTemplate(settings.spool_display_template ?? DEFAULT_SPOOL_DISPLAY_TEMPLATE);
    }
  }, [settings]);

  const savedTemplate = settings?.spool_display_template ?? DEFAULT_SPOOL_DISPLAY_TEMPLATE;
  const dirty = localTemplate !== savedTemplate;

  const saveMutation = useMutation({
    mutationFn: (template: string) => api.updateSettings({ spool_display_template: template }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['settings'] });
      showToast(t('settings.spoolDisplayName.saved'), 'success');
    },
    onError: (err: Error) =>
      showToast(err.message || t('settings.spoolDisplayName.saveFailed'), 'error'),
  });

  const insertPlaceholder = (key: string) => {
    // Append with a separating space unless the template already ends in
    // whitespace or is empty — matches operator expectation that clicking
    // a chip "adds" a field rather than overwriting selection.
    setLocalTemplate((prev) => {
      if (!prev) return `{${key}}`;
      const sep = /\s$/.test(prev) ? '' : ' ';
      return `${prev}${sep}{${key}}`;
    });
  };

  const previewText = formatSpoolDisplayName(previewSpool, localTemplate);

  return (
    <Card>
      <CardHeader>
        <h2 className="text-lg font-semibold text-white">
          {t('settings.spoolDisplayName.title')}
        </h2>
        <p className="text-xs text-bambu-gray mt-1">{t('settings.spoolDisplayName.description')}</p>
      </CardHeader>
      <CardContent>
        <div className="space-y-4">
          {/* Template input */}
          <div>
            <label className="block text-sm text-bambu-gray mb-1">
              {t('settings.spoolDisplayName.templateLabel')}
            </label>
            <input
              type="text"
              value={localTemplate}
              onChange={(e) => setLocalTemplate(e.target.value)}
              placeholder={DEFAULT_SPOOL_DISPLAY_TEMPLATE}
              disabled={isLoading}
              className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white font-mono text-sm focus:border-bambu-green focus:outline-none disabled:opacity-50"
            />
            <p className="text-xs text-bambu-gray mt-1">
              {t('settings.spoolDisplayName.templateHint')}
            </p>
          </div>

          {/* Live preview */}
          <div className="rounded-lg bg-bambu-dark border border-bambu-dark-tertiary p-3">
            <div className="text-xs text-bambu-gray mb-1">
              {t('settings.spoolDisplayName.previewLabel')}
              {!spools?.length && (
                <span className="ml-2 italic">{t('settings.spoolDisplayName.previewFallback')}</span>
              )}
            </div>
            <div className="text-white font-medium break-all">
              {previewText || <span className="italic text-bambu-gray">&lt;empty&gt;</span>}
            </div>
          </div>

          {/* Placeholder reference */}
          <div>
            <div className="text-sm text-bambu-gray mb-2">
              {t('settings.spoolDisplayName.placeholdersLabel')}
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2">
              {SPOOL_PLACEHOLDERS.map((ph) => (
                <button
                  key={ph.key}
                  type="button"
                  onClick={() => insertPlaceholder(ph.key)}
                  title={ph.description}
                  className="text-left px-3 py-2 rounded-lg bg-bambu-dark border border-bambu-dark-tertiary hover:border-bambu-green transition-colors group"
                >
                  <div className="flex items-baseline justify-between gap-2">
                    <code className="text-xs text-bambu-green font-mono">{`{${ph.key}}`}</code>
                    <span className="text-xs text-bambu-gray truncate">{ph.label}</span>
                  </div>
                  <div className="text-xs text-bambu-gray truncate mt-0.5">
                    <span className="text-white/70">{ph.format(previewSpool) || '—'}</span>
                  </div>
                </button>
              ))}
            </div>
          </div>

          {/* Save + reset */}
          <div className="flex items-center gap-2 pt-2">
            <Button
              onClick={() => saveMutation.mutate(localTemplate)}
              disabled={!dirty || saveMutation.isPending}
              variant="primary"
              size="sm"
            >
              {saveMutation.isPending ? (
                <Loader2 className="w-4 h-4 animate-spin" />
              ) : (
                <Check className="w-4 h-4" />
              )}
              <span className="ml-2">{t('settings.spoolDisplayName.save')}</span>
            </Button>
            <Button
              onClick={() => setLocalTemplate(DEFAULT_SPOOL_DISPLAY_TEMPLATE)}
              disabled={localTemplate === DEFAULT_SPOOL_DISPLAY_TEMPLATE}
              variant="secondary"
              size="sm"
            >
              {t('settings.spoolDisplayName.resetDefault')}
            </Button>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
