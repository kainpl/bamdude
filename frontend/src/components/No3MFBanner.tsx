import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { AlertCircle, ExternalLink, X } from 'lucide-react';
import type { No3MFReason, No3MFWarning } from '../api/client';

/**
 * The Archives banner for prints left without their 3MF, worded by WHY
 * (audit D6, upstream 6564c740).
 *
 * "Switch on Store sent files on external storage" (install step 4) answers
 * only a file that was looked for and not there. A refused file connection, a
 * rejected access code or an unreachable printer each get their own words and
 * their own docs entry — none of them is a slicer setting. ⚠️ There is no
 * "internal storage" variant: both storages are read on every printer that has
 * both, so a file kept in the printer's own memory is never the cause.
 *
 * Each kind is dismissed on its own, and the banner shows the most urgent kind
 * not yet dismissed — closing one must not hide the next behind it.
 */

type BannerKind = 'ftps_refused' | 'auth_rejected' | 'unreachable' | 'generic';

// The generic kind keeps the key the banner used before it knew reasons: whoever
// closed it then closed exactly this advice.
const DISMISS_KEY_GENERIC = 'archiveNo3MFWarningDismissed';
const dismissKey = (kind: BannerKind) =>
  kind === 'generic' ? DISMISS_KEY_GENERIC : `${DISMISS_KEY_GENERIC}.${kind}`;

const DOCS_PATH: Record<BannerKind, string> = {
  ftps_refused: 'reference/troubleshooting/#ftps-cleartext-answer',
  auth_rejected: 'reference/troubleshooting/#wrong-access-code',
  unreachable: 'reference/troubleshooting/#ftps-port-990-blocked',
  generic: 'getting-started/',
};

const TEXT_KEY: Record<BannerKind, string> = {
  ftps_refused: 'ftpsRefused',
  auth_rejected: 'authRejected',
  unreachable: 'unreachable',
  generic: 'notFound',
};

function kindOf(reason: No3MFReason | null): BannerKind {
  return reason === 'ftps_refused' || reason === 'auth_rejected' || reason === 'unreachable' ? reason : 'generic';
}

function isDismissed(kind: BannerKind): boolean {
  try {
    return localStorage.getItem(dismissKey(kind)) === 'true';
  } catch {
    return false;
  }
}

export function No3MFBanner({ warning }: { warning: No3MFWarning | undefined }) {
  const { t, i18n } = useTranslation();
  // Dismissed in this render tree; localStorage carries it to the next visit.
  const [dismissedNow, setDismissedNow] = useState<BannerKind[]>([]);

  if (!warning?.has_fallback) return null;
  const kinds = (warning.reasons.length ? warning.reasons : [warning.reason]).map(kindOf);
  const kind = kinds.find((k) => !dismissedNow.includes(k) && !isDismissed(k));
  if (!kind) return null;

  const dismiss = () => {
    try {
      localStorage.setItem(dismissKey(kind), 'true');
    } catch {
      // Private window / blocked storage: dismissed for this visit only.
    }
    setDismissedNow((prev) => [...prev, kind]);
  };
  const locale = i18n.resolvedLanguage?.startsWith('uk') ? 'uk/' : '';
  const text = TEXT_KEY[kind];

  return (
    <div className="mb-4 rounded-lg border border-amber-300 dark:border-amber-500/30 bg-amber-50 dark:bg-amber-500/10 px-4 py-3 flex items-start gap-3">
      <AlertCircle className="w-5 h-5 text-amber-600 dark:text-amber-400 flex-shrink-0 mt-0.5" />
      <div className="flex-1 min-w-0">
        <div className="text-sm font-medium text-amber-900 dark:text-amber-200">
          {t(`archives.no3mfBanner.${text}.title`)}
        </div>
        <div className="text-xs text-amber-800/90 dark:text-amber-200/80 mt-1">
          {t(`archives.no3mfBanner.${text}.body`)}{' '}
          <a
            href={`https://docs.bamdude.top/${locale}${DOCS_PATH[kind]}`}
            target="_blank"
            rel="noreferrer"
            className="underline hover:text-amber-900 dark:hover:text-amber-100 inline-flex items-center gap-1"
          >
            {t(`archives.no3mfBanner.${text}.docsLink`)}
            <ExternalLink className="w-3 h-3" />
          </a>
        </div>
      </div>
      <button
        onClick={dismiss}
        className="text-amber-800/70 dark:text-amber-200/60 hover:text-amber-900 dark:hover:text-amber-200 flex-shrink-0 p-1 -m-1"
        title={t('archives.no3mfBanner.dismissLabel')}
        aria-label={t('archives.no3mfBanner.dismissLabel')}
      >
        <X className="w-4 h-4" />
      </button>
    </div>
  );
}
