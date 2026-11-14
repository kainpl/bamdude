/**
 * The stream overlay's second design ("Version 2", `?design=2`).
 *
 * Classic lays text over the picture on a gradient. This one frames it: an
 * identity chip (printer, model) top left, the BamDude mark top right, and a
 * panel at the bottom with the file name, a status pill, a labelled progress
 * bar and the readings as icon + caption + value. In a portrait source the
 * three stack — header, picture, panel — so the picture is never covered.
 *
 * It draws what the page hands it and decides nothing about the printer: a
 * value that must not be shown (an offline printer's frozen progress) arrives
 * as null.
 *
 * The colours are fixed on purpose: the page is composited into a broadcast,
 * not themed with the app.
 */
import { useEffect, useLayoutEffect, useState, type CSSProperties, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { Clock, Layers, Printer, Timer } from 'lucide-react';
import { fillStyle, useRotatedBox } from '../hooks/useRotatedFit';
import type { CameraStreamPhase } from '../hooks/useCameraStreamRecovery';
import { OVERLAY_CANVAS, type OverlayLayout } from '../utils/overlayOptions';
import './streamOverlayV2.css';

export interface OverlayV2Camera {
  /** The page's one writer of the image's address (`useCameraImageRef`). */
  attach: (image: HTMLImageElement | null) => void | (() => void);
  generation: number;
  rotation: number;
  phase: CameraStreamPhase;
  onError: (event: React.SyntheticEvent<HTMLImageElement>) => void;
  onLoad: (event: React.SyntheticEvent<HTMLImageElement>) => void;
}

export interface OverlayV2Reading {
  key: string;
  icon: ReactNode;
  label: string;
  current: number;
  target: number | null;
}

interface StreamOverlayV2Props {
  /** A fixed canvas (`?layout=`), or null: follow the shape of the window. */
  layout: OverlayLayout | null;
  size: 'small' | 'medium' | 'large';
  /** 0 (solid) … 100 (the dark grounds are gone). */
  transparency: number;
  progressFill: string | undefined;
  camera: OverlayV2Camera | null;
  name: string | null;
  model: string | null;
  filename: string | null;
  status: string | null;
  /** Colours the status dot: a printer state, or `OFFLINE`. */
  state: string | null;
  progress: number | null;
  layers: string | null;
  remaining: string | null;
  finish: string | null;
  temperatures: OverlayV2Reading[];
}

// Without `?layout=` the shape of the browser source decides the arrangement.
function useViewportOrientation(): 'landscape' | 'portrait' {
  const [portrait, setPortrait] = useState(() => window.matchMedia('(orientation: portrait)').matches);
  useEffect(() => {
    const media = window.matchMedia('(orientation: portrait)');
    const update = (event: MediaQueryListEvent) => setPortrait(event.matches);
    setPortrait(media.matches);
    media.addEventListener('change', update);
    return () => media.removeEventListener('change', update);
  }, []);
  return portrait ? 'portrait' : 'landscape';
}

export function StreamOverlayV2(props: StreamOverlayV2Props) {
  const { t } = useTranslation();
  const { camera, name, model, filename, status, progress, temperatures } = props;
  const viewport = useViewportOrientation();
  const layout = props.layout ?? viewport;

  const alpha = 1 - props.transparency / 100;
  // The app paints the page's body; with a see-through ground that colour
  // would fill every pixel another OBS source should show through.
  useLayoutEffect(() => {
    if (alpha === 1) return;
    const previous = document.body.style.backgroundColor;
    document.body.style.backgroundColor = 'transparent';
    return () => {
      document.body.style.backgroundColor = previous;
    };
  }, [alpha]);

  // `cover` fills the slot; a camera turned sideways is laid out in the
  // slot's sides swapped first, or the turn would crop it.
  const { boxRef: slotRef, box: slot } = useRotatedBox(camera?.rotation);

  const stats = [
    { key: 'layers', icon: <Layers />, label: t('streamOverlay.v2.layers'), value: props.layers },
    { key: 'remaining', icon: <Timer />, label: t('streamOverlay.v2.remaining'), value: props.remaining },
    { key: 'finish', icon: <Clock />, label: t('streamOverlay.v2.finish'), value: props.finish },
  ].filter((stat) => stat.value != null);
  const hasPanel = Boolean(filename || status || progress != null || stats.length > 0 || temperatures.length > 0);

  const rootStyle: CSSProperties & { '--so2-alpha': number; '--so2-unit'?: string } = { '--so2-alpha': alpha };
  if (props.layout) {
    // On a canvas the type follows the canvas, not the window it is scaled into.
    const { width, height } = OVERLAY_CANVAS[props.layout];
    rootStyle['--so2-unit'] = `${Math.min(width, height) / 100}px`;
  }

  return (
    <div
      className="so2"
      data-overlay-design="2"
      data-size={props.size}
      data-layout={layout}
      data-canvas={props.layout ? '' : undefined}
      data-state={props.state ?? undefined}
      style={rootStyle}
    >
      <header className="so2__header">
        {(name || model) && (
          <div className="so2__identity">
            <Printer aria-hidden="true" />
            <div>
              {name && <strong>{name}</strong>}
              {model && <span>{model}</span>}
            </div>
          </div>
        )}
        <a
          className="so2__brand"
          href="https://github.com/kainpl/bamdude"
          target="_blank"
          rel="noopener noreferrer"
        >
          <img src="/img/brand/lockup-compact-on-dark.svg" alt="BamDude" />
        </a>
      </header>

      {camera && (
        <div className="so2__camera" ref={slotRef}>
          {camera.phase === 'live' && (
            <img
              ref={camera.attach}
              key={camera.generation}
              alt={t('streamOverlay.cameraStream')}
              style={fillStyle(slot, camera.rotation)}
              onError={camera.onError}
              onLoad={camera.onLoad}
            />
          )}
          {camera.phase === 'failed' && <p className="so2__nosignal">{t('streamOverlay.noSignal')}</p>}
        </div>
      )}

      {hasPanel && (
        <section className="so2__panel">
          {(filename || status) && (
            <div className="so2__summary">
              {filename && <h1>{filename}</h1>}
              {status && (
                <div className="so2__status">
                  <i aria-hidden="true" />
                  {status}
                </div>
              )}
            </div>
          )}
          {progress != null && (
            <div className="so2__progress">
              <span>{t('streamOverlay.progress')}</span>
              <div
                className="so2__track"
                role="progressbar"
                aria-label={t('streamOverlay.progress')}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={Math.round(progress)}
              >
                <div
                  className="so2__fill"
                  style={{
                    width: `${progress}%`,
                    ...(props.progressFill ? { backgroundImage: props.progressFill } : {}),
                  }}
                />
              </div>
              <strong>{Math.round(progress)}%</strong>
            </div>
          )}
          {(stats.length > 0 || temperatures.length > 0) && (
            <div className="so2__readings">
              {stats.map((stat) => (
                <div className="so2__reading" key={stat.key}>
                  {stat.icon}
                  <div>
                    <span>{stat.label}</span>
                    <strong>{stat.value}</strong>
                  </div>
                </div>
              ))}
              {temperatures.map((reading) => (
                <div className="so2__reading" key={reading.key}>
                  {reading.icon}
                  <div>
                    <span>{reading.label}</span>
                    <strong>
                      {Math.round(reading.current)}°C
                      {reading.target != null
                        && reading.target > 0
                        && Math.round(reading.target) !== Math.round(reading.current) && (
                          <small> / {Math.round(reading.target)}°C</small>
                        )}
                    </strong>
                  </div>
                </div>
              ))}
            </div>
          )}
        </section>
      )}
    </div>
  );
}
