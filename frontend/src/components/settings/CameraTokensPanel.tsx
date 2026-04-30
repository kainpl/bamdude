/**
 * Long-lived camera-stream tokens (#1108).
 *
 * Drop-in panel for Settings → API Keys: list + create + revoke UI for
 * the long-lived counterpart to the 60-minute ephemeral camera-stream
 * token. Designed to live inside an existing card without page chrome of
 * its own.
 *
 * The plaintext token is shown EXACTLY ONCE at create time inside a
 * copy-to-clipboard modal. Listings only ever show metadata.
 */
import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Copy, Plus, Trash2, AlertTriangle } from 'lucide-react';
import { api, type LongLivedToken } from '../../api/client';
import { useToast } from '../../contexts/ToastContext';
import { useAuth } from '../../contexts/AuthContext';

const DEFAULT_LIFETIME_DAYS = 90;
const MAX_LIFETIME_DAYS = 365;

function formatDate(iso: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleString();
}

function isExpired(iso: string | null): boolean {
  if (!iso) return false;
  return new Date(iso).getTime() < Date.now();
}

interface CreateTokenFormProps {
  onCreated: (token: LongLivedToken) => void;
}

function CreateTokenForm({ onCreated }: CreateTokenFormProps) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const [name, setName] = useState('');
  const [days, setDays] = useState<number>(DEFAULT_LIFETIME_DAYS);
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!name.trim()) return;
    setSubmitting(true);
    try {
      const created = await api.createLongLivedToken({
        name: name.trim(),
        expires_in_days: days,
      });
      onCreated(created);
      setName('');
      setDays(DEFAULT_LIFETIME_DAYS);
      showToast(t('cameraTokens.toast.created'));
    } catch (err) {
      showToast(
        err instanceof Error ? err.message : t('cameraTokens.toast.createFailed'),
        'error',
      );
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <form
      onSubmit={handleSubmit}
      className="bg-bambu-dark-secondary rounded-lg p-4 mb-6 border border-bambu-dark-tertiary"
    >
      <h3 className="text-base font-semibold text-white mb-3">
        {t('cameraTokens.create.title')}
      </h3>
      <div className="grid gap-3 md:grid-cols-[1fr_140px_auto]">
        <input
          type="text"
          maxLength={100}
          required
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder={t('cameraTokens.create.namePlaceholder')}
          className="px-3 py-2 bg-bambu-dark rounded-md text-white border border-bambu-dark-tertiary focus:border-bambu-green focus:outline-none"
          aria-label={t('cameraTokens.create.nameLabel')}
        />
        <input
          type="number"
          min={1}
          max={MAX_LIFETIME_DAYS}
          required
          value={days}
          onChange={(e) => {
            const next = Number(e.target.value);
            // Clamp client-side too — backend will also enforce, but a clear
            // hard cap in the input matches the policy and avoids confusing
            // 400s on submit.
            setDays(Math.min(Math.max(next, 1), MAX_LIFETIME_DAYS));
          }}
          className="px-3 py-2 bg-bambu-dark rounded-md text-white border border-bambu-dark-tertiary focus:border-bambu-green focus:outline-none"
          aria-label={t('cameraTokens.create.daysLabel')}
        />
        <button
          type="submit"
          disabled={submitting || !name.trim()}
          className="flex items-center gap-2 px-4 py-2 bg-bambu-green text-white rounded-md hover:bg-bambu-green/90 disabled:opacity-50 disabled:cursor-not-allowed"
        >
          <Plus className="w-4 h-4" />
          {t('cameraTokens.create.submit')}
        </button>
      </div>
      <p className="text-xs text-bambu-gray mt-2">
        {t('cameraTokens.create.hint')}
      </p>
    </form>
  );
}

interface ConfirmRevokeModalProps {
  token: LongLivedToken;
  onConfirm: () => void;
  onCancel: () => void;
}

function ConfirmRevokeModal({ token, onConfirm, onCancel }: ConfirmRevokeModalProps) {
  const { t } = useTranslation();
  return (
    <div
      className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4"
      role="dialog"
      aria-modal="true"
    >
      <div className="bg-bambu-dark-secondary rounded-lg p-6 max-w-md w-full border border-red-500/40">
        <div className="flex items-start gap-3 mb-4">
          <AlertTriangle className="w-6 h-6 text-red-400 flex-shrink-0 mt-0.5" />
          <div>
            <h2 className="text-lg font-semibold text-white">
              {t('cameraTokens.confirmRevoke.title')}
            </h2>
            <p className="text-sm text-bambu-gray mt-1">
              {t('cameraTokens.confirmRevoke.body', { name: token.name })}
            </p>
          </div>
        </div>
        <div className="flex justify-end gap-2">
          <button
            type="button"
            onClick={onCancel}
            className="px-4 py-2 bg-bambu-dark-tertiary text-white rounded-md hover:bg-bambu-dark-tertiary/80"
          >
            {t('cameraTokens.confirmRevoke.cancel')}
          </button>
          <button
            type="button"
            onClick={onConfirm}
            className="px-4 py-2 bg-red-500 text-white rounded-md hover:bg-red-600"
          >
            {t('cameraTokens.confirmRevoke.confirm')}
          </button>
        </div>
      </div>
    </div>
  );
}

interface JustCreatedModalProps {
  token: LongLivedToken;
  onClose: () => void;
}

function JustCreatedModal({ token, onClose }: JustCreatedModalProps) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const plaintext = token.token ?? '';

  const handleCopy = async () => {
    if (!plaintext) return;
    try {
      // Modern clipboard API requires a secure context (HTTPS or localhost).
      // Fall back to a hidden textarea + execCommand so users on plain HTTP
      // (LAN deployments) can still copy the token.
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(plaintext);
      } else {
        const ta = document.createElement('textarea');
        ta.value = plaintext;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        try {
          ta.select();
          document.execCommand('copy');
        } finally {
          document.body.removeChild(ta);
        }
      }
      showToast(t('cameraTokens.toast.copied'));
    } catch {
      showToast(t('cameraTokens.toast.copyFailed'), 'error');
    }
  };

  return (
    <div className="fixed inset-0 bg-black/60 flex items-center justify-center z-50 p-4">
      <div className="bg-bambu-dark-secondary rounded-lg p-6 max-w-2xl w-full border border-bambu-green/40">
        <div className="flex items-start gap-3 mb-4">
          <AlertTriangle className="w-6 h-6 text-yellow-400 flex-shrink-0 mt-0.5" />
          <div>
            <h2 className="text-lg font-semibold text-white">
              {t('cameraTokens.created.title')}
            </h2>
            <p className="text-sm text-bambu-gray mt-1">
              {t('cameraTokens.created.warning')}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2 mb-4">
          <code className="flex-1 px-3 py-2 bg-bambu-dark rounded-md text-bambu-green text-xs break-all font-mono select-all">
            {plaintext}
          </code>
          <button
            type="button"
            onClick={handleCopy}
            className="flex items-center gap-2 px-3 py-2 bg-bambu-green text-white rounded-md hover:bg-bambu-green/90"
          >
            <Copy className="w-4 h-4" />
            {t('cameraTokens.created.copy')}
          </button>
        </div>
        <div className="flex justify-end">
          <button
            type="button"
            onClick={onClose}
            className="px-4 py-2 bg-bambu-dark-tertiary text-white rounded-md hover:bg-bambu-dark-tertiary/80"
          >
            {t('cameraTokens.created.dismiss')}
          </button>
        </div>
      </div>
    </div>
  );
}

interface TokenRowProps {
  token: LongLivedToken;
  showOwner?: boolean;
  ownerLabel?: string;
  onRevoke: (id: number) => void;
}

function TokenRow({ token, showOwner, ownerLabel, onRevoke }: TokenRowProps) {
  const { t } = useTranslation();
  const expired = isExpired(token.expires_at);
  return (
    <tr className="border-b border-bambu-dark-tertiary last:border-b-0">
      <td className="py-3 px-3 text-white">{token.name}</td>
      {showOwner && <td className="py-3 px-3 text-bambu-gray">{ownerLabel}</td>}
      <td className="py-3 px-3 text-bambu-gray font-mono text-xs">{token.lookup_prefix}…</td>
      <td className="py-3 px-3 text-bambu-gray">{formatDate(token.created_at)}</td>
      <td className={`py-3 px-3 ${expired ? 'text-red-400' : 'text-bambu-gray'}`}>
        {formatDate(token.expires_at)}
        {expired && (
          <span className="ml-2 px-2 py-0.5 text-xs bg-red-500/20 text-red-300 rounded">
            {t('cameraTokens.list.expired')}
          </span>
        )}
      </td>
      <td className="py-3 px-3 text-bambu-gray">{formatDate(token.last_used_at)}</td>
      <td className="py-3 px-3 text-right">
        <button
          type="button"
          onClick={() => onRevoke(token.id)}
          className="inline-flex items-center gap-1 px-2 py-1 text-sm text-red-400 hover:text-red-300"
          title={t('cameraTokens.list.revoke')}
        >
          <Trash2 className="w-4 h-4" />
          {t('cameraTokens.list.revoke')}
        </button>
      </td>
    </tr>
  );
}

interface TokenTableProps {
  tokens: LongLivedToken[];
  showOwner?: boolean;
  userIdToName?: Map<number, string>;
  onRevoke: (id: number) => void;
  emptyMessage: string;
}

function TokenTable({ tokens, showOwner, userIdToName, onRevoke, emptyMessage }: TokenTableProps) {
  const { t } = useTranslation();
  if (tokens.length === 0) {
    return <p className="text-sm text-bambu-gray italic">{emptyMessage}</p>;
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="text-bambu-gray text-left border-b border-bambu-dark-tertiary">
          <tr>
            <th className="py-2 px-3 font-medium">{t('cameraTokens.list.name')}</th>
            {showOwner && <th className="py-2 px-3 font-medium">{t('cameraTokens.list.owner')}</th>}
            <th className="py-2 px-3 font-medium">{t('cameraTokens.list.prefix')}</th>
            <th className="py-2 px-3 font-medium">{t('cameraTokens.list.created')}</th>
            <th className="py-2 px-3 font-medium">{t('cameraTokens.list.expires')}</th>
            <th className="py-2 px-3 font-medium">{t('cameraTokens.list.lastUsed')}</th>
            <th className="py-2 px-3" />
          </tr>
        </thead>
        <tbody>
          {tokens.map((tok) => (
            <TokenRow
              key={tok.id}
              token={tok}
              showOwner={showOwner}
              ownerLabel={userIdToName?.get(tok.user_id) ?? `#${tok.user_id}`}
              onRevoke={onRevoke}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * The actual UI block: create form + my-tokens table + admin all-tokens table.
 * Renders without any outer page chrome so it can be embedded inside
 * Settings → API Keys (the canonical home).
 */
export default function CameraTokensPanel() {
  const { t } = useTranslation();
  const { user, isAdmin } = useAuth();
  const { showToast } = useToast();

  const [myTokens, setMyTokens] = useState<LongLivedToken[]>([]);
  const [allTokens, setAllTokens] = useState<LongLivedToken[]>([]);
  const [userIdToName, setUserIdToName] = useState<Map<number, string>>(new Map());
  const [loading, setLoading] = useState(true);
  const [justCreated, setJustCreated] = useState<LongLivedToken | null>(null);
  const [pendingRevoke, setPendingRevoke] = useState<LongLivedToken | null>(null);

  const refresh = async () => {
    setLoading(true);
    try {
      const mine = await api.getLongLivedTokens();
      setMyTokens(mine);
      if (isAdmin) {
        const all = await api.getAllLongLivedTokens();
        setAllTokens(all);
        // Username lookup: best-effort. If it errors, the table still renders
        // with the numeric user_id as fallback.
        try {
          const users = await api.getUsers();
          setUserIdToName(new Map(users.map((u) => [u.id, u.username])));
        } catch {
          setUserIdToName(new Map());
        }
      }
    } catch (err) {
      showToast(
        err instanceof Error ? err.message : t('cameraTokens.toast.loadFailed'),
        'error',
      );
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isAdmin]);

  // Open the confirmation modal. The actual delete fires from
  // ``confirmRevoke`` once the user clicks through.
  const requestRevoke = (id: number) => {
    const target = [...myTokens, ...allTokens].find((tok) => tok.id === id);
    if (target) {
      setPendingRevoke(target);
    }
  };

  const confirmRevoke = async () => {
    if (!pendingRevoke) return;
    const id = pendingRevoke.id;
    setPendingRevoke(null);
    try {
      await api.revokeLongLivedToken(id);
      showToast(t('cameraTokens.toast.revoked'));
      await refresh();
    } catch (err) {
      showToast(
        err instanceof Error ? err.message : t('cameraTokens.toast.revokeFailed'),
        'error',
      );
    }
  };

  const otherUsersTokens = useMemo(
    () => allTokens.filter((tok) => tok.user_id !== user?.id),
    [allTokens, user?.id],
  );

  return (
    <>
      <p className="text-sm text-bambu-gray mb-4">
        {t('cameraTokens.description')}
      </p>

      <CreateTokenForm
        onCreated={(token) => {
          setJustCreated(token);
          void refresh();
        }}
      />

      <div className="mb-6">
        <h3 className="text-base font-semibold text-white mb-3">
          {t('cameraTokens.list.myTitle')}
        </h3>
        {loading ? (
          <p className="text-sm text-bambu-gray">{t('cameraTokens.loading')}</p>
        ) : (
          <TokenTable
            tokens={myTokens}
            onRevoke={requestRevoke}
            emptyMessage={t('cameraTokens.list.empty')}
          />
        )}
      </div>

      {isAdmin && (
        <div>
          <h3 className="text-base font-semibold text-white mb-3">
            {t('cameraTokens.list.allTitle')}
          </h3>
          <TokenTable
            tokens={otherUsersTokens}
            showOwner
            userIdToName={userIdToName}
            onRevoke={requestRevoke}
            emptyMessage={t('cameraTokens.list.empty')}
          />
        </div>
      )}

      {justCreated && (
        <JustCreatedModal token={justCreated} onClose={() => setJustCreated(null)} />
      )}

      {pendingRevoke && (
        <ConfirmRevokeModal
          token={pendingRevoke}
          onConfirm={() => void confirmRevoke()}
          onCancel={() => setPendingRevoke(null)}
        />
      )}
    </>
  );
}
