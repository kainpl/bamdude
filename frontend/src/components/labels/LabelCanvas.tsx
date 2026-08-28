/**
 * The label, as the printer will produce it, with draggable frames on top.
 *
 * ⚠️ **This component draws no label.** The picture is a PNG the server
 * rendered at device resolution; the canvas lays it down as a backdrop and
 * everything Konva draws is a frame, a handle or a guide. Teaching the browser
 * to lay text out like PIL would mean two renderers that must agree forever,
 * and they would disagree on the first Cyrillic name.
 *
 * ⚠️ **Millimetres cross this boundary, never pixels.** `scale` is px per mm
 * and it lives here; every callback converts before it fires.
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { Image as KonvaImage, Layer, Line, Rect, Stage, Transformer } from 'react-konva';
import type Konva from 'konva';
import type { LabelTemplateElement } from '../../api/client';
import {
  boxOf,
  clampResize,
  clampToLabel,
  GRID_MM,
  mmToPx,
  pxToMm,
  roundMm,
  snapBox,
  type Box,
  type Guide,
  MIN_SIDE_MM,
} from './labelGeometry';

interface LabelCanvasProps {
  widthMm: number;
  heightMm: number;
  elements: LabelTemplateElement[];
  /** Index into `elements`, or null. */
  selected: number | null;
  onSelect: (index: number | null) => void;
  /** Fires once per gesture, not per frame — see the drag handlers. */
  onChange: (index: number, box: Box) => void;
  /** The server's render. Undefined while it is being fetched. */
  previewUrl?: string;
  /** Pixels per millimetre on screen. */
  scale: number;
}

const FRAME_COLOURS: Record<LabelTemplateElement['type'], string> = {
  text: '#22c55e',
  qr: '#3b82f6',
  barcode: '#a855f7',
  swatch: '#f59e0b',
};

export function LabelCanvas({
  widthMm,
  heightMm,
  elements,
  selected,
  onSelect,
  onChange,
  previewUrl,
  scale,
}: LabelCanvasProps) {
  const [image, setImage] = useState<HTMLImageElement | null>(null);
  const [guides, setGuides] = useState<Guide[]>([]);
  const transformerRef = useRef<Konva.Transformer>(null);
  const frameRefs = useRef<(Konva.Rect | null)[]>([]);

  // ⚠️ The object URL is revoked when it changes or the component goes. A
  // preview is re-rendered on every edit, so leaking one per keystroke would
  // hold every intermediate PNG for the life of the tab.
  useEffect(() => {
    if (!previewUrl) {
      setImage(null);
      return;
    }
    const element = new window.Image();
    element.src = previewUrl;
    const onLoad = () => setImage(element);
    element.addEventListener('load', onLoad);
    return () => element.removeEventListener('load', onLoad);
  }, [previewUrl]);

  // Konva's Transformer attaches to nodes imperatively, so it has to be told
  // again whenever the selection or the element list changes.
  useEffect(() => {
    const transformer = transformerRef.current;
    if (!transformer) return;
    const node = selected === null ? null : frameRefs.current[selected];
    transformer.nodes(node ? [node] : []);
    transformer.getLayer()?.batchDraw();
  }, [selected, elements]);

  const stageWidth = mmToPx(widthMm, scale);
  const stageHeight = mmToPx(heightMm, scale);

  const gridLines = useMemo(() => {
    // Only worth drawing when a grid square is big enough to see; below that it
    // is a grey wash that hides the label underneath it.
    if (mmToPx(GRID_MM, scale) < 6) return [];
    const lines: number[][] = [];
    for (let x = GRID_MM; x < widthMm; x += GRID_MM) {
      lines.push([mmToPx(x, scale), 0, mmToPx(x, scale), stageHeight]);
    }
    for (let y = GRID_MM; y < heightMm; y += GRID_MM) {
      lines.push([0, mmToPx(y, scale), stageWidth, mmToPx(y, scale)]);
    }
    return lines;
  }, [widthMm, heightMm, scale, stageWidth, stageHeight]);

  const othersOf = (index: number): Box[] =>
    elements.filter((_, i) => i !== index).map(boxOf);

  return (
    <Stage
      width={stageWidth}
      height={stageHeight}
      onMouseDown={(event) => {
        // Clicking the backdrop clears the selection; clicking a frame does not
        // reach here because the frame stops it.
        if (event.target === event.target.getStage() || event.target.name() === 'backdrop') {
          onSelect(null);
        }
      }}
    >
      <Layer>
        {image ? (
          <KonvaImage image={image} width={stageWidth} height={stageHeight} name="backdrop" />
        ) : (
          <Rect width={stageWidth} height={stageHeight} fill="#ffffff" name="backdrop" />
        )}

        {gridLines.map((points, index) => (
          <Line key={`grid-${index}`} points={points} stroke="#00000014" strokeWidth={1} listening={false} />
        ))}

        {elements.map((element, index) => (
          <Rect
            key={index}
            ref={(node) => {
              frameRefs.current[index] = node;
            }}
            x={mmToPx(element.x_mm, scale)}
            y={mmToPx(element.y_mm, scale)}
            width={mmToPx(element.w_mm, scale)}
            height={mmToPx(element.h_mm, scale)}
            stroke={FRAME_COLOURS[element.type]}
            strokeWidth={selected === index ? 2 : 1}
            dash={selected === index ? undefined : [4, 3]}
            fill={selected === index ? `${FRAME_COLOURS[element.type]}18` : 'transparent'}
            draggable
            onMouseDown={(event) => {
              event.cancelBubble = true;
              onSelect(index);
            }}
            onDragMove={(event) => {
              const node = event.target;
              const { box, guides: found } = snapBox(
                {
                  x_mm: pxToMm(node.x(), scale),
                  y_mm: pxToMm(node.y(), scale),
                  w_mm: element.w_mm,
                  h_mm: element.h_mm,
                },
                othersOf(index),
                widthMm,
                heightMm,
              );
              // Written straight back onto the node so the frame sticks under
              // the cursor rather than drifting and correcting on release.
              node.x(mmToPx(box.x_mm, scale));
              node.y(mmToPx(box.y_mm, scale));
              setGuides(found);
            }}
            onDragEnd={(event) => {
              setGuides([]);
              const node = event.target;
              onChange(
                index,
                clampToLabel(
                  {
                    x_mm: roundMm(pxToMm(node.x(), scale)),
                    y_mm: roundMm(pxToMm(node.y(), scale)),
                    w_mm: element.w_mm,
                    h_mm: element.h_mm,
                  },
                  widthMm,
                  heightMm,
                ),
              );
            }}
            onTransformEnd={(event) => {
              // ⚠️ Konva reports a resize as a SCALE, not as a new size. Left
              // alone the scale compounds on the next drag and the frame
              // detaches from the box it represents, so it is folded into the
              // width and reset here.
              const node = event.target as Konva.Rect;
              const box = clampResize(
                {
                  x_mm: roundMm(pxToMm(node.x(), scale)),
                  y_mm: roundMm(pxToMm(node.y(), scale)),
                  w_mm: roundMm(pxToMm(node.width() * node.scaleX(), scale)),
                  h_mm: roundMm(pxToMm(node.height() * node.scaleY(), scale)),
                },
                widthMm,
                heightMm,
              );
              node.scaleX(1);
              node.scaleY(1);
              onChange(index, box);
            }}
          />
        ))}

        {guides.map((guide, index) =>
          guide.axis === 'x' ? (
            <Line
              key={`guide-${index}`}
              points={[mmToPx(guide.at_mm, scale), 0, mmToPx(guide.at_mm, scale), stageHeight]}
              stroke="#ef4444"
              strokeWidth={1}
              listening={false}
            />
          ) : (
            <Line
              key={`guide-${index}`}
              points={[0, mmToPx(guide.at_mm, scale), stageWidth, mmToPx(guide.at_mm, scale)]}
              stroke="#ef4444"
              strokeWidth={1}
              listening={false}
            />
          ),
        )}

        <Transformer
          ref={transformerRef}
          rotateEnabled={false}
          ignoreStroke
          boundBoxFunc={(oldBox, newBox) => {
            // A box smaller than this cannot be grabbed again, which is a trap
            // rather than a feature.
            const min = mmToPx(MIN_SIDE_MM, scale);
            if (newBox.width < min || newBox.height < min) return oldBox;
            return newBox;
          }}
        />
      </Layer>
    </Stage>
  );
}
