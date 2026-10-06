import * as THREE from 'three';

// Vendored build output, typed by its sibling .d.ts.
import { buildSegmentData, makeToolpath } from '../lib/vendor/toolpathRenderer.js';
import { RenderError, type Bounds } from './protocol';
import type { Selection } from './select';

/** One renderer for the whole attempt; transparent clear, no device-pixel scaling (spec §5.2). */
export function createRenderer(size: number): THREE.WebGLRenderer {
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, preserveDrawingBuffer: true });
  renderer.setPixelRatio(1);
  renderer.setSize(size, size, false);
  renderer.setClearColor(0x000000, 0);
  return renderer;
}

/** Perspective 35°, view direction (0.7, 0.65, 0.7), distance 1.10 × radius / sin(fov/2). */
export function frameCamera(bounds: Bounds): THREE.PerspectiveCamera {
  const [a, b] = [bounds.min, bounds.max];
  // The renderer's scene is Y-up with the bed in XZ; the group below rotates it.
  const box = new THREE.Box3(new THREE.Vector3(a[0], a[2], -b[1]), new THREE.Vector3(b[0], b[2], -a[1]));
  const center = box.getCenter(new THREE.Vector3());
  const radius = Math.max(box.getSize(new THREE.Vector3()).length() / 2, 0.001);
  const camera = new THREE.PerspectiveCamera(35, 1, 0.01, 10000);
  const distance = (1.1 * radius) / Math.sin(THREE.MathUtils.degToRad(camera.fov) / 2);
  camera.position.copy(center).addScaledVector(new THREE.Vector3(0.7, 0.65, 0.7).normalize(), distance);
  camera.near = Math.max(distance / 1000, 0.01);
  camera.far = distance + radius * 4;
  camera.lookAt(center);
  camera.updateProjectionMatrix();
  return camera;
}

function canvasToBlob(canvas: HTMLCanvasElement): Promise<Blob> {
  return new Promise((resolve, reject) => {
    canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new RenderError('invalid_output', 'canvas produced no PNG'))), 'image/png');
  });
}

/** Render one selection in the file's filament colours; GPU resources are freed before returning. */
export async function renderSelection(
  renderer: THREE.WebGLRenderer,
  selection: Selection,
  palette: string[],
  defaultWidth: number,
): Promise<Blob> {
  if (selection.bounds === null) throw new RenderError('invalid_output', 'nothing to render');
  const data = buildSegmentData(selection.layers, defaultWidth);
  if (data.hasNaN) throw new RenderError('parse_failed', 'renderer geometry contains NaN');
  const handle = makeToolpath(THREE, data);
  try {
    // setColors takes one packed 0xRRGGBB per vertex in the first of four floats.
    const colors = new Float32Array(data.nV * 4);
    for (let v = 0; v < data.nV; v += 1) {
      const tool = data.meta.vType[v] - 1;
      const hex = palette[tool];
      if (!hex) throw new RenderError('parse_failed', `no filament colour for T${tool}`);
      colors[v * 4] = Number.parseInt(hex.slice(1), 16);
    }
    handle.setColors(colors);
    handle.setLayerRange(0, selection.layers.length - 1);
    handle.setTravelVisible(false);
    const scene = new THREE.Scene();
    const group = new THREE.Group();
    group.rotation.x = -Math.PI / 2;
    group.add(handle.mesh);
    scene.add(group);
    renderer.render(scene, frameCamera(selection.bounds));
    return await canvasToBlob(renderer.domElement);
  } finally {
    handle.dispose();
  }
}
