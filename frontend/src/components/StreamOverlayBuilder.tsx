/**
 * Streaming-overlay URL builder.
 *
 * The overlay at /overlay/{printerId} has been configurable by query string
 * since the kiosk-token work, but only for people who found the parameters in the wiki. The
 * issue asked for the field set to be selectable "through the web UI"; this is
 * that surface. It composes a URL, it does not persist anything — the URL *is*
 * the configuration, which keeps a scene in OBS reproducible by copy-paste and
 * means two displays can show different fields off one token.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Copy, ExternalLink, Eye, EyeOff } from 'lucide-react';
import { api, type Printer } from '../api/client';
import { useToast } from '../contexts/ToastContext';
import { Select } from './Select';
import { OVERLAY_CANVAS, overlayColour, type OverlayLayout } from '../utils/overlayOptions';

type OverlaySize = 'small' | 'medium' | 'large';
// The value the URL carries: `design=2`; Classic is the absence of it.
type BuilderDesign = 'classic' | '2';
// `auto` emits no `layout` at all — the overlay is sized by its window, which
// is what every address built before the choice existed does. `both` builds
// one address per fixed layout.
type BuilderLayout = 'auto' | OverlayLayout | 'both';

// Order matters: it is the order the fields appear in the overlay, so the
// checkbox list reads as a preview of the result.
const FIELDS = [
  { key: 'printer', labelKey: 'streamOverlay.builder.fieldPrinter', fallback: 'Printer name' },
  { key: 'model', labelKey: 'streamOverlay.builder.fieldModel', fallback: 'Printer model' },
  { key: 'filename', labelKey: 'streamOverlay.builder.fieldFilename', fallback: 'File name' },
  { key: 'status', labelKey: 'streamOverlay.builder.fieldStatus', fallback: 'Status' },
  { key: 'progress', labelKey: 'streamOverlay.builder.fieldProgress', fallback: 'Progress bar' },
  { key: 'layers', labelKey: 'streamOverlay.builder.fieldLayers', fallback: 'Layer count' },
  { key: 'eta', labelKey: 'streamOverlay.builder.fieldEta', fallback: 'Time remaining and ETA' },
  { key: 'nozzle', labelKey: 'printers.heaterHistory.nozzle', fallback: 'Nozzle' },
  { key: 'bed', labelKey: 'printers.heaterHistory.bed', fallback: 'Bed' },
  { key: 'chamber', labelKey: 'printers.heaterHistory.chamber', fallback: 'Chamber' },
] as const;

// Matches parseConfig() in StreamOverlayPage: the fields an overlay shows when
// the URL carries no ?show= at all.
const DEFAULT_FIELDS = ['progress', 'layers', 'eta', 'filename', 'status'];

const DEFAULT_FPS = 15;

// What the two pickers start on when custom colours are switched on.
const DEFAULT_PROGRESS_FROM = '#00ae42';
const DEFAULT_PROGRESS_TO = '#7ee081';

// How long the URL must stand still before an open preview follows it. A
// colour picker moves it many times a second, and every new preview address
// is a new camera stream.
const PREVIEW_SETTLE_MS = 300;

export function StreamOverlayBuilder() {
  const { t } = useTranslation();
  const { showToast } = useToast();

  const [printers, setPrinters] = useState<Printer[]>([]);
  const [printerId, setPrinterId] = useState<number | null>(null);
  const [fields, setFields] = useState<string[]>(DEFAULT_FIELDS);
  const [size, setSize] = useState<OverlaySize>('medium');
  const [fps, setFps] = useState(DEFAULT_FPS);
  const [showCamera, setShowCamera] = useState(true);
  const [customColours, setCustomColours] = useState(false);
  const [progressFrom, setProgressFrom] = useState(DEFAULT_PROGRESS_FROM);
  const [progressTo, setProgressTo] = useState(DEFAULT_PROGRESS_TO);
  const [design, setDesign] = useState<BuilderDesign>('classic');
  const [transparency, setTransparency] = useState(0);
  const [layout, setLayout] = useState<BuilderLayout>('auto');
  const [token, setToken] = useState('');
  const [preview, setPreview] = useState(false);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const list = await api.getPrinters();
        if (cancelled) return;
        setPrinters(list);
        if (list.length > 0) setPrinterId(list[0].id);
      } catch {
        // A failed printer list only costs the picker its options — the builder
        // still works if the user types a printer number into the URL by hand,
        // so this is not worth a toast on a settings page they may just be
        // scrolling past.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const buildUrl = useCallback((target: OverlayLayout | null) => {
    const id = printerId ?? 1;
    const params = new URLSearchParams();
    // Emit ?show= in the canonical field order rather than click order, so the
    // same selection always produces the same URL.
    const selected = FIELDS.filter((f) => fields.includes(f.key)).map((f) => f.key);
    params.set('show', selected.join(','));
    if (size !== 'medium') params.set('size', size);
    if (fps !== DEFAULT_FPS) params.set('fps', String(fps));
    if (!showCamera) params.set('camera', 'false');
    // Without the hash: in an address a bare `#` starts the fragment, and a
    // URL somebody shortens by hand should not lose its colours to that.
    const from = customColours ? overlayColour(progressFrom) : null;
    const to = customColours ? overlayColour(progressTo) : null;
    if (from && to) {
      params.set('progressFrom', from.slice(1));
      params.set('progressTo', to.slice(1));
    }
    if (design === '2') {
      params.set('design', '2');
      // Version 2 only: Classic has no ground to fade.
      if (transparency > 0) params.set('backgroundTransparency', String(transparency));
    }
    if (target) params.set('layout', target);
    if (token.trim()) params.set('token', token.trim());
    return `${window.location.origin}/overlay/${id}?${params.toString()}`;
  }, [printerId, fields, size, fps, showCamera, customColours, progressFrom, progressTo, design, transparency, token]);

  // One address per output: one, or a landscape and a portrait one for "both".
  const outputs = useMemo(() => {
    const targets: (OverlayLayout | null)[] = layout === 'both'
      ? ['landscape', 'portrait']
      : [layout === 'auto' ? null : layout];
    return targets.map((target) => ({ layout: target, url: buildUrl(target) }));
  }, [layout, buildUrl]);

  // What the open preview shows: the outputs once they have stood still.
  const [settled, setSettled] = useState(outputs);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(outputs), PREVIEW_SETTLE_MS);
    return () => clearTimeout(timer);
  }, [outputs]);

  const layoutName = (target: OverlayLayout) =>
    target === 'portrait'
      ? t('streamOverlay.builder.portrait', 'Portrait')
      : t('streamOverlay.builder.landscape', 'Landscape');

  const toggleField = (key: string) => {
    setFields((prev) => (prev.includes(key) ? prev.filter((f) => f !== key) : [...prev, key]));
  };

  const copyUrl = async (url: string) => {
    try {
      // ⚠️ Same fallback as CopyButton: the clipboard API needs a secure
      // context, and plenty of BamDude installs are plain HTTP on a LAN. Not
      // CopyButton itself because this one also raises a toast — the URL is
      // long enough that a silent tick is easy to miss.
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(url);
      } else {
        const ta = document.createElement('textarea');
        ta.value = url;
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
      showToast(t('cameraTokens.toast.copied', 'Copied to clipboard'));
    } catch {
      showToast(t('cameraTokens.toast.copyFailed', 'Copy failed — select and copy manually'), 'error');
    }
  };

  return (
    <div>
      <p className="text-sm text-bambu-gray mb-4">
        {t(
          'streamOverlay.builder.description',
          'Build the URL for a streaming overlay — a full-screen camera view with live print data drawn over it, for OBS, a wall display, or any browser source. Pick the fields you want and copy the URL.',
        )}
      </p>

      <div className="grid gap-4 md:grid-cols-2">
        <div>
          <label
            htmlFor="overlay-builder-printer"
            className="block text-sm font-medium text-white mb-1"
          >
            {t('streamOverlay.builder.printer', 'Printer')}
          </label>
          <Select
            className="w-full"
            id="overlay-builder-printer"
            value={printerId ?? ''}
            onChange={(e) => setPrinterId(Number(e.target.value))}
          >
            {printers.length === 0 && <option value="">{t('common.loading', 'Loading…')}</option>}
            {printers.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </Select>
        </div>

        <div>
          <label htmlFor="overlay-builder-design" className="block text-sm font-medium text-white mb-1">
            {t('streamOverlay.builder.design', 'Design')}
          </label>
          <Select
            className="w-full"
            id="overlay-builder-design"
            value={design}
            onChange={(e) => setDesign(e.target.value === '2' ? '2' : 'classic')}
          >
            <option value="classic">{t('streamOverlay.builder.designClassic', 'Classic')}</option>
            <option value="2">{t('streamOverlay.builder.designV2', 'Version 2')}</option>
          </Select>
          <p className="text-xs text-bambu-gray mt-1">
            {t(
              'streamOverlay.builder.designHint',
              'Classic lays the text over the picture. Version 2 frames it: printer and model in a corner, the job in a panel below, and a stacked arrangement in a portrait source.',
            )}
          </p>
        </div>

        {design === '2' && (
          <div>
            <label htmlFor="overlay-builder-transparency" className="block text-sm font-medium text-white mb-1">
              {t('streamOverlay.builder.transparency', 'Background transparency')}
              <span className="ml-2 font-normal text-bambu-gray">{transparency}%</span>
            </label>
            <input
              id="overlay-builder-transparency"
              type="range"
              min={0}
              max={100}
              step={5}
              value={transparency}
              onChange={(e) => setTransparency(Math.min(Math.max(Math.round(Number(e.target.value)) || 0, 0), 100))}
              className="w-full accent-bambu-green"
            />
            <p className="text-xs text-bambu-gray mt-1">
              {t(
                'streamOverlay.builder.transparencyHint',
                'Fades the dark panels so another source in your scene shows through them. At 100% only the text and the bar are left.',
              )}
            </p>
          </div>
        )}

        <div>
          <label htmlFor="overlay-builder-layout" className="block text-sm font-medium text-white mb-1">
            {t('streamOverlay.builder.layout', 'Layout')}
          </label>
          <Select
            className="w-full"
            id="overlay-builder-layout"
            value={layout}
            onChange={(e) => {
              const next = e.target.value;
              setLayout(next === 'landscape' || next === 'portrait' || next === 'both' ? next : 'auto');
            }}
          >
            <option value="auto">{t('streamOverlay.builder.layoutAuto', 'Fit the browser source')}</option>
            <option value="landscape">{t('streamOverlay.builder.layoutLandscape', 'Landscape (1920 × 1080)')}</option>
            <option value="portrait">{t('streamOverlay.builder.layoutPortrait', 'Portrait (1080 × 1920)')}</option>
            <option value="both">{t('streamOverlay.builder.layoutBoth', 'Both: landscape and portrait')}</option>
          </Select>
          <p className="text-xs text-bambu-gray mt-1">
            {t(
              'streamOverlay.builder.layoutHint',
              'A fixed layout is drawn at one size and scaled to the source, so it looks the same in the preview, in a browser tab and in OBS. "Fit the browser source" sizes the overlay by its window.',
            )}
          </p>
        </div>

        <div>
          <label htmlFor="overlay-builder-size" className="block text-sm font-medium text-white mb-1">
            {t('streamOverlay.builder.size', 'Text size')}
          </label>
          <Select
            className="w-full"
            id="overlay-builder-size"
            value={size}
            onChange={(e) => setSize(e.target.value as OverlaySize)}
          >
            <option value="small">{t('streamOverlay.builder.sizeSmall', 'Small')}</option>
            <option value="medium">{t('streamOverlay.builder.sizeMedium', 'Medium')}</option>
            <option value="large">{t('streamOverlay.builder.sizeLarge', 'Large')}</option>
          </Select>
        </div>

        <div>
          <label htmlFor="overlay-builder-fps" className="block text-sm font-medium text-white mb-1">
            {t('streamOverlay.builder.fps', 'Frame rate')}
          </label>
          <input
            id="overlay-builder-fps"
            type="number"
            min={1}
            max={30}
            value={fps}
            onChange={(e) => setFps(Math.min(Math.max(Number(e.target.value) || 1, 1), 30))}
            className="w-full px-3 py-2 bg-bambu-dark rounded-md text-white border border-bambu-dark-tertiary focus:border-bambu-green focus:outline-none"
          />
          <p className="text-xs text-bambu-gray mt-1">
            {t(
              'streamOverlay.builder.fpsHint',
              'A1 and P1 cameras top out around 5 fps whatever you ask for.',
            )}
          </p>
        </div>

        <div>
          <label htmlFor="overlay-builder-token" className="block text-sm font-medium text-white mb-1">
            {t('streamOverlay.builder.token', 'Streaming Overlay token (optional)')}
          </label>
          <input
            id="overlay-builder-token"
            type="text"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder="bblt_…"
            className="w-full px-3 py-2 bg-bambu-dark rounded-md text-white border border-bambu-dark-tertiary focus:border-bambu-green focus:outline-none font-mono text-xs"
          />
          <p className="text-xs text-bambu-gray mt-1">
            {t(
              'streamOverlay.builder.tokenHint',
              'Only needed when login is enabled: OBS has no session of its own. Create one above with the Streaming Overlay scope.',
            )}
          </p>
        </div>
      </div>

      <fieldset className="mt-4">
        <legend className="text-sm font-medium text-white mb-2">
          {t('streamOverlay.builder.fields', 'Fields to show')}
        </legend>
        <div className="grid gap-2 sm:grid-cols-2 md:grid-cols-3">
          {FIELDS.map((field) => (
            <label key={field.key} className="flex items-center gap-2 text-sm text-bambu-gray">
              <input
                type="checkbox"
                checked={fields.includes(field.key)}
                onChange={() => toggleField(field.key)}
                className="accent-bambu-green"
              />
              {t(field.labelKey, field.fallback)}
            </label>
          ))}
          <label className="flex items-center gap-2 text-sm text-bambu-gray">
            <input
              type="checkbox"
              checked={showCamera}
              onChange={(e) => setShowCamera(e.target.checked)}
              className="accent-bambu-green"
            />
            {t('streamOverlay.builder.fieldCamera', 'Camera feed')}
          </label>
        </div>
        <p className="text-xs text-bambu-gray mt-2">
          {t(
            'streamOverlay.builder.chamberHint',
            'Chamber temperature only appears on models with a real chamber sensor — P1 and A1 printers report a meaningless value, so it is left out there.',
          )}
        </p>
      </fieldset>

      <div className="mt-4">
        <label className="flex items-center gap-2 text-sm text-bambu-gray">
          <input
            type="checkbox"
            checked={customColours}
            onChange={(e) => setCustomColours(e.target.checked)}
            className="accent-bambu-green"
          />
          {t('streamOverlay.builder.customColours', 'Custom progress colours')}
        </label>
        {customColours && (
          <div className="mt-2 flex flex-wrap items-center gap-4">
            {[
              { id: 'overlay-builder-progress-from', label: t('streamOverlay.builder.progressFrom', 'Start colour'), value: progressFrom, set: setProgressFrom },
              { id: 'overlay-builder-progress-to', label: t('streamOverlay.builder.progressTo', 'End colour'), value: progressTo, set: setProgressTo },
            ].map((picker) => (
              <div key={picker.id} className="flex items-center gap-2">
                <label htmlFor={picker.id} className="text-sm text-white">
                  {picker.label}
                </label>
                <input
                  id={picker.id}
                  type="color"
                  value={picker.value}
                  onChange={(e) => picker.set(e.target.value)}
                  className="h-8 w-10 cursor-pointer rounded border border-bambu-dark-tertiary bg-bambu-dark p-0.5"
                />
                <span className="font-mono text-xs text-bambu-gray">{picker.value}</span>
              </div>
            ))}
            <div
              aria-hidden="true"
              className="h-3 min-w-24 flex-1 rounded-full"
              style={{ backgroundImage: `linear-gradient(to right, ${progressFrom}, ${progressTo})` }}
            />
          </div>
        )}
        <p className="text-xs text-bambu-gray mt-2">
          {t(
            'streamOverlay.builder.customColoursHint',
            'The progress bar runs from the start colour on the left to the end colour on the right. Pick the same colour twice for a plain bar.',
          )}
        </p>
      </div>

      <div className="mt-4">
        <p className="text-sm font-medium text-white mb-1">
          {t('streamOverlay.builder.urlTitle', 'Overlay URL')}
        </p>
        {outputs.map((output) => (
          <div key={output.layout ?? 'auto'} className="mb-3 last:mb-0">
            {output.layout && (
              <p className="text-xs text-bambu-gray mb-1">
                <span className="text-white">{layoutName(output.layout)}</span>
                {' · '}
                <span>
                  {t('streamOverlay.builder.sourceSize', 'Browser source size: {{width}} × {{height}}', OVERLAY_CANVAS[output.layout])}
                </span>
              </p>
            )}
            <div className="flex items-center gap-2">
              <code className="flex-1 px-3 py-2 bg-bambu-dark rounded-md text-bambu-green text-xs break-all font-mono select-all">
                {output.url}
              </code>
              <button
                type="button"
                onClick={() => void copyUrl(output.url)}
                className="flex items-center gap-2 px-3 py-2 bg-bambu-green text-white rounded-md hover:bg-bambu-green/90"
              >
                <Copy className="w-4 h-4" />
                {t('cameraTokens.created.copy', 'Copy')}
              </button>
              <a
                href={output.url}
                target="_blank"
                rel="noopener noreferrer"
                className="flex items-center gap-2 px-3 py-2 bg-bambu-dark-tertiary text-white rounded-md hover:bg-bambu-dark-tertiary/80"
              >
                <ExternalLink className="w-4 h-4" />
                {t('streamOverlay.builder.open', 'Open')}
              </a>
            </div>
          </div>
        ))}
        {token.trim() && (
          <p className="text-xs text-bambu-gray mt-2">
            {t(
              'streamOverlay.builder.tokenWarning',
              'This URL contains a token — anyone who can read it can watch the stream and see the file name. Revoke the token to cut it off.',
            )}
          </p>
        )}
      </div>

      {/* The preview opens a real camera stream, so it stays off until asked
          for. Leaving one running behind a settings tab would hold a subscriber
          on the printer's single camera connection for as long as the tab is
          open. */}
      <div className="mt-4">
        <button
          type="button"
          onClick={() => setPreview((p) => !p)}
          className="flex items-center gap-2 px-3 py-2 bg-bambu-dark-tertiary text-white rounded-md hover:bg-bambu-dark-tertiary/80 text-sm"
        >
          {preview ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
          {preview
            ? t('streamOverlay.builder.hidePreview', 'Hide preview')
            : t('streamOverlay.builder.showPreview', 'Show preview')}
        </button>
        {preview && (
          // "Both" is two viewers of one printer's stream: the server fans one
          // camera connection out to them, so this costs the printer nothing more.
          <div className="mt-3 flex flex-wrap items-start gap-3">
            {settled.map((output) => (
              <iframe
                key={output.url}
                src={output.url}
                title={output.layout
                  ? `${t('streamOverlay.builder.previewTitle', 'Overlay preview')} — ${layoutName(output.layout)}`
                  : t('streamOverlay.builder.previewTitle', 'Overlay preview')}
                className={`rounded-md border border-bambu-dark-tertiary bg-black ${
                  output.layout === 'portrait' ? 'w-56 aspect-[9/16]' : 'min-w-64 flex-1 aspect-video'
                }`}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
