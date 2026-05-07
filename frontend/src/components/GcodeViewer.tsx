import { useEffect, useRef, useState, useCallback, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { WebGLPreview } from 'gcode-preview';
import {
  Loader2, Layers, ChevronLeft, ChevronRight, FileWarning,
  Play, Pause, Download, Eye, EyeOff,
} from 'lucide-react';
import { getAuthToken } from '../api/client';

interface GcodeViewerProps {
  gcodeUrl: string;
  buildVolume?: { x: number; y: number; z: number };
  filamentColors?: string[];
  /** SPA theme — drives canvas background. Defaults to dark for the
   *  legacy callers that don't forward the theme prop yet. */
  theme?: 'light' | 'dark';
  /** Optional filename stem used by the Export PNG button. Falls back
   *  to "gcode-preview" when caller didn't pass anything meaningful. */
  exportFilename?: string;
  className?: string;
}

const TRAVELS_KEY = 'bd-gcode-show-travels';
const PLAY_SPEEDS = [1, 2, 4, 8];
const PLAY_INTERVAL_MS = 80;

function bgColorForTheme(theme: 'light' | 'dark'): number {
  // Match ModelViewerModal's container palette so the canvas blends in:
  //  - dark mode: slightly off-black like ``bg-bambu-dark``
  //  - light mode: light grey close to Tailwind's ``bg-gray-100`` so a
  //    white modal doesn't make the bed lines unreadable.
  return theme === 'light' ? 0xf5f5f5 : 0x1a1a1a;
}

export function GcodeViewer({
  gcodeUrl,
  buildVolume = { x: 256, y: 256, z: 256 },
  filamentColors,
  theme = 'dark',
  exportFilename,
  className = '',
}: GcodeViewerProps) {
  const { t } = useTranslation();
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const previewRef = useRef<WebGLPreview | null>(null);
  const renderTimeoutRef = useRef<number | null>(null);
  const playIntervalRef = useRef<number | null>(null);
  const initRef = useRef(false);

  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notSliced, setNotSliced] = useState(false);
  // Layer range — start..end so the slider can crop both top + bottom
  // (gcode-preview supports both `startLayer` and `endLayer`). When
  // start == 1 + end == total the result matches the legacy single-
  // slider "show everything up to layer N" UX.
  const [startLayer, setStartLayer] = useState(1);
  const [endLayer, setEndLayer] = useState(0);
  const [totalLayers, setTotalLayers] = useState(0);
  // Streaming download progress + post-download parse phase.
  const [downloadBytes, setDownloadBytes] = useState(0);
  const [totalBytes, setTotalBytes] = useState<number | null>(null);
  const [parsing, setParsing] = useState(false);
  // Travel-moves toggle persists between sessions so the operator's
  // pick sticks across modal opens.
  const [showTravels, setShowTravels] = useState(() => {
    try { return localStorage.getItem(TRAVELS_KEY) === 'true'; } catch { return false; }
  });
  // Layer-play scrubs `endLayer` from start → total at PLAY_SPEEDS[i]
  // layers per tick. Pauses at end; user clicks play again to restart.
  const [isPlaying, setIsPlaying] = useState(false);
  const [playSpeed, setPlaySpeed] = useState(1);

  // Memoise colors so the init effect only re-runs when the array
  // contents actually change (not on every parent re-render).
  const colorsKey = useMemo(() => JSON.stringify(filamentColors), [filamentColors]);

  useEffect(() => {
    if (!canvasRef.current || initRef.current) return;
    initRef.current = true;

    const canvas = canvasRef.current;
    const rect = canvas.parentElement?.getBoundingClientRect();
    if (rect) {
      canvas.width = rect.width;
      canvas.height = rect.height;
    }

    const hasMultiColor = filamentColors && filamentColors.length > 1;
    const primaryColor = filamentColors?.[0] || '#00ae42';

    const preview = new WebGLPreview({
      canvas,
      buildVolume,
      backgroundColor: bgColorForTheme(theme),
      extrusionColor: hasMultiColor ? filamentColors : primaryColor,
      disableGradient: true,
      lineHeight: 0.2,
      lineWidth: 2,
      renderTravel: showTravels,
      renderExtrusion: true,
    });
    previewRef.current = preview;

    setLoading(true);
    setError(null);
    setNotSliced(false);
    setDownloadBytes(0);
    setTotalBytes(null);
    setParsing(false);
    setIsPlaying(false);

    const headers: HeadersInit = {};
    const token = getAuthToken();
    if (token) headers['Authorization'] = `Bearer ${token}`;

    let cancelled = false;

    // Streaming fetch — count bytes as they land so the progress bar
    // moves on big multi-plate gcodes (>20 MB) instead of just spinning.
    // Falls back to plain ``response.text()`` when the body stream is
    // unavailable (older browsers, Safari with disabled streams).
    fetch(gcodeUrl, { headers })
      .then(async (response) => {
        if (!response.ok) {
          if (response.status === 404) {
            const data = await response.json().catch(() => ({}));
            if (data.detail?.includes('sliced')) {
              setNotSliced(true);
              throw new Error('not_sliced');
            }
          }
          throw new Error('Failed to load G-code');
        }

        const len = response.headers.get('content-length');
        if (len) setTotalBytes(parseInt(len, 10));

        if (!response.body) {
          return await response.text();
        }

        const reader = response.body.getReader();
        const chunks: Uint8Array[] = [];
        let received = 0;
        // Reading in a loop so React state updates can re-render the
        // progress bar mid-download. The full body is reassembled into
        // a single string at the end (gcode-preview wants a string).
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          if (value) {
            chunks.push(value);
            received += value.byteLength;
            if (!cancelled) setDownloadBytes(received);
          }
        }
        const merged = new Uint8Array(received);
        let offset = 0;
        for (const chunk of chunks) {
          merged.set(chunk, offset);
          offset += chunk.byteLength;
        }
        return new TextDecoder().decode(merged);
      })
      .then((gcode) => {
        if (cancelled) return;
        setParsing(true);

        // The gcode-preview library only supports T0-T7 (8 colours).
        // For Bambu printers with high tool numbers (X1's T255 idle, AMS
        // hub combo with T8+) we collapse the actual tool set into the
        // 0..7 range and rebuild the colour array index-aligned with
        // that mapping. Bambu special "tools" (T255, T1000, T65535)
        // are filtered out entirely as comments — they're commands, not
        // extruder selects.
        const toolNumbers = new Set<number>();
        const toolRegex = /^(\s*)T(\d+)(\s*;.*)?$/gim;
        let match;
        while ((match = toolRegex.exec(gcode)) !== null) {
          const toolNum = parseInt(match[2], 10);
          if (toolNum <= 15) toolNumbers.add(toolNum);
        }
        const toolMapping = new Map<number, number>();
        const sortedTools = Array.from(toolNumbers).sort((a, b) => a - b);
        sortedTools.forEach((tool, index) => toolMapping.set(tool, index % 8));
        const remappedColors: string[] = [];
        sortedTools.forEach((originalTool, index) => {
          remappedColors[index % 8] = filamentColors?.[originalTool] || '#00ae42';
        });

        const cleanedGcode = gcode
          .split('\n')
          .map((line) => {
            const m = line.match(/^(\s*)T(\d+)(\s*;.*)?$/i);
            if (m) {
              const toolNum = parseInt(m[2], 10);
              if (toolNum > 15) return `; FILTERED: ${line.trim()}`;
              const mappedTool = toolMapping.get(toolNum) ?? 0;
              return `${m[1]}T${mappedTool}${m[3] || ''}`;
            }
            return line;
          })
          .join('\n');

        if (remappedColors.length > 0) {
          (preview as unknown as { extrusionColor: string[] }).extrusionColor = remappedColors;
        }

        preview.processGCode(cleanedGcode);

        const layers = preview.layers?.length || 0;
        setTotalLayers(layers);
        setStartLayer(1);
        setEndLayer(layers);

        preview.render();
        setParsing(false);
        setLoading(false);
      })
      .catch((err) => {
        if (cancelled) return;
        if (err.message !== 'not_sliced') {
          setError(err.message);
        }
        setParsing(false);
        setLoading(false);
      });

    const handleResize = () => {
      if (canvas.parentElement && previewRef.current) {
        const newRect = canvas.parentElement.getBoundingClientRect();
        canvas.width = newRect.width;
        canvas.height = newRect.height;
        previewRef.current.resize();
      }
    };
    window.addEventListener('resize', handleResize);

    return () => {
      cancelled = true;
      window.removeEventListener('resize', handleResize);
      if (renderTimeoutRef.current) cancelAnimationFrame(renderTimeoutRef.current);
      if (playIntervalRef.current) {
        window.clearInterval(playIntervalRef.current);
        playIntervalRef.current = null;
      }
      if (previewRef.current) {
        previewRef.current.dispose();
        previewRef.current = null;
      }
      initRef.current = false;
    };
    // theme + showTravels are syncd live in their own effects below;
    // re-running init on those would tear down + reload the gcode for
    // a no-op state flip. eslint-disable-next-line react-hooks/exhaustive-deps
  }, [gcodeUrl, colorsKey]);

  // Theme sync — flip canvas background live without recreating the
  // preview (avoids re-fetching + re-parsing the gcode on theme toggle).
  useEffect(() => {
    if (!previewRef.current) return;
    previewRef.current.backgroundColor = bgColorForTheme(theme);
    previewRef.current.render();
  }, [theme]);

  // Travel-moves toggle — live; persist to localStorage so picks stick
  // across modal opens.
  useEffect(() => {
    if (!previewRef.current) return;
    previewRef.current.renderTravel = showTravels;
    previewRef.current.render();
    try { localStorage.setItem(TRAVELS_KEY, showTravels.toString()); } catch { /* storage unavailable */ }
  }, [showTravels]);

  // Apply layer range on every change. Wrapped in rAF so a fast slider
  // drag doesn't fire one render per pixel — gcode-preview's ``render()``
  // does a full GPU pass, expensive for big gcodes.
  useEffect(() => {
    if (!previewRef.current || totalLayers === 0) return;
    if (renderTimeoutRef.current) cancelAnimationFrame(renderTimeoutRef.current);
    renderTimeoutRef.current = requestAnimationFrame(() => {
      if (previewRef.current) {
        previewRef.current.startLayer = Math.max(1, startLayer);
        previewRef.current.endLayer = Math.min(endLayer, totalLayers);
        previewRef.current.render();
      }
    });
  }, [startLayer, endLayer, totalLayers]);

  // Layer-play loop — advances `endLayer` at PLAY_INTERVAL_MS cadence
  // by `playSpeed` layers per tick. When we hit the top, stop. If the
  // user hits play while at the top, restart from `startLayer`.
  useEffect(() => {
    if (!isPlaying || totalLayers === 0) {
      if (playIntervalRef.current) {
        window.clearInterval(playIntervalRef.current);
        playIntervalRef.current = null;
      }
      return;
    }
    setEndLayer((cur) => (cur >= totalLayers ? Math.max(startLayer, 1) : cur));
    playIntervalRef.current = window.setInterval(() => {
      setEndLayer((cur) => {
        const next = cur + playSpeed;
        if (next >= totalLayers) {
          setIsPlaying(false);
          return totalLayers;
        }
        return next;
      });
    }, PLAY_INTERVAL_MS);
    return () => {
      if (playIntervalRef.current) {
        window.clearInterval(playIntervalRef.current);
        playIntervalRef.current = null;
      }
    };
  }, [isPlaying, playSpeed, totalLayers, startLayer]);

  const handleStartChange = useCallback((value: number) => {
    // Keep at least one layer visible — start must be < end.
    setStartLayer(Math.min(Math.max(1, value), endLayer - 1));
  }, [endLayer]);

  const handleEndChange = useCallback((value: number) => {
    setEndLayer(Math.max(Math.min(totalLayers, value), startLayer + 1));
  }, [startLayer, totalLayers]);

  const handleExportPng = useCallback(() => {
    if (!canvasRef.current || !previewRef.current) return;
    // Force a fresh render before reading the pixel buffer so the PNG
    // matches whatever start/end + travels are currently displayed —
    // gcode-preview clears between frames so a stale snapshot would
    // land otherwise.
    previewRef.current.render();
    const dataUrl = canvasRef.current.toDataURL('image/png');
    const a = document.createElement('a');
    const stem = (exportFilename || 'gcode-preview').replace(/[^A-Za-z0-9._-]+/g, '_').slice(0, 80);
    a.href = dataUrl;
    a.download = `${stem}_layers_${startLayer}-${endLayer}.png`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
  }, [exportFilename, startLayer, endLayer]);

  const downloadProgress = totalBytes ? Math.round((downloadBytes / totalBytes) * 100) : null;
  const hasRange = !loading && !error && !notSliced && totalLayers > 0;

  return (
    <div className={`relative flex flex-col h-full ${className}`}>
      <div className="flex-1 relative bg-bambu-dark rounded-lg overflow-hidden">
        <canvas ref={canvasRef} className="w-full h-full" />

        {loading && (
          <div className="absolute inset-0 flex items-center justify-center bg-bambu-dark/80">
            <div className="text-center max-w-xs px-4">
              <Loader2 className="w-8 h-8 animate-spin text-bambu-green mx-auto mb-2" />
              {parsing ? (
                <p className="text-bambu-gray text-sm">{t('gcodeViewer.parsing', { defaultValue: 'Parsing G-code…' })}</p>
              ) : downloadProgress != null ? (
                <>
                  <p className="text-bambu-gray text-sm mb-2">
                    {t('gcodeViewer.downloading', { defaultValue: 'Downloading…' })}{' '}
                    {(downloadBytes / 1024 / 1024).toFixed(1)} / {(totalBytes! / 1024 / 1024).toFixed(1)} MB
                  </p>
                  <div className="w-48 h-1.5 bg-bambu-dark-tertiary rounded-full overflow-hidden mx-auto">
                    <div className="h-full bg-bambu-green transition-all" style={{ width: `${downloadProgress}%` }} />
                  </div>
                </>
              ) : downloadBytes > 0 ? (
                <p className="text-bambu-gray text-sm">
                  {t('gcodeViewer.downloading', { defaultValue: 'Downloading…' })}{' '}
                  {(downloadBytes / 1024 / 1024).toFixed(1)} MB
                </p>
              ) : (
                <p className="text-bambu-gray text-sm">{t('gcodeViewer.loading', { defaultValue: 'Loading G-code…' })}</p>
              )}
            </div>
          </div>
        )}

        {notSliced && (
          <div className="absolute inset-0 flex items-center justify-center bg-bambu-dark/80">
            <div className="text-center max-w-sm px-4">
              <FileWarning className="w-12 h-12 text-bambu-gray mx-auto mb-3" />
              <p className="text-white font-medium mb-2">G-code not available</p>
              <p className="text-bambu-gray text-sm">
                This file hasn't been sliced yet. G-code preview is only available
                after slicing in Bambu Studio or Orca Slicer.
              </p>
            </div>
          </div>
        )}

        {error && !notSliced && (
          <div className="absolute inset-0 flex items-center justify-center bg-bambu-dark/80">
            <div className="text-center text-red-400">
              <p className="text-sm">{error}</p>
            </div>
          </div>
        )}
      </div>

      {hasRange && (
        <div className="mt-3 px-2 space-y-2">
          {/* Toolbar — travels toggle, play, speed, export. The buttons
              live above the slider so they're not crowded by the labels. */}
          <div className="flex items-center gap-2 text-sm flex-wrap">
            <button
              type="button"
              onClick={() => setShowTravels((v) => !v)}
              className={`inline-flex items-center gap-1.5 px-2 py-1 rounded border text-xs transition-colors ${
                showTravels
                  ? 'border-bambu-green text-bambu-green bg-bambu-green/10'
                  : 'border-bambu-dark-tertiary text-bambu-gray hover:text-white hover:border-bambu-gray'
              }`}
              title={t('gcodeViewer.travelsTitle', { defaultValue: 'Show travel moves (G0)' })}
            >
              {showTravels ? <Eye className="w-3.5 h-3.5" /> : <EyeOff className="w-3.5 h-3.5" />}
              {t('gcodeViewer.travels', { defaultValue: 'Travels' })}
            </button>

            <button
              type="button"
              onClick={() => setIsPlaying((v) => !v)}
              className="inline-flex items-center gap-1.5 px-2 py-1 rounded border border-bambu-dark-tertiary text-bambu-gray hover:text-white hover:border-bambu-gray text-xs transition-colors"
              title={isPlaying ? t('gcodeViewer.pause', { defaultValue: 'Pause' }) : t('gcodeViewer.play', { defaultValue: 'Play' })}
            >
              {isPlaying ? <Pause className="w-3.5 h-3.5" /> : <Play className="w-3.5 h-3.5" />}
              {isPlaying
                ? t('gcodeViewer.pause', { defaultValue: 'Pause' })
                : t('gcodeViewer.play', { defaultValue: 'Play' })}
            </button>

            <div className="inline-flex items-center gap-1 text-xs text-bambu-gray">
              <span>{t('gcodeViewer.speed', { defaultValue: 'Speed' })}:</span>
              {PLAY_SPEEDS.map((s) => (
                <button
                  key={s}
                  type="button"
                  onClick={() => setPlaySpeed(s)}
                  className={`px-1.5 py-0.5 rounded text-xs ${
                    playSpeed === s
                      ? 'text-bambu-green font-medium'
                      : 'text-bambu-gray hover:text-white'
                  }`}
                >
                  {s}×
                </button>
              ))}
            </div>

            <button
              type="button"
              onClick={handleExportPng}
              className="ml-auto inline-flex items-center gap-1.5 px-2 py-1 rounded border border-bambu-dark-tertiary text-bambu-gray hover:text-white hover:border-bambu-gray text-xs transition-colors"
              title={t('gcodeViewer.exportPngTitle', { defaultValue: 'Save current view as PNG' })}
            >
              <Download className="w-3.5 h-3.5" />
              {t('gcodeViewer.exportPng', { defaultValue: 'Export PNG' })}
            </button>
          </div>

          {/* Layer range — two stacked sliders for start + end, with
              chevrons on the active end for keyboard-free fine-tuning.
              Two ranges instead of one dual-handle widget keeps the
              code stack-only without an extra dep. */}
          <div className="flex items-center gap-3">
            <Layers className="w-4 h-4 text-bambu-gray flex-shrink-0" />

            <div className="flex-1 flex flex-col gap-1.5">
              <div className="flex items-center gap-2">
                <span className="text-xs text-bambu-gray w-10 flex-shrink-0">
                  {t('gcodeViewer.start', { defaultValue: 'Start' })}
                </span>
                <button
                  type="button"
                  onClick={() => handleStartChange(startLayer - 1)}
                  disabled={startLayer <= 1}
                  className="p-0.5 rounded hover:bg-bambu-dark-tertiary disabled:opacity-30 disabled:cursor-not-allowed"
                >
                  <ChevronLeft className="w-3.5 h-3.5" />
                </button>
                <input
                  type="range"
                  min={1}
                  max={Math.max(1, totalLayers - 1)}
                  value={startLayer}
                  onChange={(e) => handleStartChange(parseInt(e.target.value, 10))}
                  className="flex-1 h-1.5 bg-bambu-dark-tertiary rounded-lg appearance-none cursor-pointer accent-bambu-gray"
                />
                <button
                  type="button"
                  onClick={() => handleStartChange(startLayer + 1)}
                  disabled={startLayer >= endLayer - 1}
                  className="p-0.5 rounded hover:bg-bambu-dark-tertiary disabled:opacity-30 disabled:cursor-not-allowed"
                >
                  <ChevronRight className="w-3.5 h-3.5" />
                </button>
                <span className="text-xs text-bambu-gray min-w-[60px] text-right tabular-nums">
                  {startLayer} / {totalLayers}
                </span>
              </div>

              <div className="flex items-center gap-2">
                <span className="text-xs text-bambu-gray w-10 flex-shrink-0">
                  {t('gcodeViewer.end', { defaultValue: 'End' })}
                </span>
                <button
                  type="button"
                  onClick={() => handleEndChange(endLayer - 1)}
                  disabled={endLayer <= startLayer + 1}
                  className="p-0.5 rounded hover:bg-bambu-dark-tertiary disabled:opacity-30 disabled:cursor-not-allowed"
                >
                  <ChevronLeft className="w-3.5 h-3.5" />
                </button>
                <input
                  type="range"
                  min={2}
                  max={totalLayers}
                  value={endLayer}
                  onChange={(e) => handleEndChange(parseInt(e.target.value, 10))}
                  className="flex-1 h-2 bg-bambu-dark-tertiary rounded-lg appearance-none cursor-pointer accent-bambu-green"
                />
                <button
                  type="button"
                  onClick={() => handleEndChange(endLayer + 1)}
                  disabled={endLayer >= totalLayers}
                  className="p-0.5 rounded hover:bg-bambu-dark-tertiary disabled:opacity-30 disabled:cursor-not-allowed"
                >
                  <ChevronRight className="w-3.5 h-3.5" />
                </button>
                <span className="text-xs text-bambu-gray min-w-[60px] text-right tabular-nums">
                  {endLayer} / {totalLayers}
                </span>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
