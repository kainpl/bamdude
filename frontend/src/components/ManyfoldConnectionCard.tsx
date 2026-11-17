import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { CheckCircle2, Library, Loader2, XCircle } from 'lucide-react';

import { api, ApiError, type ManyfoldConnectionDraft, type ManyfoldConnectionTest, type SecretAction } from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { useToast } from '../contexts/ToastContext';
import { credentialDraft } from '../utils/credentialDraft';
import { Button } from './Button';
import { Card, CardContent, CardHeader } from './Card';
import { SecretInput } from './SecretInput';

const SECRET_KEY = 'manyfold_client_secret';
const inputClass =
  'w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white disabled:opacity-50';

/**
 * The installation's one Manyfold connection (Settings → Network).
 *
 * The client secret is write-only: the form shows whether one is stored and
 * offers keep / replace / clear, never the value. A kept secret belongs to the
 * address and client ID it was entered for — the server refuses to keep it
 * when either changes, here and in "Test connection".
 */
export function ManyfoldConnectionCard() {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const canEdit = hasPermission('settings:update');

  const saved = useQuery({ queryKey: ['manyfold-connection'], queryFn: () => api.getManyfoldConnection() });
  const [enabled, setEnabled] = useState(false);
  const [url, setUrl] = useState('');
  const [clientId, setClientId] = useState('');
  const [secret, setSecret] = useState('');
  const [secretAction, setSecretAction] = useState<SecretAction>('keep');
  const [tested, setTested] = useState<ManyfoldConnectionTest | null>(null);

  useEffect(() => {
    if (!saved.data) return;
    setEnabled(saved.data.enabled);
    setUrl(saved.data.url);
    setClientId(saved.data.client_id);
    setSecret('');
    setSecretAction(saved.data.client_secret_configured ? 'keep' : 'replace');
  }, [saved.data]);

  const draft = (): ManyfoldConnectionDraft => {
    // "Replace" with nothing typed, where nothing is stored, is no change — not an empty replacement.
    const action = secretAction === 'replace' && !secret && !saved.data?.client_secret_configured ? 'keep' : secretAction;
    const credential = credentialDraft(SECRET_KEY, secret, action, saved.data?.client_secret_configured);
    const body: ManyfoldConnectionDraft = { enabled, url: url.trim(), client_id: clientId.trim() };
    if (credential[SECRET_KEY] !== undefined) body.client_secret = credential[SECRET_KEY];
    if (credential.secret_actions) body.secret_actions = credential.secret_actions;
    return body;
  };
  const refusal = (error: unknown) =>
    error instanceof ApiError && error.code
      ? t(`manyfold.errors.${error.code}`, { defaultValue: error.message })
      : error instanceof Error
        ? error.message
        : t('manyfold.errors.manyfold_failed');

  const save = useMutation({
    mutationFn: () => api.saveManyfoldConnection(draft()),
    onSuccess: (connection) => {
      queryClient.setQueryData(['manyfold-connection'], connection);
      // The secret is written: let go of it here, not when the cached answer changes —
      // an answer equal to the cached one (only the secret was replaced) changes nothing there.
      setSecret('');
      setSecretAction(connection.client_secret_configured ? 'keep' : 'replace');
      queryClient.invalidateQueries({ queryKey: ['manyfold-status'] });
      queryClient.invalidateQueries({ queryKey: ['manyfold-models'] });
      setTested(null);
      showToast(t('manyfold.connectionSaved'), 'success');
    },
    onError: (error) => showToast(refusal(error), 'error'),
  });
  const test = useMutation({
    mutationFn: () => api.testManyfoldConnection(draft()),
    onSuccess: setTested,
    onError: (error) => setTested({ ok: false, models: null, error: null, message: refusal(error) }),
  });

  const pending = save.isPending || test.isPending;

  return (
    <Card>
      <CardHeader>
        <h2 className="text-lg font-semibold text-white flex items-center gap-2">
          <Library className="w-5 h-5 text-bambu-green" />
          {t('manyfold.connectionTitle')}
        </h2>
      </CardHeader>
      <CardContent className="space-y-4">
        <p className="text-sm text-bambu-gray">{t('manyfold.connectionHelp')}</p>
        <p className="text-xs text-bambu-gray">{t('manyfold.connectionTls')}</p>

        <label className="flex items-center justify-between gap-3">
          <span className="text-white">{t('manyfold.enabled')}</span>
          <input
            type="checkbox"
            role="switch"
            checked={enabled}
            disabled={!canEdit || pending}
            onChange={(event) => setEnabled(event.target.checked)}
          />
        </label>

        <div>
          <label htmlFor="manyfold-url" className="block text-sm text-bambu-gray mb-1">{t('manyfold.url')}</label>
          <input
            id="manyfold-url"
            type="text"
            inputMode="url"
            autoComplete="off"
            spellCheck={false}
            maxLength={300}
            value={url}
            placeholder={t('manyfold.urlPlaceholder')}
            disabled={!canEdit || pending}
            onChange={(event) => setUrl(event.target.value)}
            className={inputClass}
          />
        </div>
        <div>
          <label htmlFor="manyfold-client-id" className="block text-sm text-bambu-gray mb-1">{t('manyfold.clientId')}</label>
          <input
            id="manyfold-client-id"
            type="text"
            autoComplete="off"
            spellCheck={false}
            maxLength={255}
            value={clientId}
            disabled={!canEdit || pending}
            onChange={(event) => setClientId(event.target.value)}
            className={inputClass}
          />
        </div>
        <div>
          <p className="block text-sm text-bambu-gray mb-1">{t('manyfold.clientSecret')}</p>
          <SecretInput
            value={secret}
            action={secretAction}
            configured={saved.data?.client_secret_configured}
            disabled={!canEdit || pending}
            onChange={setSecret}
            onActionChange={setSecretAction}
          />
        </div>

        {tested && (
          <p role="status" className={`text-sm flex items-start gap-2 ${tested.ok ? 'text-bambu-green' : 'text-red-400'}`}>
            {tested.ok ? <CheckCircle2 className="w-4 h-4 mt-0.5 shrink-0" /> : <XCircle className="w-4 h-4 mt-0.5 shrink-0" />}
            <span>
              {tested.ok
                ? tested.models === null
                  ? t('manyfold.testOkUnknown')
                  : t('manyfold.testOk', { count: tested.models })
                : tested.error
                  ? t(`manyfold.errors.${tested.error}`, { defaultValue: tested.message ?? '' })
                  : tested.message}
            </span>
          </p>
        )}

        {canEdit && (
          <div className="flex flex-wrap gap-2">
            <Button variant="secondary" size="sm" disabled={pending || !url.trim()} onClick={() => test.mutate()}>
              {test.isPending && <Loader2 className="w-4 h-4 mr-1 animate-spin" />}
              {t('manyfold.test')}
            </Button>
            <Button size="sm" disabled={pending} onClick={() => save.mutate()}>
              {save.isPending && <Loader2 className="w-4 h-4 mr-1 animate-spin" />}
              {t('manyfold.saveConnection')}
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
