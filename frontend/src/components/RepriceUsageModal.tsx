import { useId, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Coins, Loader2 } from 'lucide-react';
import { api, type UsageRepriceResult } from '../api/client';
import { Button } from './Button';
import { Modal } from './Modal';

const FIELD_CLASS =
  'w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none';

interface RepriceUsageModalProps {
  /** The spool to re-price, when the dialog is opened for one. */
  spool?: { id: number; name: string };
  currency: string;
  onClose: () => void;
  /** After a re-pricing that changed something. */
  onDone: (result: UsageRepriceResult) => void;
}

/**
 * Re-price recorded usage — the correction door for a price typed wrong.
 *
 * A print's cost is a snapshot, so nothing here happens by itself: the operator
 * names one spool and / or one period, asks for a preview, reads how many
 * prints and orders would move, and only then applies. Any change to the
 * selection throws the preview away, so what is applied is what was shown.
 */
export function RepriceUsageModal({ spool, currency, onClose, onDone }: RepriceUsageModalProps) {
  const { t } = useTranslation();
  const ids = { spool: useId(), from: useId(), to: useId() };
  const [spoolId, setSpoolId] = useState(spool ? String(spool.id) : '');
  const [dateFrom, setDateFrom] = useState('');
  const [dateTo, setDateTo] = useState('');
  const [preview, setPreview] = useState<UsageRepriceResult | null>(null);
  const [busy, setBusy] = useState<'preview' | 'apply' | null>(null);
  const [error, setError] = useState<string | null>(null);

  const parsedSpool = /^\d+$/.test(spoolId.trim()) ? Number(spoolId.trim()) : null;
  const spoolInvalid = spoolId.trim() !== '' && parsedSpool === null;
  const hasPeriod = dateFrom !== '' && dateTo !== '';
  const halfPeriod = (dateFrom !== '') !== (dateTo !== '');
  const backwards = hasPeriod && dateTo < dateFrom;
  const scoped = (parsedSpool !== null || hasPeriod) && !spoolInvalid && !halfPeriod && !backwards;

  const reset = <T,>(set: (value: T) => void) => (value: T) => {
    set(value);
    setPreview(null);
    setError(null);
  };

  const body = (dryRun: boolean) => ({
    spool_id: parsedSpool,
    date_from: hasPeriod ? dateFrom : null,
    date_to: hasPeriod ? dateTo : null,
    dry_run: dryRun,
  });

  const run = async (kind: 'preview' | 'apply') => {
    setBusy(kind);
    setError(null);
    try {
      const result = await api.repriceUsage(body(kind === 'preview'));
      if (kind === 'preview') {
        setPreview(result);
      } else {
        onDone(result);
        onClose();
      }
    } catch (e) {
      setError(e instanceof Error && e.message ? e.message : t('stats.reprice.failed'));
    } finally {
      setBusy(null);
    }
  };

  const money = (value: number) => `${currency}${value.toFixed(2)}`;
  const hint = spoolInvalid
    ? t('stats.reprice.spoolInvalid')
    : halfPeriod
      ? t('stats.reprice.periodIncomplete')
      : backwards
        ? t('stats.reprice.periodBackwards')
        : !scoped
          ? t('stats.reprice.scopeRequired')
          : null;

  return (
    <Modal
      onClose={onClose}
      title={t('stats.reprice.title')}
      icon={<Coins className="w-5 h-5 text-bambu-green" />}
      size="md"
      closeDisabled={busy !== null}
      alert={error ? <p className="px-4 py-2 text-sm text-red-400" role="alert">{error}</p> : undefined}
      footer={
        <div className="flex gap-3 p-4">
          <Button variant="secondary" className="flex-1" onClick={onClose} disabled={busy !== null}>
            {t('common.cancel')}
          </Button>
          <Button variant="outline" className="flex-1" onClick={() => run('preview')} disabled={!scoped || busy !== null}>
            {busy === 'preview' && <Loader2 className="w-4 h-4 animate-spin" />}
            {t('stats.reprice.preview')}
          </Button>
          <Button
            className="flex-1"
            onClick={() => run('apply')}
            disabled={!preview || preview.rows_changed === 0 || busy !== null}
            title={!preview ? t('stats.reprice.previewFirst') : undefined}
          >
            {busy === 'apply' && <Loader2 className="w-4 h-4 animate-spin" />}
            {t('stats.reprice.apply')}
          </Button>
        </div>
      }
    >
      <div className="p-4 space-y-4">
        <p className="text-sm text-bambu-gray">{t('stats.reprice.intro')}</p>

        {spool ? (
          <p className="text-sm text-white">
            <span className="text-bambu-gray">{t('stats.reprice.spool')}: </span>
            {spool.name} <span className="text-bambu-gray">#{spool.id}</span>
          </p>
        ) : (
          <div>
            <label htmlFor={ids.spool} className="block text-sm text-bambu-gray mb-1">
              {t('stats.reprice.spoolNumber')}
            </label>
            <input
              id={ids.spool}
              inputMode="numeric"
              value={spoolId}
              onChange={(e) => reset(setSpoolId)(e.target.value)}
              placeholder={t('stats.reprice.spoolNumberPlaceholder')}
              className={FIELD_CLASS}
              disabled={busy !== null}
            />
          </div>
        )}

        <div className="grid grid-cols-2 gap-3">
          <div>
            <label htmlFor={ids.from} className="block text-sm text-bambu-gray mb-1">
              {t('stats.reprice.dateFrom')}
            </label>
            <input
              id={ids.from}
              type="date"
              value={dateFrom}
              onChange={(e) => reset(setDateFrom)(e.target.value)}
              className={FIELD_CLASS}
              disabled={busy !== null}
            />
          </div>
          <div>
            <label htmlFor={ids.to} className="block text-sm text-bambu-gray mb-1">
              {t('stats.reprice.dateTo')}
            </label>
            <input
              id={ids.to}
              type="date"
              value={dateTo}
              onChange={(e) => reset(setDateTo)(e.target.value)}
              className={FIELD_CLASS}
              disabled={busy !== null}
            />
          </div>
        </div>

        {hint && <p className="text-xs text-bambu-gray">{hint}</p>}

        {preview && (
          <div className="rounded-lg border border-bambu-dark-tertiary bg-bambu-dark p-3 text-sm text-white space-y-1" role="status">
            {preview.rows_changed === 0 ? (
              <p>{t('stats.reprice.nothingToChange', { rows: preview.rows })}</p>
            ) : (
              <>
                <p>{t('stats.reprice.rows', { changed: preview.rows_changed, rows: preview.rows })}</p>
                <p>{t('stats.reprice.archives', { count: preview.archives_changed })}</p>
                <p>{t('stats.reprice.orders', { count: preview.orders_changed })}</p>
                <p>
                  {t('stats.reprice.total', { before: money(preview.cost_before), after: money(preview.cost_after) })}
                </p>
              </>
            )}
          </div>
        )}
      </div>
    </Modal>
  );
}
