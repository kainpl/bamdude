import { useId, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button } from './Button';
import type { PlateDetectionROI, PlatePoint } from '../api/client';

interface Props {
  imageUrl: string;
  roi: PlateDetectionROI;
  polygon?: PlatePoint[] | null;
  saving: boolean;
  onSave: (roi: PlateDetectionROI, polygon: PlatePoint[] | null) => void;
  onCancel: () => void;
}

export function PlateDetectionAreaEditor({ imageUrl, roi, polygon, saving, onSave, onCancel }: Props) {
  const { t } = useTranslation();
  const clipId = useId();
  const svgRef = useRef<SVGSVGElement>(null);
  const dragging = useRef<number | null>(null);
  const [mode, setMode] = useState<'rectangle' | 'polygon'>(polygon ? 'polygon' : 'rectangle');
  const [rectangle, setRectangle] = useState(roi);
  const [points, setPoints] = useState<PlatePoint[]>(polygon ?? [
    { x: roi.x, y: roi.y }, { x: Math.min(1, roi.x + roi.w), y: roi.y },
    { x: Math.min(1, roi.x + roi.w), y: Math.min(1, roi.y + roi.h) },
    { x: roi.x, y: Math.min(1, roi.y + roi.h) },
  ]);
  const [error, setError] = useState(false);
  const clamp = (v: number) => Math.max(0, Math.min(1, v));
  const move = (index: number, point: PlatePoint) => {
    setError(false);
    setPoints(old => old.map((p, i) => i === index ? { x: clamp(point.x), y: clamp(point.y) } : p));
  };
  const cursor = (e: React.PointerEvent<SVGSVGElement>) => {
    const bounds = e.currentTarget.getBoundingClientRect();
    return { x: clamp((e.clientX - bounds.left) / bounds.width), y: clamp((e.clientY - bounds.top) / bounds.height) };
  };
  const save = () => {
    if (mode === 'rectangle') { onSave(rectangle, null); return; }
    const area = Math.abs(points.reduce((sum, p, i) => {
      const next = points[(i + 1) % points.length];
      return sum + p.x * next.y - next.x * p.y;
    }, 0)) / 2;
    if (points.length < 3 || area < 0.00001) { setError(true); return; }
    // The server validates intersections and duplicate vertices before saving.
    onSave(rectangle, points);
  };
  return <div className="space-y-3 rounded-lg bg-bambu-dark-tertiary/50 p-3">
    <div className="flex flex-wrap gap-2" role="group" aria-label={t('printers.roi.shape')}>
      {(['rectangle', 'polygon'] as const).map(value => <Button key={value} size="sm"
        variant={mode === value ? 'primary' : 'ghost'} disabled={saving} aria-pressed={mode === value}
        onClick={() => setMode(value)}>{t(`printers.roi.${value}`)}</Button>)}
    </div>
    <div className="relative overflow-hidden rounded-lg border border-bambu-dark-tertiary">
      <img src={imageUrl} alt={t('printers.roi.sourceImage')} className="block w-full" draggable={false} />
      <svg ref={svgRef} viewBox="0 0 1000 1000" preserveAspectRatio="none"
        className="absolute inset-0 h-full w-full touch-none" aria-label={t('printers.roi.title')}
        onPointerDown={e => {
          if (mode !== 'polygon' || saving || e.target !== e.currentTarget || points.length >= 32) return;
          setPoints([...points, cursor(e)]); setError(false);
        }}
        onPointerMove={e => { if (dragging.current !== null && !saving) move(dragging.current, cursor(e)); }}
        onPointerUp={e => { dragging.current = null; if (e.currentTarget.hasPointerCapture(e.pointerId)) e.currentTarget.releasePointerCapture(e.pointerId); }}
        onPointerCancel={() => { dragging.current = null; }}>
        <defs><mask id={clipId}>
          <rect width="1000" height="1000" fill="white" />
          {mode === 'polygon'
            ? <polygon points={points.map(p => `${p.x * 1000},${p.y * 1000}`).join(' ')} fill="black" />
            : <rect x={rectangle.x * 1000} y={rectangle.y * 1000} width={rectangle.w * 1000} height={rectangle.h * 1000} fill="black" />}
        </mask></defs>
        <rect width="1000" height="1000" fill="black" opacity="0.55" mask={`url(#${clipId})`} pointerEvents="none" />
        {mode === 'polygon' ? <>
          <polygon points={points.map(p => `${p.x * 1000},${p.y * 1000}`).join(' ')} fill="none" stroke="#00ae42" strokeWidth="2" vectorEffect="non-scaling-stroke" pointerEvents="none" />
          {points.map((p, i) => <circle key={i} cx={p.x * 1000} cy={p.y * 1000} r="12"
            fill="#00ae42" stroke="white" strokeWidth="2" tabIndex={saving ? -1 : 0} role="button"
            aria-label={t('printers.roi.vertex', { count: i + 1 })}
            onPointerDown={e => {
              if (saving) return;
              e.stopPropagation(); dragging.current = i; svgRef.current?.setPointerCapture(e.pointerId);
            }}
            onKeyDown={e => {
              if (saving) return;
              const step = e.shiftKey ? 0.05 : 0.005;
              if (e.key === 'Delete' || e.key === 'Backspace') { e.preventDefault(); setPoints(points.filter((_, n) => n !== i)); }
              if (e.key.startsWith('Arrow')) {
                e.preventDefault(); move(i, { x: p.x + (e.key === 'ArrowRight' ? step : e.key === 'ArrowLeft' ? -step : 0), y: p.y + (e.key === 'ArrowDown' ? step : e.key === 'ArrowUp' ? -step : 0) });
              }
            }} />)}
        </> : <rect x={rectangle.x * 1000} y={rectangle.y * 1000} width={rectangle.w * 1000} height={rectangle.h * 1000}
          fill="none" stroke="#00ae42" strokeWidth="2" vectorEffect="non-scaling-stroke" pointerEvents="none" />}
      </svg>
    </div>
    {mode === 'rectangle' ? <div className="grid grid-cols-2 gap-3">
      {(['x', 'y', 'w', 'h'] as const).map(key => <label key={key} className="text-xs text-bambu-gray">
        {t(`printers.roi.${{ x: 'xStart', y: 'yStart', w: 'width', h: 'height' }[key]}`)}
        <input type="range" min={key === 'w' || key === 'h' ? 0.01 : 0} max={key === 'w' ? 1 - rectangle.x : key === 'h' ? 1 - rectangle.y : key === 'x' ? 1 - rectangle.w : 1 - rectangle.h}
          step="0.01" value={rectangle[key]} disabled={saving}
          onChange={e => setRectangle({ ...rectangle, [key]: Number(e.target.value) })}
          className="block w-full accent-green-500" />
        {Math.round(rectangle[key] * 100)}%
      </label>)}
    </div> : <div className="flex gap-2">
      <Button size="sm" variant="ghost" disabled={saving || points.length === 0} onClick={() => setPoints([])}>{t('printers.roi.redraw')}</Button>
      <Button size="sm" variant="ghost" disabled={saving || points.length === 0} onClick={() => setPoints(points.slice(0, -1))}>{t('printers.roi.undoPoint')}</Button>
    </div>}
    <p className="text-xs text-bambu-gray">{t(mode === 'polygon' ? 'printers.roi.polygonHelp' : 'printers.roi.instruction')}</p>
    <p className="text-xs text-bambu-gray">{t('printers.roi.coverageHelp')}</p>
    {error && <p role="alert" className="text-sm text-red-400">{t('printers.roi.invalidPolygon')}</p>}
    <div className="flex justify-end gap-2">
      <Button size="sm" variant="ghost" onClick={onCancel} disabled={saving}>{t('common.cancel')}</Button>
      <Button size="sm" onClick={save} disabled={saving || (mode === 'polygon' && points.length < 3)}>{t('common.save')}</Button>
    </div>
  </div>;
}
