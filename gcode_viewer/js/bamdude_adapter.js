/**
 * bamdude_adapter.js
 * Bridges OctoPrint-PrettyGCode to BamDude's API.
 *
 * Load this BEFORE prettygcode.js. It provides:
 *   - OCTOPRINT_VIEWMODELS shim
 *   - Minimal KnockoutJS observable shim (ko.observable)
 *   - fetch() + XHR interceptors for path rewriting
 *   - BamDude WebSocket → fromCurrentData bridge
 *   - File picker backed by BamDude's library API
 *   - Settings load/save via plugin settings endpoint
 *
 * What works:
 *   - Full 3D GCode visualisation
 *   - Dark mode and all dat.GUI settings
 *   - File selection from BamDude's file library
 *   - Print progress highlight (% based)
 *   - Auto-load currently printing file
 *
 * What doesn't work (Bambu hardware limitation):
 *   - Live nozzle animation during printing — Bambu printers do not expose
 *     GCode serial echo logs (Send: G1 X...), so PrintHeadSimulator has no input.
 */

(function () {
    'use strict';

    const API_BASE = '/api/v1';
    const VIEWER_BASE = '/gcode-viewer'; // static assets now served from here

    // -------------------------------------------------------------------------
    // i18n — small bundled dictionary, keyed by URL-param ?lang=…
    //
    // The iframe runs in its own document so it can't reach the SPA's
    // react-i18next. GCodeViewerPage forwards the SPA's current language
    // as ``?lang=<code>``; we read that once on init and apply labels to
    // the toolbar / dat.GUI controllers / filename composers / PNotify
    // wrapper. Defaults to English when the param is missing or unknown.
    //
    // ONLY add keys that BamDude actually surfaces in the iframe; deep
    // prettygcode.js internal strings (status overlay, slider digits)
    // are either hidden or language-neutral.
    // -------------------------------------------------------------------------
    const I18N = {
        en: {
            viewOptions: 'View Options',
            noFileLoaded: '— no file loaded —',
            playAria: 'Play layer animation',
            playbackSpeedAria: 'Playback speed',
            speedSlow: '1× slow',
            speedNormal: '3×',
            speedFast: '10× fast',
            speedTurbo: '25× turbo',
            archiveLabel: 'Archive #{id}',
            libraryFileLabel: 'Library file #{id}',
            plateSuffix: '(plate {n})',
            // dat.GUI controller labels (see §3 in
            // temp/gcode-viewer-prettygcode-knobs.md)
            darkMode: 'Dark mode',
            showMirror: 'Show mirror',
            orbitWhenIdle: 'Orbit when idle',
            fatLines: 'Fat lines',
            antialias: 'Anti-aliasing',
            showNozzle: 'Show nozzle',
            // PNotify (only one is fired by prettygcode — the antialias one)
            reloadRequiredTitle: 'Reload required',
            reloadRequiredText: 'Anti-aliasing changes only take effect after a page reload.',
        },
        uk: {
            viewOptions: 'Параметри перегляду',
            noFileLoaded: '— файл не завантажено —',
            playAria: 'Програти анімацію по шарах',
            playbackSpeedAria: 'Швидкість відтворення',
            speedSlow: '1× повільно',
            speedNormal: '3×',
            speedFast: '10× швидко',
            speedTurbo: '25× турбо',
            archiveLabel: 'Архів №{id}',
            libraryFileLabel: 'Файл бібліотеки №{id}',
            plateSuffix: '(плита {n})',
            darkMode: 'Темний режим',
            showMirror: 'Дзеркало шару',
            orbitWhenIdle: 'Авто-обертання',
            fatLines: 'Товсті лінії',
            antialias: 'Згладжування',
            showNozzle: 'Показувати сопло',
            reloadRequiredTitle: 'Потрібне перезавантаження',
            reloadRequiredText: 'Зміни згладжування набудуть чинності після перезавантаження сторінки.',
        },
    };

    function _detectLang() {
        try {
            var p = new URLSearchParams(window.location.search).get('lang');
            if (p && I18N[p]) return p;
        } catch (e) { /* URLSearchParams unsupported — fall through */ }
        return 'en';
    }
    var LANG = _detectLang();
    function t(key, vars) {
        var s = (I18N[LANG] && I18N[LANG][key]) || I18N.en[key] || key;
        if (vars) {
            for (var k in vars) {
                if (Object.prototype.hasOwnProperty.call(vars, k)) {
                    s = s.replace(new RegExp('\\{' + k + '\\}', 'g'), vars[k]);
                }
            }
        }
        return s;
    }

    // dat.GUI's collapse/expand button reads these globals once at
    // construction time, so they must be set BEFORE prettygcode.js runs
    // (our adapter loads after dat.gui.js but before prettygcode.js per
    // the <script> order in index.html).
    if (typeof window.dat === 'undefined') window.dat = {};
    if (typeof window.dat.GUI === 'undefined') window.dat.GUI = {};
    window.dat.GUI.TEXT_OPEN = t('viewOptions');
    window.dat.GUI.TEXT_CLOSED = t('viewOptions');

    // Capture the live dat.GUI instance prettygcode.js builds.
    // PrettyGCode keeps the gui as a closure-local var, so the only way
    // to reach it from here is to wrap the constructor and stash the
    // instance on a known global. We use this in `_customiseDatGui()`
    // (post-init) to apply BamDude defaults + relabel + hide rows.
    if (typeof window.dat.GUI === 'function' && !window.dat.GUI.__bamdudeWrapped) {
        var _OrigDatGUI = window.dat.GUI;
        function WrappedDatGUI(opts) {
            var instance = new _OrigDatGUI(opts);
            // Prefer the first GUI as "the" one (prettygcode only ever
            // builds one main GUI). Subfolders go through addFolder()
            // and don't hit the constructor.
            if (!window.__bamdudeDatGui) window.__bamdudeDatGui = instance;
            return instance;
        }
        WrappedDatGUI.__bamdudeWrapped = true;
        WrappedDatGUI.prototype = _OrigDatGUI.prototype;
        for (var _gk in _OrigDatGUI) {
            if (Object.prototype.hasOwnProperty.call(_OrigDatGUI, _gk)) {
                WrappedDatGUI[_gk] = _OrigDatGUI[_gk];
            }
        }
        // Re-pin our text overrides on the wrapper too (loop above
        // copied them, but explicit-set keeps the contract obvious).
        WrappedDatGUI.TEXT_OPEN = t('viewOptions');
        WrappedDatGUI.TEXT_CLOSED = t('viewOptions');
        window.dat.GUI = WrappedDatGUI;
    }

    // Localise the only PNotify firing in prettygcode.js (the antialias
    // "reload required" notice). Wrap the constructor BEFORE prettygcode
    // loads so the wrapper is in place by the time the user clicks the
    // toggle. Native English is the only message we know about — match
    // it loosely so a future upstream wording change still produces a
    // localised toast (falls through to original text otherwise).
    var _NativePNotify = window.PNotify;
    function _wrapPNotify() {
        if (typeof window.PNotify !== 'function') return;
        var Native = window.PNotify;
        if (Native.__bamdudeWrapped) return;
        function Wrapped(opts) {
            try {
                if (opts && typeof opts.text === 'string'
                    && /antialias/i.test(opts.text)
                    && /(reload|refresh)/i.test(opts.text)) {
                    opts.title = t('reloadRequiredTitle');
                    opts.text = t('reloadRequiredText');
                }
            } catch (e) { /* fall through to native behaviour */ }
            return new Native(opts);
        }
        Wrapped.__bamdudeWrapped = true;
        // Preserve any static helpers / prototype the native PNotify carries.
        Wrapped.prototype = Native.prototype;
        for (var k in Native) {
            if (Object.prototype.hasOwnProperty.call(Native, k)) Wrapped[k] = Native[k];
        }
        window.PNotify = Wrapped;
    }
    if (_NativePNotify) {
        _wrapPNotify();
    } else {
        // PNotify might be loaded by a later <script>; install a lazy
        // setter so we wrap as soon as it appears.
        Object.defineProperty(window, 'PNotify', {
            configurable: true,
            get: function () { return _NativePNotify; },
            set: function (v) {
                _NativePNotify = v;
                _wrapPNotify();
            },
        });
    }

    // -------------------------------------------------------------------------
    // Auth helper
    // -------------------------------------------------------------------------
    function authHeaders() {
        // sessionStorage is used when the user opts out of "remember me";
        // fall back to localStorage for persistent sessions.
        const token = sessionStorage.getItem('auth_token') ?? localStorage.getItem('auth_token');
        return token ? { Authorization: 'Bearer ' + token } : {};
    }

    // When auth is enabled and the user has no valid token, every API call
    // returns 401 and the viewer chrome stays on screen showing empty state.
    // Intercept the first 401 and hand control back to the SPA, which owns
    // the login flow and will redirect to /login when appropriate.
    let _authRedirectFired = false;
    function apiFetch(path, opts) {
        return fetch(API_BASE + path, {
            ...opts,
            headers: { ...authHeaders(), ...(opts && opts.headers) },
            cache: 'no-store',
        }).then((response) => {
            if (response.status === 401 && !_authRedirectFired) {
                _authRedirectFired = true;
                try {
                    sessionStorage.removeItem('auth_token');
                    localStorage.removeItem('auth_token');
                } catch (e) { /* storage unavailable */ }
                window.top.location.replace('/');
            }
            return response;
        });
    }

    // -------------------------------------------------------------------------
    // 1. Minimal KnockoutJS shim  (ko.observable / ko.computed)
    // -------------------------------------------------------------------------
    window.ko = {
        observable: function (initial) {
            var _val = initial;
            var _subs = [];
            var obs = function (newVal) {
                if (arguments.length > 0) {
                    _val = newVal;
                    _subs.forEach(function (cb) { try { cb(newVal); } catch (e) {} });
                }
                return _val;
            };
            obs.subscribe = function (cb) {
                _subs.push(cb);
                return { dispose: function () { _subs = _subs.filter(function (s) { return s !== cb; }); } };
            };
            obs.peek = function () { return _val; };
            return obs;
        },
        computed: function (fn) {
            var obs = window.ko.observable(null);
            try { obs(fn()); } catch (e) {}
            return obs;
        },
        pureComputed: function (fn) { return window.ko.computed(fn); },
        mapping: { fromJS: function (obj) { return obj; } },
    };

    // -------------------------------------------------------------------------
    // 2. OCTOPRINT_VIEWMODELS registration shim
    // -------------------------------------------------------------------------
    window.OCTOPRINT_VIEWMODELS = [];

    // -------------------------------------------------------------------------
    // 3. Fake OctoPrint settings / printer profile / login viewmodels
    // -------------------------------------------------------------------------
    var fakeSettings = {
        webcam: {
            streamUrl: ko.observable(''),
            flipH: ko.observable(false),
            flipV: ko.observable(false),
            rotate90: ko.observable(false),
        },
        plugins: {
            prettygcode: {
                darkMode: ko.observable(false),
            },
        },
    };

    // Bed sizes for common Bambu models (mm)
    // Fallback bed size used until loadArchiveById() fetches the archive's
    // actual build_volume from /api/v1/archives/{id}/capabilities.
    var DEFAULT_BED = { width: 256, depth: 256, height: 256 };

    var currentBed = Object.assign({}, DEFAULT_BED);

    function makeFakeProfileData(bed) {
        return {
            volume: {
                width:       ko.observable(bed.width),
                depth:       ko.observable(bed.depth),
                height:      ko.observable(bed.height),
                origin:      ko.observable('lowerleft'),
                formFactor:  ko.observable('rectangular'),
                // Make custom_box a function so prettygcode.js uses width()/depth()/height()
                custom_box:  function () { return false; },
            },
        };
    }

    var fakePrinterProfiles = {
        currentProfileData: ko.observable(makeFakeProfileData(currentBed)),
    };

    var fakeLoginState = {
        isUser:  ko.observable(true),
        isAdmin: ko.observable(false),
    };

    var fakeControl = {};

    // -------------------------------------------------------------------------
    // 4. fetch() interceptor — rewrite OctoPrint paths to BamDude
    // -------------------------------------------------------------------------
    var _originalFetch = window.fetch.bind(window);
    window.fetch = function (resource, init) {
        var url = (typeof resource === 'string') ? resource
                : (resource && resource.url) ? resource.url
                : null;

        if (url) {
            // Normalize: strip scheme+host so regexes work on the path regardless
            // of whether the browser resolved a relative URL to absolute.
            var path = url.replace(/^https?:\/\/[^\/]+/, '');
            // Also strip the viewer's own path prefix — the browser resolves relative URLs
            // like 'downloads/files/local/...' to '/gcode-viewer/downloads/...' because
            // the page is served from /gcode-viewer/. The regexes below expect bare paths.
            path = path.replace(/^\/gcode-viewer(?=\/|$)/, '');

            var newPath = path;

            // OctoPrint file download  →  BamDude library download
            newPath = newPath.replace(
                /^\/?downloads\/files\/local\/__bamdude_file_(\d+)$/,
                API_BASE + '/library/files/$1/download'
            );
            // OctoPrint file download  →  BamDude archive gcode (specific plate)
            newPath = newPath.replace(
                /^\/?downloads\/files\/local\/__bamdude_archive_(\d+)_plate(\d+)$/,
                API_BASE + '/archives/$1/gcode?plate=$2'
            );
            // OctoPrint file download  →  BamDude archive gcode (first plate)
            newPath = newPath.replace(
                /^\/?downloads\/files\/local\/__bamdude_archive_(\d+)$/,
                API_BASE + '/archives/$1/gcode'
            );
            // OctoPrint file download  →  BamDude library file gcode
            // (sliced LibraryFile — extracts embedded gcode from .gcode.3mf
            // or returns plain .gcode). BamDude divergence from upstream:
            // we forward the plate suffix as ?plate_id=N because our
            // /library/files/<id>/gcode endpoint accepts it (multi-plate
            // library files would otherwise always show plate 1).
            newPath = newPath.replace(
                /^\/?downloads\/files\/local\/__bamdude_libgcode_(\d+)_plate(\d+)$/,
                API_BASE + '/library/files/$1/gcode?plate_id=$2'
            );
            newPath = newPath.replace(
                /^\/?downloads\/files\/local\/__bamdude_libgcode_(\d+)$/,
                API_BASE + '/library/files/$1/gcode'
            );
            // OctoPrint plugin static assets  →  gcode-viewer static files
            newPath = newPath.replace(
                /^\/?plugin\/prettygcode\/static\//,
                VIEWER_BASE + '/'
            );

            if (newPath !== path) {
                url = newPath;
                resource = url; // always pass as string after rewriting
            }

            // Inject auth header for all BamDude API calls
            if (url.startsWith(API_BASE)) {
                var hdrs = authHeaders();
                init = init || {};
                init.headers = Object.assign({}, hdrs, init.headers || {});
            }
        }

        var promise = _originalFetch(resource, init);

        // Tee GCode downloads to build the layer map for sync + nozzle animation
        if (url && (url.match(/\/library\/files\/\d+\/download/) || url.match(/\/archives\/\d+\/gcode/))) {
            promise = promise.then(function (response) {
                var clone = response.clone();
                clone.text().then(function (text) {
                    gcodeLayerMap = parseGcodeLayerMap(text);
                    lastFedLayer = -1;
                    console.log('[PrettyGCode] Parsed ' + gcodeLayerMap.layerOffsets.length +
                                ' layers for sync (' + Math.round(gcodeLayerMap.totalBytes / 1024) + ' KB)');
                }).catch(function (e) {
                    console.warn('[PrettyGCode] GCode layer parse failed:', e);
                });
                return response;
            });
        }

        return promise;
    };

    // -------------------------------------------------------------------------
    // 5. XHR interceptor — rewrite OctoPrint paths (used by THREE.OBJLoader etc.)
    // -------------------------------------------------------------------------
    var _origXHROpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function (method, url) {
        if (typeof url === 'string') {
            // Strip host if absolute, then rewrite OctoPrint static asset paths
            var path = url.replace(/^https?:\/\/[^\/]+/, '');
            path = path.replace(/^\/?plugin\/prettygcode\/static\//, VIEWER_BASE + '/');
            url = path;
        }
        var args = Array.prototype.slice.call(arguments);
        args[1] = url;
        return _origXHROpen.apply(this, args);
    };

    // -------------------------------------------------------------------------
    // 6. GCode layer parser
    //
    // Builds a layer map from the raw GCode text so we can:
    //   a) Map layer_num → byte offset in file (drives prettygcode's filepos sync
    //      and layer highlight, same as if OctoPrint were reporting filepos)
    //   b) Extract a set of G0/G1 commands per layer to feed the PrintHeadSimulator
    //      as synthetic "Send: G1 X... Y... Z..." entries, animating the nozzle model.
    //
    // Layer detection mirrors prettygcode.js: a new layer starts on the first extrusion
    // at a Z position we haven't extruded at before.
    // -------------------------------------------------------------------------
    function parseGcodeLayerMap(text) {
        var lines = text.split('\n');
        var layerOffsets = [];  // layerOffsets[i] = byte pos in file where layer i starts
        var layerCmds = [];     // layerCmds[i]    = array of ' G1 X... Y... Z...' strings
        var byteOffset = 0;
        var x = 0, y = 0, z = 0, e = 0;
        var relative = false, relativeE = false;
        var currentLayerZ = null;
        var curCmds = [];

        for (var i = 0; i < lines.length; i++) {
            var raw = lines[i];
            // +1 for the \n that was consumed by split
            var lineBytes = raw.length + 1;

            var cmd = raw.replace(/;.*$/, '').trim();
            if (!cmd) { byteOffset += lineBytes; continue; }

            var parts = cmd.split(/\s+/);
            var g = parts[0].toUpperCase();

            if (g === 'G90') { relative = false; relativeE = false; }
            else if (g === 'G91') { relative = true; relativeE = true; }
            else if (g === 'M82') { relativeE = false; }
            else if (g === 'M83') { relativeE = true; }
            else if (g === 'G92') {
                // coordinate reset
                for (var p = 1; p < parts.length; p++) {
                    var k0 = parts[p][0].toUpperCase();
                    var v0 = parseFloat(parts[p].slice(1));
                    if (!isNaN(v0)) {
                        if (k0 === 'X') x = v0;
                        else if (k0 === 'Y') y = v0;
                        else if (k0 === 'Z') z = v0;
                        else if (k0 === 'E') e = v0;
                    }
                }
            } else if (g === 'G0' || g === 'G1') {
                var nx = x, ny = y, nz = z, ne = e;
                var hasE = false;
                for (var p = 1; p < parts.length; p++) {
                    if (!parts[p]) continue;
                    var k1 = parts[p][0].toUpperCase();
                    var v1 = parseFloat(parts[p].slice(1));
                    if (isNaN(v1)) continue;
                    if (k1 === 'X') nx = relative ? x + v1 : v1;
                    else if (k1 === 'Y') ny = relative ? y + v1 : v1;
                    else if (k1 === 'Z') nz = relative ? z + v1 : v1;
                    else if (k1 === 'E') { ne = relativeE ? e + v1 : v1; hasE = true; }
                }

                // New layer: first extrusion at a new Z (same logic as prettygcode.js)
                if (hasE && ne > e && nz !== currentLayerZ) {
                    currentLayerZ = nz;
                    if (curCmds.length > 0) layerCmds.push(curCmds);
                    else if (layerOffsets.length > 0) layerCmds.push([]); // gap layer
                    curCmds = [];
                    layerOffsets.push(byteOffset);
                }

                // Record movement commands for nozzle sim (keep arrays small — max 500/layer)
                if ((hasE || nz !== z) && curCmds.length < 500) {
                    curCmds.push(' G1 X' + nx.toFixed(3) +
                                      ' Y' + ny.toFixed(3) +
                                      ' Z' + nz.toFixed(3));
                }

                x = nx; y = ny; z = nz; e = ne;
            }

            byteOffset += lineBytes;
        }
        if (curCmds.length > 0) layerCmds.push(curCmds);

        return {
            layerOffsets: layerOffsets,
            layerCmds:    layerCmds,
            totalBytes:   byteOffset,
        };
    }

    // -------------------------------------------------------------------------
    // 8. State
    // -------------------------------------------------------------------------
    var viewModel = null;
    var currentFileId = null;
    var currentFilename = null;
    var currentFileDate = 0; // stable epoch — only changes when a new file is loaded
    var gcodeLayerMap = null;     // parsed layer data: {layerOffsets, layerCmds, totalBytes}
    var lastFedLayer = -1;        // last layer_num whose commands we fed to printHeadSim

    // The viewer is scoped to previewing a specific archive (/gcode-viewer?archive=<id>).
    // It no longer observes live printer state, so the WebSocket connection, the
    // printer selector, auto-load-currently-printing, and library file picker are all
    // intentionally absent. Bed size is derived from the archive's sliced_for_model.

    function updateFilenameDisplay(filename) {
        var el = document.getElementById('bb-current-file');
        if (el) el.textContent = filename || t('noFileLoaded');
    }

    // Helper: build the trailing " (plate N)" suffix for filename labels.
    function _plateSuffix(plate) {
        return (typeof plate === 'number' && plate >= 1)
            ? (' ' + t('plateSuffix', { n: plate }))
            : '';
    }

    // -------------------------------------------------------------------------
    // 10. Archive loader — invoked via /gcode-viewer/?archive=<id>
    // -------------------------------------------------------------------------
    function loadArchiveById(archiveId, plate) {
        // Pretygcode downloads /downloads/files/local/__bamdude_archive_<id>(_plate<N>)
        // and the fetch intercept rewrites it to /api/v1/archives/<id>/gcode[?plate=N].
        var plateSuffix = (typeof plate === 'number' && plate >= 1) ? ('_plate' + plate) : '';
        var labelSuffix = _plateSuffix(plate);
        currentFileId = 'archive_' + archiveId + plateSuffix;
        currentFilename = t('archiveLabel', { id: archiveId }) + labelSuffix;
        currentFileDate = Date.now();
        gcodeLayerMap = null;
        lastFedLayer = -1;
        stopPlayback(true);
        updateFilenameDisplay(currentFilename);
        var playBtn = document.getElementById('bb-play-btn');
        if (playBtn) playBtn.disabled = false;
        if (viewModel && viewModel.fromCurrentData) {
            viewModel.fromCurrentData({
                job: {
                    file: {
                        path: '__bamdude_archive_' + archiveId + plateSuffix,
                        date: currentFileDate,
                    },
                    estimatedPrintTime: null,
                },
                state: { text: 'Operational', flags: { printing: false } },
                progress: { filepos: null, completion: 0 },
                currentZ: null,
                logs: [],
            });
        }

        // Fetch metadata (for the filename display) and capabilities (for the
        // bed size) in parallel. Capabilities extracts the actual build_volume
        // from the 3MF's slicer config (printable_area / printable_height), so
        // the bed matches whatever hardware the archive was sliced for — no
        // hardcoded per-model map, correct for H2D (350×320×325), H-family
        // machines, and any future model.
        apiFetch('/archives/' + archiveId, {})
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (meta) {
                if (meta && (meta.print_name || meta.filename)) {
                    currentFilename = (meta.print_name || meta.filename) + labelSuffix;
                    updateFilenameDisplay(currentFilename);
                }
            })
            .catch(function () { /* best-effort — filename stays the localised "Archive #N" */ });

        apiFetch('/archives/' + archiveId + '/capabilities', {})
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (caps) {
                if (!caps || !caps.build_volume) return;
                var bv = caps.build_volume;
                if (bv.x > 0 && bv.y > 0 && bv.z > 0) {
                    currentBed = { width: bv.x, depth: bv.y, height: bv.z };
                    fakePrinterProfiles.currentProfileData(makeFakeProfileData(currentBed));
                }
            })
            .catch(function () { /* best-effort — default bed stays */ });
    }

    // -------------------------------------------------------------------------
    // 10b. Library file loader — invoked via /gcode-viewer/?library_file=<id>
    // -------------------------------------------------------------------------
    function loadLibraryFileById(fileId, plate) {
        // Mirror loadArchiveById, but use the library gcode endpoint. The
        // /library/files/<id>/gcode endpoint extracts the embedded gcode
        // from a .gcode.3mf or returns a plain .gcode body. BamDude
        // forwards the plate as ?plate_id=N (see fetch intercept above).
        var plateSuffix = (typeof plate === 'number' && plate >= 1) ? ('_plate' + plate) : '';
        var labelSuffix = _plateSuffix(plate);
        currentFileId = 'libfile_' + fileId + plateSuffix;
        currentFilename = t('libraryFileLabel', { id: fileId }) + labelSuffix;
        currentFileDate = Date.now();
        gcodeLayerMap = null;
        lastFedLayer = -1;
        stopPlayback(true);
        updateFilenameDisplay(currentFilename);
        var playBtn = document.getElementById('bb-play-btn');
        if (playBtn) playBtn.disabled = false;
        if (viewModel && viewModel.fromCurrentData) {
            viewModel.fromCurrentData({
                job: {
                    file: {
                        path: '__bamdude_libgcode_' + fileId + plateSuffix,
                        date: currentFileDate,
                    },
                    estimatedPrintTime: null,
                },
                state: { text: 'Operational', flags: { printing: false } },
                progress: { filepos: null, completion: 0 },
                currentZ: null,
                logs: [],
            });
        }

        // Fetch metadata for the filename display. There is no
        // /library/files/<id>/capabilities endpoint, so the bed stays at
        // whatever the default fakePrinterProfile is set to.
        apiFetch('/library/files/' + fileId, {})
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (meta) {
                if (meta && (meta.print_name || meta.filename)) {
                    currentFilename = (meta.print_name || meta.filename) + labelSuffix;
                    updateFilenameDisplay(currentFilename);
                }
            })
            .catch(function () { /* best-effort — filename stays the localised "Library file #N" */ });
    }

    // -------------------------------------------------------------------------
    // 11. Initialise after DOM + scripts are ready
    // -------------------------------------------------------------------------
    function init() {
        // Find the ViewModel registration that prettygcode.js pushed
        var reg = null;
        for (var i = 0; i < window.OCTOPRINT_VIEWMODELS.length; i++) {
            if (window.OCTOPRINT_VIEWMODELS[i].construct) {
                reg = window.OCTOPRINT_VIEWMODELS[i];
                break;
            }
        }

        if (!reg) {
            console.error('[PrettyGCode] No ViewModel found in OCTOPRINT_VIEWMODELS');
            return;
        }

        try {
            viewModel = new reg.construct([
                fakeSettings,
                fakeLoginState,
                fakePrinterProfiles,
                fakeControl,
            ]);
        } catch (e) {
            console.error('[PrettyGCode] ViewModel constructor failed:', e);
            return;
        }

        if (viewModel.onAfterBinding) {
            try { viewModel.onAfterBinding(); } catch (e) {}
        }

        // Trigger tab activation — this calls onTabChange which initialises the Three.js scene
        if (viewModel.onTabChange) {
            try { viewModel.onTabChange('#tab_plugin_prettygcode', ''); } catch (e) {
                console.error('[PrettyGCode] onTabChange failed:', e);
            }
        }

        console.log('[PrettyGCode] BamDude adapter initialised');

        // Wire up playback controls
        var playBtn = document.getElementById('bb-play-btn');
        var speedSel = document.getElementById('bb-play-speed');
        if (playBtn) {
            playBtn.addEventListener('click', function () {
                if (isPlaying) stopPlayback();
                else startPlayback();
            });
            playBtn.title = t('playAria');
            playBtn.setAttribute('aria-label', t('playAria'));
        }
        if (speedSel) {
            speedSel.addEventListener('change', function () {
                layersPerTick = parseInt(speedSel.value, 10) || 1;
                // Restart if already playing so speed takes effect immediately
                if (isPlaying) { stopPlayback(); startPlayback(); }
            });
            speedSel.title = t('playbackSpeedAria');
            speedSel.setAttribute('aria-label', t('playbackSpeedAria'));
            // Localise option labels in place. Map by `value` so we
            // don't depend on DOM order.
            var speedKey = { '1': 'speedSlow', '3': 'speedNormal', '10': 'speedFast', '25': 'speedTurbo' };
            for (var i = 0; i < speedSel.options.length; i++) {
                var opt = speedSel.options[i];
                var k = speedKey[opt.value];
                if (k) opt.textContent = t(k);
            }
        }
        // Filename placeholder — show the localised "no file loaded" hint
        // before any loadArchiveById/loadLibraryFileById call replaces it.
        updateFilenameDisplay(null);

        // Customise dat.GUI: localise controller labels + hide rows that
        // are meaningless for archive previews. We dig into private state
        // (gui.__controllers) because dat.GUI doesn't expose a public
        // remove-by-property API. The main-folder controllers live on
        // ``gui`` directly; the hidden Windows folder is in
        // ``gui.__folders.Windows``. window.bamdudeGui isn't a real
        // global — we read it lazily via the DOM (`#mygui` contains the
        // gui's <li> rows so we can match by `.property` in the controllers
        // list that prettygcode.js held onto via `gui.add()` calls).
        try { _customiseDatGui(); } catch (e) { console.warn('[BamDude] dat.GUI customisation failed:', e); }
    }

    // BamDude defaults for archive previews (per
    // temp/gcode-viewer-prettygcode-knobs.md §1). Applied ONLY when the
    // user hasn't persisted any choice yet — once dat.GUI has written
    // localStorage["dat.gui"] we leave their picks alone.
    var BAMDUDE_DEFAULTS = {
        darkMode: true,
        syncToProgress: false,
        showNozzle: false,
        orbitWhenIdle: true,
    };
    // Controllers whose row should be hidden in the View Options panel.
    // The setting still exists internally; the dat.GUI label is just
    // not rendered. ``syncToProgress`` is meaningless for static archive
    // previews — there's no live print to sync against.
    var BAMDUDE_HIDDEN_CONTROLLERS = ['syncToProgress'];

    function _customiseDatGui() {
        // Use the instance our constructor wrap stashed on the global.
        // If the wrap didn't fire (e.g. dat.gui loaded after us via
        // some unexpected reordering, or prettygcode bypassed the
        // constructor), bail silently — the GUI will just show its
        // default English labels.
        var gui = window.__bamdudeDatGui;
        if (!gui || !gui.__controllers) return;

        // 1. Apply BamDude defaults — only on the very first visit
        //    (localStorage empty), so we never clobber user picks.
        var hasPersisted = false;
        try {
            var s = localStorage.getItem('dat.gui');
            hasPersisted = !!(s && s.length > 2);
        } catch (e) { /* storage unavailable */ }
        if (!hasPersisted) {
            for (var i = 0; i < gui.__controllers.length; i++) {
                var c = gui.__controllers[i];
                if (BAMDUDE_DEFAULTS.hasOwnProperty(c.property)) {
                    try { c.setValue(BAMDUDE_DEFAULTS[c.property]); } catch (e) {}
                }
            }
        }

        // 2. Localise controller labels.
        var labelKey = {
            syncToProgress: 'noFileLoaded' /* hidden anyway, no-op */,
            darkMode: 'darkMode',
            showMirror: 'showMirror',
            orbitWhenIdle: 'orbitWhenIdle',
            fatLines: 'fatLines',
            antialias: 'antialias',
            showNozzle: 'showNozzle',
        };
        for (var j = 0; j < gui.__controllers.length; j++) {
            var ctl = gui.__controllers[j];
            var k = labelKey[ctl.property];
            if (k && ctl.property !== 'syncToProgress') {
                try { ctl.name(t(k)); } catch (e) {}
            }
        }

        // 3. Hide rows whose setting is irrelevant for archive previews.
        for (var n = 0; n < gui.__controllers.length; n++) {
            var hCtl = gui.__controllers[n];
            if (BAMDUDE_HIDDEN_CONTROLLERS.indexOf(hCtl.property) !== -1) {
                try {
                    var li = hCtl.domElement && hCtl.domElement.closest('li');
                    if (li) li.style.display = 'none';
                } catch (e) {}
            }
        }
    }

    // -------------------------------------------------------------------------
    // 14. Playback engine
    // -------------------------------------------------------------------------
    var isPlaying = false;
    var playInterval = null;
    var layersPerTick = 1;   // layers advanced per 50 ms tick
    var TICK_MS = 50;        // ~20 fps

    function getSlider() { return $('#myslider-vertical'); }

    function startPlayback() {
        var $sl = getSlider();
        if (!$sl.length) return;
        var data = $sl.data('_pgslider');
        if (!data) return;

        var max = data.opts.max || 0;
        if (max === 0) return;

        // Restart from beginning if already at the end
        var cur = data.opts.value || 0;
        if (cur >= max) cur = 0;

        // Suppress live-print sync while playing
        var evStart = $.Event('slideStart'); evStart.value = cur; $sl.trigger(evStart);

        _setSliderLayer($sl, cur);

        isPlaying = true;
        _updatePlayBtn();

        playInterval = setInterval(function () {
            var d = getSlider().data('_pgslider');
            if (!d) { stopPlayback(); return; }
            var next = (d.opts.value || 0) + layersPerTick;
            if (next >= d.opts.max) {
                next = d.opts.max;
                _setSliderLayer(getSlider(), next);
                stopPlayback(/* skipEvStop */ false);
                return;
            }
            _setSliderLayer(getSlider(), next);
        }, TICK_MS);
    }

    function stopPlayback(skipEvStop) {
        if (playInterval) { clearInterval(playInterval); playInterval = null; }
        isPlaying = false;
        _updatePlayBtn();
        if (!skipEvStop) {
            var $sl = getSlider();
            if ($sl.length) {
                var d = $sl.data('_pgslider');
                var evStop = $.Event('slideStop');
                evStop.value = d ? d.opts.value : 0;
                $sl.trigger(evStop);
            }
        }
    }

    function _setSliderLayer($sl, layer) {
        $sl.slider('setValue', layer);
        var ev = $.Event('slide'); ev.value = layer; $sl.trigger(ev);
        $sl.find('.slider-handle').text(layer);
    }

    function _updatePlayBtn() {
        var btn = document.getElementById('bb-play-btn');
        if (btn) btn.textContent = isPlaying ? '⏸' : '▶';
    }

    // Run after all scripts have loaded. init() (viewmodel + 3D canvas) runs
    // 200 ms later to let prettygcode.js finish its own synchronous setup first.
    function onDomReady() {
        setTimeout(function () {
            init();
            // If the viewer was opened with ?archive=<id>[&plate=<N>] or
            // ?library_file=<id>[&plate=<N>], load that source's gcode once
            // the viewmodel is ready.
            try {
                var params = new URLSearchParams(window.location.search);
                var archiveParam = params.get('archive');
                var libParam = params.get('library_file');
                var plateParam = params.get('plate');
                var plateId = (plateParam && /^[1-9][0-9]*$/.test(plateParam))
                    ? parseInt(plateParam, 10)
                    : undefined;
                if (archiveParam && /^[1-9][0-9]*$/.test(archiveParam)) {
                    var archiveId = parseInt(archiveParam, 10);
                    setTimeout(function () { loadArchiveById(archiveId, plateId); }, 50);
                } else if (libParam && /^[1-9][0-9]*$/.test(libParam)) {
                    var libId = parseInt(libParam, 10);
                    setTimeout(function () { loadLibraryFileById(libId, plateId); }, 50);
                }
            } catch (e) { /* URLSearchParams unsupported — skip */ }
        }, 200);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', onDomReady);
    } else {
        onDomReady();
    }

    // -------------------------------------------------------------------------
    // Public API
    // -------------------------------------------------------------------------
    window.BamDudePrettyGCode = {
        loadArchive: loadArchiveById,
        loadLibraryFile: loadLibraryFileById,
        getViewModel: function () { return viewModel; },
        play: startPlayback,
        stop: stopPlayback,
    };

})();
