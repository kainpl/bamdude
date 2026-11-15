import { useState, useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { ArrowDownAZ, ArrowUpAZ, Check, ChevronDown, ChevronUp, Loader2, X } from 'lucide-react';

import { api } from '../api/client';
import { Card, CardContent, CardHeader } from './Card';
import { Button } from './Button';
import { Select } from './Select';
import { useToast } from '../contexts/ToastContext';
import {
  SPOOL_SORT_FIELDS,
  parseSpoolSortOrder,
  serializeSpoolSortOrder,
  type SpoolSortLevel,
} from '../utils/spoolSort';

/**
 * The operator's spool order: levels of «field + direction», arranged top to
 * bottom. Saved as one setting; the server orders the local inventory by it
 * and the Spoolman lists are ordered by the same chain in the browser.
 */
export function SpoolSortOrderSettings() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();

  const { data: settings, isLoading } = useQuery({
    queryKey: ['ui-preferences'],
    queryFn: api.getUiPreferences,
  });
  const saved = serializeSpoolSortOrder(parseSpoolSortOrder(settings?.spool_sort_order));

  const [levels, setLevels] = useState<SpoolSortLevel[]>([]);
  useEffect(() => {
    setLevels(parseSpoolSortOrder(saved));
  }, [saved]);

  const dirty = serializeSpoolSortOrder(levels) !== saved;
  const unused = SPOOL_SORT_FIELDS.filter((field) => !levels.some((level) => level.field === field));

  const saveMutation = useMutation({
    mutationFn: (value: string) => api.updateSettings({ spool_sort_order: value }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['ui-preferences'] });
      queryClient.invalidateQueries({ queryKey: ['settings'] });
      queryClient.invalidateQueries({ queryKey: ['inventory-spools'] });
      showToast(t('settings.spoolSortOrder.saved'), 'success');
    },
    onError: (err: Error) => showToast(err.message || t('settings.spoolSortOrder.saveFailed'), 'error'),
  });

  const move = (index: number, by: number) => setLevels((prev) => {
    const next = [...prev];
    const target = index + by;
    if (target < 0 || target >= next.length) return prev;
    [next[index], next[target]] = [next[target], next[index]];
    return next;
  });
  const flip = (index: number) => setLevels((prev) => prev.map((level, i) => (
    i === index ? { ...level, direction: level.direction === 'asc' ? 'desc' : 'asc' } : level
  )));
  const setField = (index: number, field: string) => setLevels((prev) => prev.map((level, i) => (
    i === index ? { ...level, field } : level
  )));

  const iconButton = 'p-1.5 rounded text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary disabled:opacity-30 disabled:hover:bg-transparent disabled:hover:text-bambu-gray';

  return (
    <Card>
      <CardHeader>
        <h2 className="text-lg font-semibold text-white">{t('settings.spoolSortOrder.title')}</h2>
        <p className="text-xs text-bambu-gray mt-1">{t('settings.spoolSortOrder.description')}</p>
      </CardHeader>
      <CardContent>
        <div className="space-y-3">
          {levels.length === 0 ? (
            <p className="text-sm text-bambu-gray italic">{t('settings.spoolSortOrder.empty')}</p>
          ) : (
            <ol className="space-y-2">
              {levels.map((level, index) => (
                <li key={level.field} className="flex items-center gap-2">
                  <span className="w-5 text-right text-xs text-bambu-gray tabular-nums">{index + 1}.</span>
                  <Select
                    value={level.field}
                    onChange={(e) => setField(index, e.target.value)}
                    className="min-w-0 flex-1"
                  >
                    {[level.field, ...unused].map((field) => (
                      <option key={field} value={field}>{t(`settings.spoolSortOrder.fields.${field}`)}</option>
                    ))}
                  </Select>
                  <button
                    type="button"
                    onClick={() => flip(index)}
                    className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg bg-bambu-dark border border-bambu-dark-tertiary text-sm text-white hover:border-bambu-green"
                  >
                    {level.direction === 'asc' ? <ArrowDownAZ className="w-4 h-4" /> : <ArrowUpAZ className="w-4 h-4" />}
                    <span className="hidden sm:inline">
                      {t(level.direction === 'asc' ? 'settings.spoolSortOrder.ascending' : 'settings.spoolSortOrder.descending')}
                    </span>
                  </button>
                  <button type="button" onClick={() => move(index, -1)} disabled={index === 0}
                    title={t('settings.spoolSortOrder.moveUp')} aria-label={t('settings.spoolSortOrder.moveUp')} className={iconButton}>
                    <ChevronUp className="w-4 h-4" />
                  </button>
                  <button type="button" onClick={() => move(index, 1)} disabled={index === levels.length - 1}
                    title={t('settings.spoolSortOrder.moveDown')} aria-label={t('settings.spoolSortOrder.moveDown')} className={iconButton}>
                    <ChevronDown className="w-4 h-4" />
                  </button>
                  <button type="button" onClick={() => setLevels((prev) => prev.filter((_, i) => i !== index))}
                    title={t('settings.spoolSortOrder.remove')} aria-label={t('settings.spoolSortOrder.remove')} className={iconButton}>
                    <X className="w-4 h-4" />
                  </button>
                </li>
              ))}
            </ol>
          )}

          {unused.length > 0 && (
            <Select
              value=""
              disabled={isLoading}
              onChange={(e) => {
                const field = e.target.value;
                if (field) setLevels((prev) => [...prev, { field, direction: 'asc' }]);
              }}
              aria-label={t('settings.spoolSortOrder.addLevel')}
              tone="muted"
            >
              <option value="">{t('settings.spoolSortOrder.addLevel')}</option>
              {unused.map((field) => (
                <option key={field} value={field}>{t(`settings.spoolSortOrder.fields.${field}`)}</option>
              ))}
            </Select>
          )}

          <div className="flex items-center gap-2 pt-2">
            <Button
              onClick={() => saveMutation.mutate(serializeSpoolSortOrder(levels))}
              disabled={!dirty || saveMutation.isPending}
              variant="primary"
              size="sm"
            >
              {saveMutation.isPending ? <Loader2 className="w-4 h-4 animate-spin" /> : <Check className="w-4 h-4" />}
              <span className="ml-2">{t('settings.spoolSortOrder.save')}</span>
            </Button>
            <Button onClick={() => setLevels([])} disabled={levels.length === 0} variant="secondary" size="sm">
              {t('settings.spoolSortOrder.clear')}
            </Button>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
