import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api } from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';

/** Counts remain visible independently of inbox subscriptions and live delivery. */
export function CredentialRolloutNotice() {
  const { t } = useTranslation();
  const { isAdmin, hasPermission } = useAuth();
  const allowed = isAdmin && hasPermission('api_keys:read');
  const { data, isError, refetch } = useQuery({
    queryKey: ['credential-rollout'], queryFn: api.getCredentialRolloutStatus,
    enabled: allowed, retry: false,
  });
  if (!allowed) return null;
  if (isError) return <p className="mb-4 text-sm text-bambu-gray">
    {t('credentialRollout.loadFailed')}{' '}
    <button type="button" className="text-bambu-green underline" onClick={() => void refetch()}>{t('common.retry')}</button>
  </p>;
  if (!data?.episode_id) return null;
  return <section aria-label={t('credentialRollout.title')} className="mb-4 rounded-lg border border-amber-500/40 bg-amber-500/10 p-4 space-y-2">
    <h3 className="font-semibold text-white">{t('credentialRollout.title')}</h3>
    <p className="text-sm text-bambu-gray">{t('credentialRollout.counts', {
      owned: data.owned_api_key_count, ownerless: data.ownerless_api_key_count, tokens: data.long_lived_token_count,
    })}</p>
    {data.notification_state === 'pending' && <p className="text-sm text-bambu-gray">{t('credentialRollout.notificationPending')}</p>}
    {data.notification_state === 'delivered' && <p className="text-sm text-bambu-gray">{t('credentialRollout.notificationCompleted')}</p>}
    <p className="text-sm text-bambu-gray">{t('credentialRollout.checklist')}</p>
    <p className="text-sm text-bambu-gray">{t('credentialRollout.safety')}</p>
  </section>;
}

/** Finite local copy only: never display an arbitrary server-provided reason. */
export function RetiredCredentialNotice({ reason }: { reason: string }) {
  const { t } = useTranslation();
  return <div className="mt-1 text-xs text-amber-700 dark:text-amber-300">
    <span className="font-semibold">{t('credentialRollout.needsReissue')}</span>{' — '}
    {t(reason === 'legacy_ownerless_reissue_required' ? 'credentialRollout.ownerless' : 'credentialRollout.legacy')}
  </div>;
}
