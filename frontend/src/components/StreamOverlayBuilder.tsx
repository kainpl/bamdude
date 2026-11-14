/**
 * Streaming-overlay URL builder.
 *
 * The overlay at /overlay/{printerId} has been configurable by query string
 * since the kiosk-token work, but only for people who found the parameters in the wiki. The
 * issue asked for the field set to be selectable "through the web UI"; this is
 * that surface. It composes a URL — the URL *is* the configuration, which
 * keeps a scene in OBS reproducible by copy-paste and means two displays can
 * show different fields off one token.
 *
 * The choices that shape the URL are remembered in this browser, so the
 * builder reopens as it was left, and an address already in a scene can be
 * pasted back in to edit it.
 *
 * ⚠️ THE TOKEN IS NEVER ONE OF THOSE CHOICES. Typed, imported or created
 * here, it lives in this component's memory and in the address it builds, and
 * nowhere else: not in browser storage, not in a request of the builder's
 * own, and not on the screen until asked for. A token created here is the
 * ordinary long-lived token of the signed-in user, with the overlay scope —
 * the server shows it once and keeps only its hash, so leaving the page loses
 * it for good (`StreamOverlayBuilderToken.test.tsx`).
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Copy, ExternalLink, Eye, EyeOff, Import, Plus, RotateCcw } from 'lucide-react';
import { api, type Printer } from '../api/client';
import { useToast } from '../contexts/ToastContext';
import { useAuth } from '../contexts/AuthContext';
import { Select } from './Select';
import {
  DEFAULT_OVERLAY_CHOICES,
  OVERLAY_CANVAS,
  overlayChoices,
  overlayColour,
  parseOverlayUrl,
  type BuilderDesign,
  type BuilderLayout,
  type OverlayChoices,
  type OverlayLayout,
  type OverlaySize,
} from '../utils/overlayOptions';

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

const DEFAULT_FPS = DEFAULT_OVERLAY_CHOICES.fps;

// Where the choices are remembered. The token is not a choice.
const STORAGE_KEY = 'streamOverlayBuilder';

// What stands in for the token in the address on screen.
const MASKED_TOKEN = '****';

const DEFAULT_TOKEN_DAYS = 90;
const MAX_TOKEN_DAYS = 365;

/** The remembered choices, each checked as the overlay itself would check it. */
function loadChoices(): OverlayChoices {
  try {
    return overlayChoices(JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? 'null'));
  } catch {
    // Unreadable, or a browser that refuses storage: start from the defaults.
    return DEFAULT_OVERLAY_CHOICES;
  }
}

function saveChoices(choices: OverlayChoices): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(choices));
  } catch {
    // A private window, or storage that is full or blocked: the builder still
    // works, it only starts from the defaults next time.
  }
}

// How long the URL must stand still before an open preview follows it. A
// colour picker moves it many times a second, and every new preview address
// is a new camera stream.
const PREVIEW_SETTLE_MS = 300;

interface StreamOverlayBuilderProps {
  /** A token was created here: whoever lists the tokens should read them again. */
  onTokenCreated?: () => void;
}

export function StreamOverlayBuilder({ onTokenCreated }: StreamOverlayBuilderProps = {}) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const { hasPermission } = useAuth();

  const [remembered] = useState(loadChoices);
  const [printers, setPrinters] = useState<Printer[]>([]);
  const [printerId, setPrinterId] = useState<number | null>(remembered.printerId);
  const [fields, setFields] = useState<string[]>(remembered.fields);
  const [size, setSize] = useState<OverlaySize>(remembered.size);
  const [fps, setFps] = useState(remembered.fps);
  const [showCamera, setShowCamera] = useState(remembered.showCamera);
  const [customColours, setCustomColours] = useState(remembered.customColours);
  const [progressFrom, setProgressFrom] = useState(remembered.progressFrom);
  const [progressTo, setProgressTo] = useState(remembered.progressTo);
  const [design, setDesign] = useState<BuilderDesign>(remembered.design);
  const [transparency, setTransparency] = useState(remembered.transparency);
  const [layout, setLayout] = useState<BuilderLayout>(remembered.layout);
  // Memory only — see the note at the top of the file.
  const [token, setTokenValue] = useState('');
  const [revealToken, setRevealToken] = useState(false);
  const [tokenCreatedHere, setTokenCreatedHere] = useState(false);
  const [preview, setPreview] = useState(false);

  const [importText, setImportText] = useState('');
  const [importFailed, setImportFailed] = useState(false);

  const [creating, setCreating] = useState(false);
  const [tokenName, setTokenName] = useState('');
  const [tokenDays, setTokenDays] = useState(DEFAULT_TOKEN_DAYS);
  const [submitting, setSubmitting] = useState(false);

  // A new token closes the preview: opening one sends the token to the
  // server, and that is the user's step to take, not a side effect of typing.
  const setToken = (next: string, createdHere = false) => {
    setTokenValue(next);
    setTokenCreatedHere(createdHere);
    setPreview(false);
  };

  const applyChoices = (choices: OverlayChoices) => {
    setPrinterId(choices.printerId);
    setFields(choices.fields);
    setSize(choices.size);
    setFps(choices.fps);
    setShowCamera(choices.showCamera);
    setCustomColours(choices.customColours);
    setProgressFrom(choices.progressFrom);
    setProgressTo(choices.progressTo);
    setDesign(choices.design);
    setTransparency(choices.transparency);
    setLayout(choices.layout);
  };

  // Every choice, and only the choices: this is what is remembered.
  const choices = useMemo<OverlayChoices>(
    () => ({ printerId, fields, size, fps, showCamera, customColours, progressFrom, progressTo, design, transparency, layout }),
    [printerId, fields, size, fps, showCamera, customColours, progressFrom, progressTo, design, transparency, layout],
  );
  const lastSaved = useRef(JSON.stringify(remembered));
  useEffect(() => {
    const serialized = JSON.stringify(choices);
    // Nothing is written until something differs from what was read.
    if (serialized === lastSaved.current) return;
    lastSaved.current = serialized;
    saveChoices(choices);
  }, [choices]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const list = await api.getPrinters();
        if (cancelled) return;
        setPrinters(list);
        // The remembered printer, unless it is gone: then the first one. (An
        // imported address may name a printer this server does not have; that
        // happens later and is kept as it is.)
        if (list.length > 0) {
          setPrinterId((current) => (current != null && list.some((p) => p.id === current) ? current : list[0].id));
        }
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

  const buildUrl = useCallback((target: OverlayLayout | null, tokenText: string) => {
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
    if (tokenText) params.set('token', tokenText);
    // Always this server: an imported address contributes its parameters only.
    return `${window.location.origin}/overlay/${id}?${params.toString()}`;
  }, [printerId, fields, size, fps, showCamera, customColours, progressFrom, progressTo, design, transparency]);

  // One address per output: one, or a landscape and a portrait one for "both".
  // `url` is the real one (copied, opened, previewed); `shown` is what the
  // screen reads, with the token hidden unless it was asked for.
  const outputs = useMemo(() => {
    const targets: (OverlayLayout | null)[] = layout === 'both'
      ? ['landscape', 'portrait']
      : [layout === 'auto' ? null : layout];
    const real = token.trim();
    return targets.map((target) => ({
      layout: target,
      url: buildUrl(target, real),
      shown: buildUrl(target, real && !revealToken ? MASKED_TOKEN : real),
    }));
  }, [layout, buildUrl, token, revealToken]);

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

  const importAddress = () => {
    const parsed = parseOverlayUrl(importText);
    if (!parsed) {
      setImportFailed(true);
      return;
    }
    applyChoices(parsed.choices);
    // The token is taken as the text it is — not checked, not looked up, not
    // sent anywhere. An address without one leaves the token already here.
    if (parsed.token) setToken(parsed.token);
    else setPreview(false);
    setImportText('');
    setImportFailed(false);
    showToast(t('streamOverlay.builder.imported', 'Address imported'));
  };

  const resetChoices = () => {
    applyChoices({ ...DEFAULT_OVERLAY_CHOICES, printerId: printers[0]?.id ?? null });
  };

  const createToken = async () => {
    const name = tokenName.trim();
    if (!name || submitting) return;
    setSubmitting(true);
    try {
      // The ordinary long-lived token of the signed-in user; the scope is
      // fixed, so this button can never mint anything wider than an overlay.
      const created = await api.createLongLivedToken({ name, expires_in_days: tokenDays, scope: 'overlay' });
      if (!created.token) throw new Error(t('cameraTokens.toast.createFailed'));
      setToken(created.token, true);
      setRevealToken(false);
      setCreating(false);
      setTokenName('');
      setTokenDays(DEFAULT_TOKEN_DAYS);
      showToast(t('cameraTokens.toast.created'));
      onTokenCreated?.();
    } catch (err) {
      showToast(err instanceof Error ? err.message : t('cameraTokens.toast.createFailed'), 'error');
    } finally {
      setSubmitting(false);
    }
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
            {printers.length === 0 && printerId == null && <option value="">{t('common.loading', 'Loading…')}</option>}
            {/* A printer this server does not list — an imported address may
                name one — is shown by its number rather than silently replaced. */}
            {printerId != null && !printers.some((p) => p.id === printerId) && (
              <option value={printerId}>#{printerId}</option>
            )}
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
          <div className="flex items-center gap-2">
            <input
              id="overlay-builder-token"
              type={revealToken ? 'text' : 'password'}
              autoComplete="new-password"
              spellCheck={false}
              value={token}
              onChange={(e) => setToken(e.target.value)}
              placeholder="bblt_…"
              className="min-w-0 flex-1 px-3 py-2 bg-bambu-dark rounded-md text-white border border-bambu-dark-tertiary focus:border-bambu-green focus:outline-none font-mono text-xs"
            />
            <button
              type="button"
              onClick={() => setRevealToken((shown) => !shown)}
              aria-label={revealToken
                ? t('streamOverlay.builder.hideToken', 'Hide token')
                : t('streamOverlay.builder.showToken', 'Show token')}
              title={revealToken
                ? t('streamOverlay.builder.hideToken', 'Hide token')
                : t('streamOverlay.builder.showToken', 'Show token')}
              className="shrink-0 rounded-md bg-bambu-dark-tertiary p-2 text-white hover:bg-bambu-dark-tertiary/80"
            >
              {revealToken ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
            </button>
            {hasPermission('camera:view') && !creating && (
              <button
                type="button"
                onClick={() => setCreating(true)}
                className="flex shrink-0 items-center gap-1 rounded-md bg-bambu-dark-tertiary px-3 py-2 text-sm text-white hover:bg-bambu-dark-tertiary/80"
              >
                <Plus className="w-4 h-4" />
                {t('streamOverlay.builder.createToken', 'Create token')}
              </button>
            )}
          </div>
          {creating && (
            <div className="mt-2 rounded-md border border-bambu-dark-tertiary bg-bambu-dark-secondary p-3">
              <div className="grid gap-2 sm:grid-cols-[1fr_7rem]">
                <div>
                  <label htmlFor="overlay-builder-token-name" className="block text-xs text-bambu-gray mb-1">
                    {t('streamOverlay.builder.tokenName', 'Token name')}
                  </label>
                  <input
                    id="overlay-builder-token-name"
                    type="text"
                    maxLength={100}
                    value={tokenName}
                    onChange={(e) => setTokenName(e.target.value)}
                    className="w-full px-3 py-2 bg-bambu-dark rounded-md text-white border border-bambu-dark-tertiary focus:border-bambu-green focus:outline-none"
                  />
                </div>
                <div>
                  <label htmlFor="overlay-builder-token-days" className="block text-xs text-bambu-gray mb-1">
                    {t('streamOverlay.builder.tokenDays', 'Valid for (days)')}
                  </label>
                  <input
                    id="overlay-builder-token-days"
                    type="number"
                    min={1}
                    max={MAX_TOKEN_DAYS}
                    value={tokenDays}
                    onChange={(e) => setTokenDays(Math.min(Math.max(Math.round(Number(e.target.value)) || 1, 1), MAX_TOKEN_DAYS))}
                    className="w-full px-3 py-2 bg-bambu-dark rounded-md text-white border border-bambu-dark-tertiary focus:border-bambu-green focus:outline-none"
                  />
                </div>
              </div>
              <div className="mt-2 flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => void createToken()}
                  disabled={submitting || !tokenName.trim()}
                  className="rounded-md bg-bambu-green px-3 py-1.5 text-sm text-white hover:bg-bambu-green/90 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {t('streamOverlay.builder.createTokenSubmit', 'Create')}
                </button>
                <button
                  type="button"
                  onClick={() => setCreating(false)}
                  disabled={submitting}
                  className="rounded-md bg-bambu-dark-tertiary px-3 py-1.5 text-sm text-white hover:bg-bambu-dark-tertiary/80"
                >
                  {t('common.cancel', 'Cancel')}
                </button>
              </div>
              <p className="text-xs text-bambu-gray mt-2">
                {t(
                  'streamOverlay.builder.createTokenHint',
                  'Creates a Streaming Overlay token for your account and puts it in the address. It is listed with your other tokens above, where you can revoke it.',
                )}
              </p>
            </div>
          )}
          {tokenCreatedHere && token && (
            <p className="text-xs text-yellow-600 dark:text-yellow-400 mt-1">
              {t(
                'streamOverlay.builder.tokenCreatedHint',
                'The new token is in the address below. Copy the address now: once you leave this page the token cannot be shown again.',
              )}
            </p>
          )}
          <p className="text-xs text-bambu-gray mt-1">
            {t(
              'streamOverlay.builder.tokenHint',
              'Only needed when login is enabled: OBS has no session of its own. Paste a Streaming Overlay token or create one here. It is kept in this page only — never saved in the browser — and it opens the overlay of every printer its owner may view, not only the one chosen here.',
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
        <div className="mb-1 flex flex-wrap items-center justify-between gap-2">
          <p className="text-sm font-medium text-white">
            {t('streamOverlay.builder.urlTitle', 'Overlay URL')}
          </p>
          <button
            type="button"
            onClick={resetChoices}
            title={t('streamOverlay.builder.resetChoicesHint', 'Back to the defaults. The token stays.')}
            className="flex items-center gap-1 text-xs text-bambu-gray hover:text-white"
          >
            <RotateCcw className="w-3 h-3" />
            {t('streamOverlay.builder.resetChoices', 'Reset choices')}
          </button>
        </div>
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
                {output.shown}
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
        <p className="text-xs text-bambu-gray mt-2">
          {t(
            'streamOverlay.builder.rememberedHint',
            'Your choices are remembered in this browser. The token never is.',
          )}
        </p>
      </div>

      <div className="mt-4">
        <label htmlFor="overlay-builder-import" className="block text-sm font-medium text-white mb-1">
          {t('streamOverlay.builder.importLabel', 'Import an overlay address')}
        </label>
        <div className="flex items-center gap-2">
          {/* Masked like the token field: an address from a scene usually
              carries a token. */}
          <input
            id="overlay-builder-import"
            type="password"
            autoComplete="new-password"
            spellCheck={false}
            value={importText}
            onChange={(e) => {
              setImportText(e.target.value);
              setImportFailed(false);
            }}
            onKeyDown={(e) => {
              if (e.key === 'Enter') importAddress();
            }}
            placeholder="https://…/overlay/1?show=…"
            aria-invalid={importFailed}
            className="min-w-0 flex-1 px-3 py-2 bg-bambu-dark rounded-md text-white border border-bambu-dark-tertiary focus:border-bambu-green focus:outline-none font-mono text-xs"
          />
          <button
            type="button"
            onClick={importAddress}
            className="flex shrink-0 items-center gap-2 px-3 py-2 bg-bambu-dark-tertiary text-white rounded-md hover:bg-bambu-dark-tertiary/80 text-sm"
          >
            <Import className="w-4 h-4" />
            {t('streamOverlay.builder.importAction', 'Import')}
          </button>
        </div>
        {importFailed && (
          <p role="alert" className="text-xs text-red-600 dark:text-red-400 mt-1">
            {t('streamOverlay.builder.importFailed', 'That is not a stream overlay address.')}
          </p>
        )}
        <p className="text-xs text-bambu-gray mt-1">
          {t(
            'streamOverlay.builder.importHint',
            'Paste an address from an existing scene to edit it. Only its settings are read: the address built here always points at this server, and a token in it is used as it is — it is not checked, saved or sent anywhere.',
          )}
        </p>
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
