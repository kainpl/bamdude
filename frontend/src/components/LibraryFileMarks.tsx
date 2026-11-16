/**
 * The small marks a file's card or row carries for what its details hold: the
 * operator's own link, opened apart from the app, and how many photos are
 * attached — which opens the details, where they are.
 *
 * A mark exists only when there is something behind it — a file without a link
 * and without photos shows nothing.
 */
import { useTranslation } from 'react-i18next';
import { Camera, Globe } from 'lucide-react';
import { isHttpLink } from '../utils/httpLink';

interface Props {
  file: { external_url?: string | null; photos_count?: number };
  /** Open the file's details — what the photo mark does. Without it the count is shown, not clickable. */
  onPhotos?: () => void;
  /** `overlay` — a pill over the card's thumbnail; `inline` — beside a row's actions. */
  variant: 'overlay' | 'inline';
}

export function LibraryFileMarks({ file, variant, onPhotos }: Props) {
  const { t } = useTranslation();
  const photos = file.photos_count ?? 0;
  const link = isHttpLink(file.external_url) ? file.external_url : null;
  if (!link && photos <= 0) return null;
  const markClass =
    variant === 'overlay'
      ? 'rounded-md bg-bambu-dark/80 backdrop-blur text-bambu-gray hover:text-bambu-green hover:bg-bambu-dark transition-colors flex items-center'
      : 'p-1.5 rounded transition-colors text-bambu-gray hover:text-bambu-green hover:bg-bambu-dark flex items-center';
  const iconClass = variant === 'overlay' ? 'w-5 h-5' : 'w-4 h-4';
  const photosLabel = t('fileDetails.photos.count', { count: photos });
  return (
    <>
      {link && (
        <a
          href={link}
          target="_blank"
          rel="noopener noreferrer"
          onClick={(e) => e.stopPropagation()}
          aria-label={t('fileDetails.openLink')}
          title={link}
          className={markClass}
        >
          <Globe className={iconClass} />
        </a>
      )}
      {photos > 0 &&
        (onPhotos ? (
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              onPhotos();
            }}
            aria-label={photosLabel}
            title={photosLabel}
            className={markClass}
          >
            <Camera className={iconClass} />
            <span className="ml-1 text-xs font-medium tabular-nums">{photos}</span>
          </button>
        ) : (
          <span aria-label={photosLabel} title={photosLabel} className={markClass}>
            <Camera className={iconClass} />
            <span className="ml-1 text-xs font-medium tabular-nums">{photos}</span>
          </span>
        ))}
    </>
  );
}
