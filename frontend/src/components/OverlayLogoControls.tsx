/**
 * The stream overlay's logo, inside the overlay builder.
 *
 * ONE logo per installation: it is the farm's branding, not a choice of the
 * address. What the address chooses is only whether to draw it (`logo=1`), and
 * that checkbox is one of the builder's remembered choices.
 *
 * Uploading, replacing and removing need `settings:update`; without it the
 * controls are absent and a line says why. The server never keeps a file as
 * sent — it decodes PNG, JPEG or WebP and stores the pixels alone — so the
 * preview shows what was saved, read back through the session.
 */
import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Trash2, Upload } from 'lucide-react';
import { api, type OverlayLogoInfo } from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { useToast } from '../contexts/ToastContext';
import { useOverlayLogo } from '../hooks/useOverlayLogo';
import { ConfirmModal } from './ConfirmModal';

/** The formats the server decodes. An SVG is script and is refused there too. */
const ACCEPTED = 'image/png,image/jpeg,image/webp';

interface OverlayLogoControlsProps {
  showLogo: boolean;
  onShowLogoChange: (show: boolean) => void;
  /** The saved logo changed (or was first read): an open preview should follow. */
  onRevisionChange?: (revision: string | null) => void;
}

export function OverlayLogoControls({ showLogo, onShowLogoChange, onRevisionChange }: OverlayLogoControlsProps) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const { hasPermission } = useAuth();
  const mayChange = hasPermission('settings:update');

  // Null until the server has answered: nothing is said about a logo before that.
  const [info, setInfo] = useState<OverlayLogoInfo | null>(null);
  const [busy, setBusy] = useState(false);
  const [removing, setRemoving] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  const adopt = (next: OverlayLogoInfo) => {
    setInfo(next);
    onRevisionChange?.(next.revision);
  };
  const adoptRef = useRef(adopt);
  adoptRef.current = adopt;

  useEffect(() => {
    let cancelled = false;
    api.getOverlayLogoInfo().then(
      (answer) => {
        if (!cancelled) adoptRef.current(answer);
      },
      () => {
        // Not allowed to ask, or the server is away: the checkbox still works,
        // only the preview and the hint are missing.
      },
    );
    return () => {
      cancelled = true;
    };
  }, []);

  const saved = useOverlayLogo(info?.revision, null);

  const upload = async (file: File | undefined) => {
    if (!file || busy) return;
    setBusy(true);
    try {
      adopt(await api.uploadOverlayLogo(file));
      showToast(t('streamOverlay.builder.logoSaved', 'Logo saved'));
    } catch (err) {
      // The server's own sentence: it says what was wrong with the file.
      showToast(err instanceof Error ? err.message : t('streamOverlay.builder.logoFailed', 'The logo could not be saved'), 'error');
    } finally {
      setBusy(false);
      // The same file can be chosen again after a refusal.
      if (fileInput.current) fileInput.current.value = '';
    }
  };

  const remove = async () => {
    setRemoving(false);
    setBusy(true);
    try {
      adopt(await api.deleteOverlayLogo());
      showToast(t('streamOverlay.builder.logoRemoved', 'Logo removed'));
    } catch (err) {
      showToast(err instanceof Error ? err.message : t('streamOverlay.builder.logoFailed', 'The logo could not be saved'), 'error');
    } finally {
      setBusy(false);
    }
  };

  const exists = info?.exists === true;
  const uploadLabel = exists
    ? t('streamOverlay.builder.logoReplace', 'Replace the logo')
    : t('streamOverlay.builder.logoUpload', 'Upload a logo');

  return (
    <fieldset className="mt-4">
      <legend className="text-sm font-medium text-white mb-2">
        {t('streamOverlay.builder.logoTitle', 'Logo')}
      </legend>
      <div className="flex flex-wrap items-center gap-3">
        {saved && (
          // A dark ground: the overlay draws the logo over the picture or a dark panel.
          <div className="flex h-16 items-center rounded-md border border-bambu-dark-tertiary bg-black px-3">
            <img
              data-overlay-logo-preview=""
              src={saved.src}
              onError={saved.onError}
              alt={t('streamOverlay.builder.logoPreviewAlt', 'The saved logo')}
              className="max-h-12 max-w-48 object-contain"
            />
          </div>
        )}
        {mayChange && (
          <>
            <input
              ref={fileInput}
              id="overlay-builder-logo-file"
              type="file"
              accept={ACCEPTED}
              aria-label={uploadLabel}
              disabled={busy}
              onChange={(e) => void upload(e.target.files?.[0])}
              className="sr-only"
            />
            <button
              type="button"
              disabled={busy}
              onClick={() => fileInput.current?.click()}
              className="flex items-center gap-2 rounded-md bg-bambu-dark-tertiary px-3 py-2 text-sm text-white hover:bg-bambu-dark-tertiary/80 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <Upload className="w-4 h-4" aria-hidden="true" />
              <span aria-hidden="true">{uploadLabel}</span>
            </button>
            {exists && (
              <button
                type="button"
                disabled={busy}
                onClick={() => setRemoving(true)}
                className="flex items-center gap-2 rounded-md bg-bambu-dark-tertiary px-3 py-2 text-sm text-white hover:bg-bambu-dark-tertiary/80 disabled:cursor-not-allowed disabled:opacity-50"
              >
                <Trash2 className="w-4 h-4" aria-hidden="true" />
                {t('streamOverlay.builder.logoRemove', 'Remove logo')}
              </button>
            )}
          </>
        )}
      </div>
      <label className="mt-3 flex items-center gap-2 text-sm text-bambu-gray">
        <input
          type="checkbox"
          checked={showLogo}
          onChange={(e) => onShowLogoChange(e.target.checked)}
          className="accent-bambu-green"
        />
        {t('streamOverlay.builder.logoShow', 'Show the logo on the overlay')}
      </label>
      {info && !exists && (
        <p className="text-xs text-bambu-gray mt-2">
          {t(
            'streamOverlay.builder.logoNone',
            'No logo is saved yet. An overlay asked to show it draws none until one is uploaded.',
          )}
        </p>
      )}
      <p className="text-xs text-bambu-gray mt-2">
        {t(
          'streamOverlay.builder.logoHint',
          'One logo for the whole installation, drawn above the BamDude mark in the corner of the overlay. PNG, JPEG or WebP up to 2 MiB; it is saved without its metadata and scaled down to 512 pixels. Anyone with an overlay address can see it.',
        )}
      </p>
      {!mayChange && (
        <p className="text-xs text-bambu-gray mt-2">
          {t(
            'streamOverlay.builder.logoNoPermission',
            'Only somebody who may change settings can upload, replace or remove the logo.',
          )}
        </p>
      )}

      {removing && (
        <ConfirmModal
          variant="warning"
          title={t('streamOverlay.builder.logoRemoveTitle', 'Remove the logo?')}
          message={t(
            'streamOverlay.builder.logoRemoveMessage',
            'Every overlay that shows the logo stops drawing it, including the ones already in a scene.',
          )}
          confirmText={t('streamOverlay.builder.logoRemove', 'Remove logo')}
          onCancel={() => setRemoving(false)}
          onConfirm={() => void remove()}
        />
      )}
    </fieldset>
  );
}
