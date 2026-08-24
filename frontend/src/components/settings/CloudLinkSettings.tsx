/**
 * Settings → Network → Cloud Link.
 *
 * Pairing this farm with a BamDude portal, and the four controls that follow
 * from it: the switch, the published-printer allowlist, Unpair, and the audit
 * of what crossed the link.
 *
 * Three shapes here are deliberate and easy to undo by accident:
 *
 * ⚠️ **Every error message is chosen by HTTP STATUS, never by the server's
 * `detail`.** That text is English (this UI is en+uk), and FastAPI's `detail`
 * is a string on our own raises but a pydantic list on a validation failure —
 * so rendering it is both untranslated and shape-dependent. 400 / 404 / 502 /
 * 422 each name a different repair; anything else gets the generic line.
 * ⚠️ In particular a 502 says *the portal refused or is unreachable*: the
 * backend's `network` failure code covers a portal answering 500 and a proxy
 * answering 502 as well as a dead socket, so a message that sends the user to
 * check their router would be wrong most of the time it is shown.
 *
 * ⚠️ **A failed pairing still saved the portal URL.** The backend validates
 * and stores `portal_url` *before* redeeming the code, on purpose — reverting
 * it would make the second attempt silently retry the old portal. So a failed
 * pair with a URL supplied re-reads the status and says which portal is now
 * saved, or the user retypes a code against a portal they cannot see.
 *
 * ⚠️ **The publish set is a draft until saved.** `draftSet === null` means
 * "follow the server", which is what lets the 5-second status poll refresh the
 * card underneath a user who is mid-tick without stealing their checkboxes.
 * The save is all-or-nothing (the backend refuses a partial one) — so a
 * rejection clears nothing locally, and the boxes still show what was asked
 * for.
 */
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  AlertTriangle,
  ChevronDown,
  ChevronRight,
  Cloud,
  CloudOff,
  Link2Off,
  Loader2,
  ShieldOff,
} from 'lucide-react';
import { ApiError, api, type CloudLinkStatus } from '../../api/client';
import { Card, CardContent, CardHeader } from '../Card';
import { ConfirmModal } from '../ConfirmModal';
import { useAuth } from '../../contexts/AuthContext';
import { useToast } from '../../contexts/ToastContext';
import { parseUTCDate } from '../../utils/date';

/** Rows per page of the audit table. The backend caps a page at 100. */
const AUDIT_PAGE_SIZE = 20;

/** How often the panel re-reads the status while the link is running. */
const STATUS_POLL_MS = 5_000;

/** How often the audit table re-reads while the link is up. Slower than the
 *  badge on purpose — it is a log somebody scans, not a state somebody waits
 *  on — but not never: a frozen table beside a live badge reads as a bug. */
const AUDIT_POLL_MS = 15_000;

function formatWhen(iso: string | null): string | null {
  if (!iso) return null;
  // parseUTCDate forces UTC interpretation — a bare `new Date("...T10:00:00")`
  // is parsed as LOCAL time and renders the backend's naive stamps at the
  // wrong offset (#1602 / #504 class).
  const d = parseUTCDate(iso);
  return d ? d.toLocaleString() : null;
}

export function CloudLinkSettings() {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const { showToast } = useToast();
  const qc = useQueryClient();
  const canManage = hasPermission('cloud_link:manage');

  const [pairingCode, setPairingCode] = useState('');
  const [portalUrlInput, setPortalUrlInput] = useState('');
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [pairError, setPairError] = useState<string | null>(null);
  const [savedPortalNotice, setSavedPortalNotice] = useState<string | null>(null);
  const [publishError, setPublishError] = useState<string | null>(null);
  const [draftSet, setDraftSet] = useState<number[] | null>(null);
  const [confirmingUnpair, setConfirmingUnpair] = useState(false);
  const [auditPage, setAuditPage] = useState(1);

  // `enabled` decides whether this query exists at all — gating only
  // `refetchInterval` would still fire the first request for a user who may
  // not ask this question. The interval then polls only while the link is
  // actually running; a link that is off has nothing to watch.
  const statusQuery = useQuery({
    queryKey: ['cloudLink', 'status'],
    queryFn: api.getCloudLinkStatus,
    enabled: canManage,
    refetchInterval: (query) => (query.state.data?.enabled ? STATUS_POLL_MS : false),
  });
  const status = statusQuery.data;
  const paired = !!status?.paired;

  // House rule: `getPrinters()` stays param-free so the many bare
  // `queryFn: api.getPrinters` call sites keep their inference.
  //
  // ⚠️ It excludes ARCHIVED printers and nothing else. `archived` and
  // `is_active` are INDEPENDENT axes — Maintenance Mode parks a printer with
  // `is_active === false` and keeps its card visible — while the backend
  // validates the publish set against `is_active AND NOT archived`. So the
  // picker has to apply the second half itself, or it offers rows whose only
  // possible outcome is a 422 that refuses the WHOLE save.
  const printersQuery = useQuery({
    queryKey: ['printers'],
    queryFn: api.getPrinters,
    enabled: canManage && paired,
  });

  const auditQuery = useQuery({
    queryKey: ['cloudLink', 'audit', auditPage],
    queryFn: () => api.getCloudLinkAudit(auditPage, AUDIT_PAGE_SIZE),
    enabled: canManage && paired,
    refetchInterval: AUDIT_POLL_MS,
  });

  /** Every mutating route answers with the full status — take it rather than
   *  re-fetching, or the card is stale for a round trip and wrong if the
   *  re-fetch fails. */
  const adoptStatus = (next: CloudLinkStatus) => {
    qc.setQueryData(['cloudLink', 'status'], next);
  };

  const statusOf = (err: unknown): number | undefined =>
    err instanceof ApiError ? err.status : undefined;

  const pairErrorMessage = (err: unknown): string => {
    switch (statusOf(err)) {
      case 400:
        return t('cloudLink.errors.badCode');
      case 404:
        return t('cloudLink.errors.unknownCode');
      case 502:
        return t('cloudLink.errors.portalRefused');
      default:
        return t('cloudLink.errors.pairFailed');
    }
  };

  const toggleEnabled = useMutation({
    mutationFn: (next: boolean) => api.setCloudLinkEnabled(next),
    onSuccess: adoptStatus,
    onError: () => showToast(t('cloudLink.errors.toggleFailed'), 'error'),
  });

  const pairMutation = useMutation({
    mutationFn: (vars: { code: string; portalUrl?: string }) =>
      api.pairCloudLink(vars.code, vars.portalUrl),
    onSuccess: (next) => {
      adoptStatus(next);
      setPairingCode('');
      setPairError(null);
      setSavedPortalNotice(null);
      setDraftSet(null);
      qc.invalidateQueries({ queryKey: ['cloudLink', 'audit'] });
      showToast(t('cloudLink.pair.success'), 'success');
    },
    onError: async (err, vars) => {
      setPairError(pairErrorMessage(err));
      setSavedPortalNotice(null);
      // The URL persists by design (see the module docstring) — say where the
      // next attempt will go, read back rather than echoed from the input so
      // it is the value the backend actually kept.
      if (vars.portalUrl) {
        const fresh = await statusQuery.refetch();
        if (fresh.data?.portal_url) {
          setSavedPortalNotice(t('cloudLink.pair.savedPortal', { url: fresh.data.portal_url }));
        }
      }
    },
  });

  const unpairMutation = useMutation({
    mutationFn: () => api.unpairCloudLink(),
    onSuccess: (next) => {
      adoptStatus(next);
      setConfirmingUnpair(false);
      setDraftSet(null);
      setPublishError(null);
      qc.invalidateQueries({ queryKey: ['cloudLink', 'audit'] });
      showToast(t('cloudLink.unpair.done'), 'success');
    },
    onError: () => {
      setConfirmingUnpair(false);
      showToast(t('cloudLink.errors.unpairFailed'), 'error');
    },
  });

  const publishMutation = useMutation({
    mutationFn: (ids: number[]) => api.setCloudLinkPublishSet(ids),
    onSuccess: (next) => {
      adoptStatus(next);
      // Back to following the server now that the two agree.
      setDraftSet(null);
      setPublishError(null);
      showToast(t('cloudLink.publish.saved'), 'success');
    },
    onError: (err) => {
      // 422 names ids in an English sentence we will not render. The repair is
      // the same whichever printer it was: reload and pick again.
      setPublishError(
        statusOf(err) === 422
          ? t('cloudLink.errors.publishRejected')
          : t('cloudLink.errors.publishFailed'),
      );
    },
  });

  if (!canManage) return null;

  const header = (
    <CardHeader>
      <div className="flex items-center gap-2">
        <Cloud className="w-5 h-5 text-blue-600 dark:text-blue-400" />
        <h2 className="text-lg font-semibold text-white">{t('cloudLink.title')}</h2>
      </div>
    </CardHeader>
  );

  if (!status) {
    return (
      <Card id="card-cloud-link" data-testid="cloud-link-card">
        {header}
        <CardContent>
          {statusQuery.isError ? (
            <p className="text-sm text-red-600 dark:text-red-400">
              {t('cloudLink.errors.loadFailed')}
            </p>
          ) : (
            <p className="flex items-center gap-2 text-sm text-bambu-gray">
              <Loader2 className="w-4 h-4 animate-spin" />
              {t('cloudLink.loading')}
            </p>
          )}
        </CardContent>
      </Card>
    );
  }

  // Four fields collapse into one badge, in this order: a revoked credential
  // is not repaired by enabling, an unpaired farm has nothing to enable, and a
  // link that is switched off is not "offline" — it is off on purpose.
  const badge = (() => {
    if (status.revoked) {
      return {
        label: t('cloudLink.status.revokedByPortal'),
        tone: 'bg-red-500/15 text-red-600 dark:text-red-400',
        Icon: ShieldOff,
      };
    }
    if (!status.paired) {
      return {
        label: t('cloudLink.status.notPaired'),
        tone: 'bg-bambu-dark-tertiary text-bambu-gray',
        Icon: Link2Off,
      };
    }
    if (!status.enabled) {
      return {
        label: t('cloudLink.status.disabled'),
        tone: 'bg-bambu-dark-tertiary text-bambu-gray',
        Icon: CloudOff,
      };
    }
    if (status.connected) {
      return {
        label: t('cloudLink.status.connected'),
        tone: 'bg-green-500/15 text-green-600 dark:text-green-400',
        Icon: Cloud,
      };
    }
    if (status.last_error) {
      return {
        label: t('cloudLink.status.error'),
        tone: 'bg-yellow-500/15 text-yellow-700 dark:text-yellow-400',
        Icon: AlertTriangle,
      };
    }
    return {
      label: t('cloudLink.status.offline'),
      tone: 'bg-bambu-dark-tertiary text-bambu-gray',
      Icon: CloudOff,
    };
  })();
  const BadgeIcon = badge.Icon;

  // "Available" is both halves of the backend's definition — see the comment
  // on `printersQuery` above.
  const availablePrinters = (printersQuery.data ?? []).filter(
    (p) => p.is_active && !p.archived,
  );
  const availableIds = new Set(availablePrinters.map((p) => p.id));

  const savedSet = status.published_printer_ids;
  // ⚠️ A saved id outlives the printer it names: publish a printer, then
  // archive it or park it in Maintenance Mode, and its id stays in
  // `published_printer_ids` with no row to render. Left in the selection it
  // would be invisible, ride into every save, and 422 the whole thing forever
  // — while "N of M selected" counted rows that are not on screen. Prune it
  // from the seed the moment the picker has actually loaded (never before, or
  // a slow list would silently clear the selection).
  const seedSet = printersQuery.isSuccess
    ? savedSet.filter((id) => availableIds.has(id))
    : savedSet;
  const selectedSet = draftSet ?? seedSet;
  // Measured against what is SAVED, not against the seed: a pruned seed is a
  // real difference from the stored set, and leaving Save disabled there would
  // show the user a selection they cannot commit.
  const publishDirty =
    selectedSet.length !== savedSet.length ||
    selectedSet.some((id) => !savedSet.includes(id));

  const togglePrinter = (id: number) => {
    setPublishError(null);
    setDraftSet(
      selectedSet.includes(id) ? selectedSet.filter((x) => x !== id) : [...selectedSet, id],
    );
  };

  const audit = auditQuery.data;
  const auditTotalPages = Math.max(1, Math.ceil((audit?.total ?? 0) / AUDIT_PAGE_SIZE));
  const lastConnected = formatWhen(status.last_connected_at);

  return (
    <Card id="card-cloud-link" data-testid="cloud-link-card">
      {header}
      <CardContent className="space-y-5">
        <p className="text-sm text-bambu-gray">{t('cloudLink.description')}</p>

        {/* ── Status ── */}
        <div className="space-y-2">
          <div className="flex items-center gap-2 flex-wrap">
            <span className="text-sm text-bambu-gray">{t('cloudLink.status.label')}</span>
            <span
              data-testid="cloud-link-badge"
              className={`inline-flex items-center gap-1.5 px-2 py-0.5 rounded text-xs font-medium ${badge.tone}`}
            >
              <BadgeIcon className="w-3.5 h-3.5" />
              {badge.label}
            </span>
          </div>
          {status.revoked && (
            <p className="text-xs text-red-600 dark:text-red-400">
              {t('cloudLink.status.revokedHint')}
            </p>
          )}
          <dl className="text-xs text-bambu-gray space-y-1">
            <div className="flex gap-2">
              <dt>{t('cloudLink.status.portal')}</dt>
              <dd className="text-white break-all">{status.portal_url}</dd>
            </div>
            {status.instance_id && (
              <div className="flex gap-2">
                <dt>{t('cloudLink.status.instanceId')}</dt>
                <dd className="text-white break-all">{status.instance_id}</dd>
              </div>
            )}
            <div className="flex gap-2">
              <dt>{t('cloudLink.status.lastConnected')}</dt>
              <dd className="text-white">{lastConnected ?? t('cloudLink.status.never')}</dd>
            </div>
          </dl>
          {/* Free text from the link, not a localised message — labelled as
              such so it never reads as something this UI is saying. */}
          {status.last_error && (
            <p className="text-xs text-yellow-700 dark:text-yellow-400 break-words">
              {t('cloudLink.status.reportedByLink')}: {status.last_error}
            </p>
          )}
        </div>

        {/* ── The switch ── */}
        <div className="flex items-center justify-between gap-4">
          <div>
            <p className="text-white text-sm">{t('cloudLink.enable.label')}</p>
            <p className="text-xs text-bambu-gray">{t('cloudLink.enable.hint')}</p>
          </div>
          <label className="relative inline-flex items-center cursor-pointer flex-shrink-0">
            <input
              type="checkbox"
              className="sr-only peer"
              aria-label={t('cloudLink.enable.toggleAria')}
              checked={status.enabled}
              disabled={toggleEnabled.isPending}
              onChange={(e) => toggleEnabled.mutate(e.target.checked)}
            />
            <div className="w-11 h-6 bg-bambu-dark-tertiary peer-focus:outline-none rounded-full peer peer-checked:after:translate-x-full after:content-[''] after:absolute after:top-[2px] after:left-[2px] after:bg-white after:rounded-full after:h-5 after:w-5 after:transition-all peer-checked:bg-bambu-green" />
          </label>
        </div>

        {/* ── Pairing (unpaired only) ── */}
        {!status.paired && (
          <div className="space-y-3 border-t border-bambu-dark-tertiary pt-4">
            <div>
              <h3 className="text-sm font-semibold text-white">{t('cloudLink.pair.title')}</h3>
              <p className="text-xs text-bambu-gray">{t('cloudLink.pair.hint')}</p>
            </div>
            <form
              className="space-y-3"
              onSubmit={(e) => {
                e.preventDefault();
                const code = pairingCode.trim();
                if (!code) return;
                const url = portalUrlInput.trim();
                pairMutation.mutate({ code, portalUrl: url || undefined });
              }}
            >
              <div>
                <label
                  className="block text-xs text-bambu-gray mb-1"
                  htmlFor="cloud-link-pairing-code"
                >
                  {t('cloudLink.pair.codeLabel')}
                </label>
                <input
                  id="cloud-link-pairing-code"
                  type="text"
                  autoComplete="off"
                  value={pairingCode}
                  onChange={(e) => setPairingCode(e.target.value)}
                  placeholder={t('cloudLink.pair.codePlaceholder')}
                  className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
                />
              </div>

              <button
                type="button"
                onClick={() => setAdvancedOpen((v) => !v)}
                className="flex items-center gap-1 text-xs text-bambu-gray hover:text-white"
              >
                {advancedOpen ? (
                  <ChevronDown className="w-3.5 h-3.5" />
                ) : (
                  <ChevronRight className="w-3.5 h-3.5" />
                )}
                {t('cloudLink.pair.advanced')}
              </button>
              {advancedOpen && (
                <div>
                  <label
                    className="block text-xs text-bambu-gray mb-1"
                    htmlFor="cloud-link-portal-url"
                  >
                    {t('cloudLink.pair.portalUrlLabel')}
                  </label>
                  <input
                    id="cloud-link-portal-url"
                    type="url"
                    autoComplete="off"
                    value={portalUrlInput}
                    onChange={(e) => setPortalUrlInput(e.target.value)}
                    placeholder={status.portal_url}
                    className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
                  />
                  <p className="text-xs text-bambu-gray mt-1">
                    {t('cloudLink.pair.portalUrlHint')}
                  </p>
                </div>
              )}

              <button
                type="submit"
                disabled={pairMutation.isPending || !pairingCode.trim()}
                className="flex items-center gap-2 px-4 py-2 bg-bambu-green text-white rounded-md hover:bg-bambu-green/90 disabled:opacity-50 disabled:cursor-not-allowed text-sm"
              >
                {pairMutation.isPending && <Loader2 className="w-4 h-4 animate-spin" />}
                {pairMutation.isPending ? t('cloudLink.pair.submitting') : t('cloudLink.pair.submit')}
              </button>
            </form>
            {pairError && (
              <p role="alert" className="text-sm text-red-600 dark:text-red-400">
                {pairError}
              </p>
            )}
            {savedPortalNotice && (
              <p className="text-xs text-bambu-gray">{savedPortalNotice}</p>
            )}
          </div>
        )}

        {/* ── Published printers (paired only) ── */}
        {status.paired && (
          <div className="space-y-3 border-t border-bambu-dark-tertiary pt-4">
            <div>
              <h3 className="text-sm font-semibold text-white">{t('cloudLink.publish.title')}</h3>
              <p className="text-xs text-bambu-gray">{t('cloudLink.publish.hint')}</p>
            </div>
            {printersQuery.isLoading ? (
              <p className="flex items-center gap-2 text-sm text-bambu-gray">
                <Loader2 className="w-4 h-4 animate-spin" />
                {t('common.loading')}
              </p>
            ) : availablePrinters.length === 0 ? (
              <p className="text-sm text-bambu-gray italic">{t('cloudLink.publish.empty')}</p>
            ) : (
              <>
                <div className="space-y-1 max-h-56 overflow-y-auto pr-1">
                  {availablePrinters.map((p) => (
                    <label
                      key={p.id}
                      className="flex items-center gap-2 px-2 py-1.5 rounded hover:bg-bambu-dark cursor-pointer"
                    >
                      <input
                        type="checkbox"
                        checked={selectedSet.includes(p.id)}
                        onChange={() => togglePrinter(p.id)}
                        className="accent-bambu-green"
                      />
                      <span className="text-sm text-white truncate">{p.name}</span>
                      <span className="text-xs text-bambu-gray truncate">{p.model || '—'}</span>
                    </label>
                  ))}
                </div>
                <div className="flex items-center justify-between gap-3 flex-wrap">
                  <span className="text-xs text-bambu-gray">
                    {t('cloudLink.publish.selected', {
                      selected: selectedSet.length,
                      total: availablePrinters.length,
                    })}
                  </span>
                  <button
                    type="button"
                    disabled={!publishDirty || publishMutation.isPending}
                    onClick={() => publishMutation.mutate([...selectedSet].sort((a, b) => a - b))}
                    className="flex items-center gap-2 px-3 py-1.5 text-sm rounded-md bg-bambu-green text-white hover:bg-bambu-green/90 disabled:opacity-50 disabled:cursor-not-allowed"
                  >
                    {publishMutation.isPending && <Loader2 className="w-4 h-4 animate-spin" />}
                    {publishMutation.isPending
                      ? t('cloudLink.publish.saving')
                      : t('cloudLink.publish.save')}
                  </button>
                </div>
              </>
            )}
            {publishError && (
              <p role="alert" className="text-sm text-red-600 dark:text-red-400">
                {publishError}
              </p>
            )}
          </div>
        )}

        {/* ── Audit (paired only) ── */}
        {status.paired && (
          <div className="space-y-3 border-t border-bambu-dark-tertiary pt-4">
            <div>
              <h3 className="text-sm font-semibold text-white">{t('cloudLink.audit.title')}</h3>
              <p className="text-xs text-bambu-gray">{t('cloudLink.audit.hint')}</p>
            </div>
            {auditQuery.isError ? (
              <p className="text-sm text-red-600 dark:text-red-400">
                {t('cloudLink.audit.loadError')}
              </p>
            ) : !audit || audit.items.length === 0 ? (
              <p className="text-sm text-bambu-gray italic">{t('cloudLink.audit.empty')}</p>
            ) : (
              <>
                <div className="overflow-x-auto">
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="text-bambu-gray text-left">
                        <th className="py-1 pr-3 font-medium">{t('cloudLink.audit.colTime')}</th>
                        <th className="py-1 pr-3 font-medium">
                          {t('cloudLink.audit.colDirection')}
                        </th>
                        <th className="py-1 pr-3 font-medium">{t('cloudLink.audit.colKind')}</th>
                        <th className="py-1 pr-3 font-medium">{t('cloudLink.audit.colSummary')}</th>
                        <th className="py-1 font-medium">{t('cloudLink.audit.colResult')}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {audit.items.map((row, i) => (
                        <tr
                          key={`${row.ts}-${i}`}
                          className="border-t border-bambu-dark-tertiary align-top"
                        >
                          <td className="py-1 pr-3 text-bambu-gray whitespace-nowrap">
                            {formatWhen(row.ts) ?? row.ts}
                          </td>
                          <td className="py-1 pr-3 text-bambu-gray whitespace-nowrap">
                            {row.direction === 'down'
                              ? t('cloudLink.audit.directionDown')
                              : t('cloudLink.audit.directionUp')}
                          </td>
                          <td className="py-1 pr-3 text-white whitespace-nowrap">{row.kind}</td>
                          {/* Free text the far end supplied — shown as data,
                              never interpreted. */}
                          <td className="py-1 pr-3 text-bambu-gray break-words">{row.summary}</td>
                          <td
                            className={`py-1 whitespace-nowrap ${
                              row.ok
                                ? 'text-green-600 dark:text-green-400'
                                : 'text-red-600 dark:text-red-400'
                            }`}
                          >
                            {row.ok
                              ? t('cloudLink.audit.resultOk')
                              : t('cloudLink.audit.resultFailed')}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <div className="flex items-center justify-between gap-3">
                  <button
                    type="button"
                    disabled={auditPage <= 1}
                    onClick={() => setAuditPage((p) => Math.max(1, p - 1))}
                    className="px-2 py-1 text-xs rounded bg-bambu-dark-tertiary text-white disabled:opacity-40"
                  >
                    {t('cloudLink.audit.prev')}
                  </button>
                  <span className="text-xs text-bambu-gray">
                    {t('cloudLink.audit.page', { page: auditPage, pages: auditTotalPages })}
                  </span>
                  <button
                    type="button"
                    disabled={auditPage >= auditTotalPages}
                    onClick={() => setAuditPage((p) => p + 1)}
                    className="px-2 py-1 text-xs rounded bg-bambu-dark-tertiary text-white disabled:opacity-40"
                  >
                    {t('cloudLink.audit.next')}
                  </button>
                </div>
              </>
            )}
          </div>
        )}

        {/* ── Unpair (paired only) ── */}
        {status.paired && (
          <div className="border-t border-bambu-dark-tertiary pt-4">
            <button
              type="button"
              disabled={unpairMutation.isPending}
              onClick={() => setConfirmingUnpair(true)}
              className="flex items-center gap-2 px-3 py-1.5 text-sm rounded-md text-bambu-gray hover:text-red-600 dark:hover:text-red-400 disabled:opacity-50"
            >
              <Link2Off className="w-4 h-4" />
              {t('cloudLink.unpair.button')}
            </button>
          </div>
        )}
      </CardContent>

      {confirmingUnpair && (
        <ConfirmModal
          variant="danger"
          title={t('cloudLink.unpair.confirmTitle')}
          message={t('cloudLink.unpair.confirmMessage')}
          confirmText={t('cloudLink.unpair.confirmAction')}
          isLoading={unpairMutation.isPending}
          onConfirm={() => unpairMutation.mutate()}
          onCancel={() => setConfirmingUnpair(false)}
        />
      )}
    </Card>
  );
}
