/**
 * The small marks a file's card or row carries for what its details hold: the
 * operator's own link, opened apart from the app.
 *
 * A mark exists only when there is something behind it — a file without a link
 * shows nothing.
 */
import { useTranslation } from 'react-i18next';
import { Globe } from 'lucide-react';
import { isHttpLink } from '../utils/httpLink';

interface Props {
  file: { external_url?: string | null };
  /** `overlay` — a pill over the card's thumbnail; `inline` — beside a row's actions. */
  variant: 'overlay' | 'inline';
}

export function LibraryFileMarks({ file, variant }: Props) {
  const { t } = useTranslation();
  if (!isHttpLink(file.external_url)) return null;
  const markClass =
    variant === 'overlay'
      ? 'rounded-md bg-bambu-dark/80 backdrop-blur text-bambu-gray hover:text-bambu-green hover:bg-bambu-dark transition-colors flex items-center'
      : 'p-1.5 rounded transition-colors text-bambu-gray hover:text-bambu-green hover:bg-bambu-dark flex items-center';
  const iconClass = variant === 'overlay' ? 'w-5 h-5' : 'w-4 h-4';
  return (
    <a
      href={file.external_url}
      target="_blank"
      rel="noopener noreferrer"
      onClick={(e) => e.stopPropagation()}
      aria-label={t('fileDetails.openLink')}
      title={file.external_url}
      className={markClass}
    >
      <Globe className={iconClass} />
    </a>
  );
}
