import { useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import { useTranslation } from 'react-i18next';
import { ArrowLeft, X } from 'lucide-react';

/**
 * Iframe wrapper around the vendored PrettyGCode viewer at
 * ``/gcode-viewer/`` (B.8 — port of upstream Bambuddy #963 + refactor
 * `c44b6219`). The outer SPA URL ``/gcode-viewer?archive=<id>[&plate=<N>]``
 * (or ``?library_file=<id>[&plate=<N>]``) keeps the BamDude layout shell
 * on full-page reload (the SPA catch-all route handles the no-trailing-
 * slash form); the iframe ``src`` adds the trailing slash so it hits the
 * static-serve route in ``backend/app/main.py::serve_gcode_viewer_index``
 * and forwards the outer page's query string so the adapter can pick up
 * the source id.
 *
 * Adds a slim BamDude-themed header with a Back button + source-context
 * label on top of the iframe — the vendored PrettyGCode UI has no
 * "exit" affordance of its own (it was designed as a full-tab OctoPrint
 * page), so without this header users had no obvious way out other than
 * clicking the sidebar.
 */
export function GCodeViewerPage() {
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();

  // Parse the outer URL's query so the header can label what the viewer
  // is showing — recomputed on each render because the URL can change
  // mid-session (e.g. PlatePickerModal navigates to ?archive=N&plate=M
  // without unmounting this page). Hooks must run on every render
  // (rules-of-hooks); the in-iframe early-return below comes after them.
  const sourceLabel = useMemo(() => {
    const params = new URLSearchParams(window.location.search);
    const plate = params.get('plate');
    const plateSuffix = plate ? ` · ${t('gcodeViewer.plate', { index: plate })}` : '';
    const archive = params.get('archive');
    if (archive) {
      return `${t('gcodeViewer.archiveLabel', { id: archive })}${plateSuffix}`;
    }
    const lib = params.get('library_file');
    if (lib) {
      return `${t('gcodeViewer.libraryFileLabel', { id: lib })}${plateSuffix}`;
    }
    return t('gcodeViewer.title');
  }, [t]);

  // Forward the outer page's query string (e.g. ?archive=82&plate=2) to the
  // iframe so the adapter inside can pick up the archive to load. The
  // iframe URL must keep the trailing slash on /gcode-viewer/ so it hits
  // the static-serve route; the outer SPA URL uses no trailing slash so a
  // full-page reload falls through to the SPA catch-all and keeps the
  // BamDude layout shell. Also append the SPA's current language as
  // ``&lang=<code>`` so the iframe adapter can localise its toolbar +
  // dat.GUI labels (en + uk only — see I18N dict in
  // gcode_viewer/js/bamdude_adapter.js). Iframe is forced to remount on
  // language change via a key on the <iframe> below.
  const iframeSrc = useMemo(() => {
    const search = new URLSearchParams(window.location.search);
    // Don't double-set lang if the outer URL somehow already carries one.
    if (!search.has('lang')) search.set('lang', (i18n.language || 'en').slice(0, 2));
    return `/gcode-viewer/?${search.toString()}`;
  }, [i18n.language]);

  // Safety guard: if this React app is itself inside an iframe (e.g. the
  // gcode_viewer/ static files weren't vendored and serve_spa returned us
  // here), don't render another iframe — that would create an infinite
  // loop that browsers eventually refuse to render anyway.
  if (window !== window.top) {
    return (
      <div style={{ padding: 32, color: '#f88' }}>
        {t('gcodeViewer.staticMissing')}
      </div>
    );
  }

  // Back: prefer history.back() so the user returns to whichever page
  // navigated here (Archives card, list row, or FileManagerPage). When
  // there's no back-history (deep-link / refresh / opened in new tab),
  // fall back to /archives as the most likely intent.
  const handleBack = () => {
    if (window.history.length > 1) {
      navigate(-1);
    } else {
      navigate('/archives');
    }
  };

  // Layout: 3.5rem global header (Layout.tsx) + 2.5rem viewer header.
  // Subtract both from 100vh so the iframe doesn't trigger a double scrollbar.
  return (
    <div className="flex flex-col" style={{ height: 'calc(100vh - 3.5rem)' }}>
      <header className="h-10 flex items-center gap-2 px-3 border-b border-bambu-dark-tertiary bg-bambu-dark-secondary flex-shrink-0">
        <button
          onClick={handleBack}
          className="inline-flex items-center gap-1.5 px-2 py-1 text-sm text-bambu-gray hover:text-white rounded transition-colors"
          title={t('gcodeViewer.back')}
        >
          <ArrowLeft className="w-4 h-4" />
          <span>{t('gcodeViewer.back')}</span>
        </button>
        <div className="flex-1 min-w-0 text-sm text-white truncate" title={sourceLabel}>
          {sourceLabel}
        </div>
        <button
          onClick={handleBack}
          className="p-1 text-bambu-gray hover:text-white rounded transition-colors"
          title={t('common.close')}
          aria-label={t('common.close')}
        >
          <X className="w-4 h-4" />
        </button>
      </header>
      <iframe
        // Key on language so changing it remounts the iframe — adapter
        // reads ``?lang=`` once on init, no postMessage handshake here.
        key={i18n.language}
        src={iframeSrc}
        title="GCode Viewer"
        className="flex-1 w-full block border-0"
      />
    </div>
  );
}
