/**
 * CPU rasterizer for part thumbnails (spec §5.2): the segment geometry and the
 * lighting of libvgcode's vertex shader (lib/vendor/toolpathRenderer.js), drawn
 * as z-buffered, Gouraud-shaded triangles with a 2x2 supersample.
 *
 * Space is the parser's (Z up, millimetres). The viewer's scene is Y up with the
 * bed rotated -90deg about X, so its camera direction (0.7, 0.65, 0.7) is
 * (0.7, -0.7, 0.65) here and its up vector is +Z; eye space -- and therefore
 * the shader's eye-space lights -- is identical.
 *
 * Inner loops allocate nothing: under `node --jitless` every short-lived array
 * costs the interpreter dearly (spec §6.3).
 */
import type { SegmentData } from '../lib/vendor/toolpathRenderer.js';
import type { Bounds } from './protocol';

// libvgcode shader constants -- keep in step with toolpathRenderer.js.
const LTX = -0.4574957, LTY = 0.4574957, LTZ = 0.7624929; // light_top_dir
const TOP_DIFFUSE = 0.6 * 0.8;
const TOP_SPECULAR = 0.6 * 0.125;
const TOP_SHININESS = 20.0;
const LFX = 0.6985074, LFY = 0.1397015, LFZ = 0.6985074; // light_front_dir
const FRONT_DIFFUSE = 0.6 * 0.3;
const AMBIENT = 0.3;
const EMISSION = 0.15;

/** horizontal_vertical_view_signs_array: vertex_id + 8 * is_vertical_view -> (x, y). */
const SIGN_X = [1, 0, 0, 0, 0, 1, 0, 0, 0, -1, 0, 1, 1, 0, -1, 0];
const SIGN_Y = [0, 1, 0, -1, -1, 0, 1, 0, 1, 0, 0, 0, 0, 1, 0, 0];
/** The segment template's triangles (VERTEX_DATA). */
const TRIANGLES = [0, 1, 2, 0, 2, 3, 0, 3, 4, 0, 4, 5, 0, 5, 6, 0, 6, 1, 5, 4, 7, 5, 7, 6];

export const FOV_DEGREES = 35;
/** The viewer's (0.7, 0.65, 0.7), expressed in data space and normalised in frameCamera. */
export const VIEW_DIRECTION: readonly [number, number, number] = [0.7, -0.7, 0.65];

export interface Camera {
  ex: number; ey: number; ez: number; // eye
  sx: number; sy: number; sz: number; // right
  ux: number; uy: number; uz: number; // up
  fx: number; fy: number; fz: number; // forward
  focal: number; // 1 / tan(fov / 2)
  near: number;
  far: number;
}

/** Perspective 35deg, distance 1.10 x radius / sin(fov/2) from the centre of the bounds (spec §5.2). */
export function frameCamera(bounds: Bounds): Camera {
  const [x0, y0, z0] = bounds.min;
  const [x1, y1, z1] = bounds.max;
  const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2, cz = (z0 + z1) / 2;
  const radius = Math.max(Math.hypot(x1 - x0, y1 - y0, z1 - z0) / 2, 0.001);
  const half = (FOV_DEGREES * Math.PI) / 360;
  const distance = (1.1 * radius) / Math.sin(half);
  const [dx0, dy0, dz0] = VIEW_DIRECTION;
  const dl = Math.hypot(dx0, dy0, dz0);
  const ex = cx + (dx0 / dl) * distance, ey = cy + (dy0 / dl) * distance, ez = cz + (dz0 / dl) * distance;
  let fx = cx - ex, fy = cy - ey, fz = cz - ez;
  const fl = Math.hypot(fx, fy, fz);
  fx /= fl; fy /= fl; fz /= fl;
  // right = forward x up(0, 0, 1); up' = right x forward
  let sx = fy, sy = -fx;
  const sl = Math.hypot(sx, sy);
  sx /= sl; sy /= sl;
  const sz = 0;
  const ux = sy * fz - sz * fy, uy = sz * fx - sx * fz, uz = sx * fy - sy * fx;
  return {
    ex, ey, ez, sx, sy, sz, ux, uy, uz, fx, fy, fz,
    focal: 1 / Math.tan(half),
    near: Math.max(distance / 1000, 0.01),
    far: distance + radius * 4,
  };
}

/**
 * Bounds of the DRAWN geometry, not of the toolpath axes (spec §5.2): every
 * vertex widened by max(line width, layer height), which covers the half-width
 * sides, the pointed caps and the half-height top and bottom of the bead. Framing
 * the axes alone cut small parts off or lost them (consilium N3).
 */
export function geometryBounds(data: SegmentData): Bounds | null {
  if (data.nV === 0) return null;
  const pos = data.position, hwa = data.hwa;
  let x0 = Infinity, y0 = Infinity, z0 = Infinity, x1 = -Infinity, y1 = -Infinity, z1 = -Infinity;
  for (let v = 0; v < data.nV; v++) {
    const e = Math.max(hwa[v * 4], hwa[v * 4 + 1]);
    const x = pos[v * 4], y = pos[v * 4 + 1], z = pos[v * 4 + 2];
    if (x - e < x0) x0 = x - e;
    if (y - e < y0) y0 = y - e;
    if (z - e < z0) z0 = z - e;
    if (x + e > x1) x1 = x + e;
    if (y + e > y1) y1 = y + e;
    if (z + e > z1) z1 = z + e;
  }
  return { min: [x0, y0, z0], max: [x1, y1, z1] };
}

/** True when no sample of the resolved image is covered. */
export function isEmpty(rgba: Uint8Array): boolean {
  for (let i = 3; i < rgba.length; i += 4) if (rgba[i] !== 0) return false;
  return true;
}

/** The shader's `lighting(eye_position, eye_normal)`; the normal must be unit length. */
export function lighting(px: number, py: number, pz: number, nx: number, ny: number, nz: number): number {
  const top = nx * LTX + ny * LTY + nz * LTZ;
  const front = nx * LFX + ny * LFY + nz * LFZ;
  // pow(max(dot(-normalize(eye_position), reflect(-light_top_dir, eye_normal)), 0), shininess)
  const pl = Math.sqrt(px * px + py * py + pz * pz) || 1;
  const d = -top; // dot(N, -L)
  const rx = -LTX - 2 * d * nx, ry = -LTY - 2 * d * ny, rz = -LTZ - 2 * d * nz;
  const spec = Math.max(-(px * rx + py * ry + pz * rz) / pl, 0);
  return (
    AMBIENT +
    TOP_DIFFUSE * Math.max(top, 0) +
    FRONT_DIFFUSE * Math.max(front, 0) +
    TOP_SPECULAR * Math.pow(spec, TOP_SHININESS) +
    EMISSION
  );
}

/** Scratch for the 8 template vertices of one segment: screen x, y, ndc z, rgb. */
export class SegmentVertices {
  readonly x = new Float64Array(8);
  readonly y = new Float64Array(8);
  readonly z = new Float64Array(8);
  readonly r = new Float64Array(8);
  readonly g = new Float64Array(8);
  readonly b = new Float64Array(8);
  /** False when a vertex sits at or behind the near plane -- the segment is skipped. */
  ok = true;
}

/**
 * The vertex shader for one segment (vertices `ia` and `ia + 1` of `data`),
 * with POINTY_CAPS and FIX_TWISTING. `rgb` holds the base colour per vertex;
 * `pixels` is the side of the square target in samples.
 */
export function segmentVertices(
  data: SegmentData, ia: number, rgb: Float32Array, cam: Camera, pixels: number, out: SegmentVertices,
): void {
  const pos = data.position, hwa = data.hwa;
  const ib = ia + 1;
  const ax = pos[ia * 4], ay = pos[ia * 4 + 1], az = pos[ia * 4 + 2];
  const bx = pos[ib * 4], by = pos[ib * 4 + 1], bz = pos[ib * 4 + 2];
  let lx = bx - ax, ly = by - ay, lz = bz - az;
  const ll = Math.sqrt(lx * lx + ly * ly + lz * lz);
  if (ll < 1e-4) { lx = 1; ly = 0; lz = 0; } else { lx /= ll; ly /= ll; lz /= ll; }
  // line_right_dir: cross(x, line) near vertical, else cross(line, UP)
  let rx: number, ry: number, rz: number;
  if (Math.abs(lz) > 0.9) { rx = 0; ry = -lz; rz = ly; } else { rx = ly; ry = -lx; rz = 0; }
  const rl = Math.sqrt(rx * rx + ry * ry + rz * rz);
  rx /= rl; ry /= rl; rz /= rl;
  // line_up_dir = cross(right, line)
  let upx = ry * lz - rz * ly, upy = rz * lx - rx * lz, upz = rx * ly - ry * lx;
  const ul = Math.sqrt(upx * upx + upy * upy + upz * upz);
  upx /= ul; upy /= ul; upz /= ul;

  const dax = ax - cam.ex, day = ay - cam.ey, daz = az - cam.ez;
  const dbx = bx - cam.ex, dby = by - cam.ey, dbz = bz - cam.ez;
  const closerA = dax * dax + day * day + daz * daz < dbx * dbx + dby * dby + dbz * dbz;
  const cid = closerA ? ia : ib;
  let vx = closerA ? dax : dbx, vy = closerA ? day : dby, vz = closerA ? daz : dbz;
  const vl = Math.sqrt(vx * vx + vy * vy + vz * vz);
  vx /= vl; vy /= vl; vz /= vl;
  const ch = hwa[cid * 4], cw = hwa[cid * 4 + 1];
  let gx = ch * upx + cw * rx, gy = ch * upy + cw * ry, gz = ch * upz + cw * rz;
  const gl = Math.sqrt(gx * gx + gy * gy + gz * gz);
  gx /= gl; gy /= gl; gz /= gl;
  const vUp = vx * upx + vy * upy + vz * upz, vRight = vx * rx + vy * ry + vz * rz;
  const vertical =
    Math.abs(vUp) / Math.abs(gx * upx + gy * upy + gz * upz) > Math.abs(vRight) / Math.abs(gx * rx + gy * ry + gz * rz);
  const rightSign = Math.sign(-vRight), topSign = Math.sign(-vUp);
  const signBase = vertical ? 8 : 0;

  const near = cam.near, far = cam.far, focal = cam.focal;
  const za = (far + near) / (far - near), zb = (2 * far * near) / (far - near);
  out.ok = true;
  for (let v = 0; v < 8; v++) {
    const id = v < 4 ? ia : ib;
    const px0 = v < 4 ? ax : bx, py0 = v < 4 ? ay : by, pz0 = v < 4 ? az : bz;
    const halfH = 0.5 * hwa[id * 4], halfW = 0.5 * hwa[id * 4 + 1], angle = hwa[id * 4 + 2];
    const hs = SIGN_X[v + signBase] * rightSign, vs = SIGN_Y[v + signBase] * topSign;
    let px = px0 + hs * halfW * rx + vs * halfH * upx;
    let py = py0 + hs * halfW * ry + vs * halfH * upy;
    let pz = pz0 + hs * halfW * rz + vs * halfH * upz;
    if (v === 2 || v === 7) {
      const along = v === 2 ? -1 : 1;
      if (angle === 0) {
        px += along * lx * halfW; py += along * ly * halfW; pz += along * lz * halfW;
      } else {
        const s = along * halfW * Math.sin(Math.abs(angle) * 0.5);
        const c = Math.sign(angle) * halfW * Math.cos(Math.abs(angle) * 0.5);
        px += s * lx + c * rx; py += s * ly + c * ry; pz += s * lz + c * rz;
      }
    }
    // eye space
    const dx = px - cam.ex, dy = py - cam.ey, dz = pz - cam.ez;
    const ex = dx * cam.sx + dy * cam.sy + dz * cam.sz;
    const ey = dx * cam.ux + dy * cam.uy + dz * cam.uz;
    const ez = -(dx * cam.fx + dy * cam.fy + dz * cam.fz);
    let nx = px - px0, ny = py - py0, nz = pz - pz0;
    const nl = Math.sqrt(nx * nx + ny * ny + nz * nz);
    if (nl > 0) { nx /= nl; ny /= nl; nz /= nl; } else { nx = lx; ny = ly; nz = lz; }
    const enx = nx * cam.sx + ny * cam.sy + nz * cam.sz;
    const eny = nx * cam.ux + ny * cam.uy + nz * cam.uz;
    const enz = -(nx * cam.fx + ny * cam.fy + nz * cam.fz);
    const light = lighting(ex, ey, ez, enx, eny, enz);
    out.r[v] = rgb[id * 3] * light;
    out.g[v] = rgb[id * 3 + 1] * light;
    out.b[v] = rgb[id * 3 + 2] * light;
    const w = -ez;
    if (!(w > near)) out.ok = false;
    out.x[v] = ((focal * ex) / w * 0.5 + 0.5) * pixels;
    out.y[v] = (0.5 - (focal * ey) / w * 0.5) * pixels;
    out.z[v] = (za * ez + zb) / ez;
  }
}

/** Depth and colour samples of one render target (side = size x supersample). */
export class RenderTarget {
  readonly side: number;
  readonly depth: Float32Array;
  readonly rgb: Float32Array;
  readonly size: number;
  readonly supersample: number;

  constructor(size: number, supersample: number) {
    this.size = size;
    this.supersample = supersample;
    this.side = size * supersample;
    this.depth = new Float32Array(this.side * this.side);
    this.rgb = new Float32Array(this.side * this.side * 3);
  }

  clear(): void {
    this.depth.fill(Infinity);
    this.rgb.fill(0);
  }
}

/** One Gouraud triangle of `s` into `t` with the depth test (nearer ndc z wins); exported for tests. */
export function rasterTriangle(t: RenderTarget, s: SegmentVertices, a: number, b: number, c: number): void {
  const x0 = s.x[a], y0 = s.y[a], x1 = s.x[b], y1 = s.y[b], x2 = s.x[c], y2 = s.y[c];
  const area = (x1 - x0) * (y2 - y0) - (x2 - x0) * (y1 - y0);
  if (area === 0 || !Number.isFinite(area)) return;
  const side = t.side;
  const minX = Math.max(0, Math.floor(Math.min(x0, x1, x2)));
  const maxX = Math.min(side - 1, Math.ceil(Math.max(x0, x1, x2)));
  const minY = Math.max(0, Math.floor(Math.min(y0, y1, y2)));
  const maxY = Math.min(side - 1, Math.ceil(Math.max(y0, y1, y2)));
  if (minX > maxX || minY > maxY) return;
  const inv = 1 / area;
  // barycentrics are linear in the sample position: w = k + dx * x + dy * y
  const w0dx = (y1 - y2) * inv, w0dy = (x2 - x1) * inv, w0k = (x1 * y2 - x2 * y1) * inv;
  const w1dx = (y2 - y0) * inv, w1dy = (x0 - x2) * inv, w1k = (x2 * y0 - x0 * y2) * inv;
  const z0 = s.z[a], z1 = s.z[b], z2 = s.z[c];
  const depth = t.depth, rgb = t.rgb;
  for (let py = minY; py <= maxY; py++) {
    const sy = py + 0.5;
    let w0 = w0k + w0dx * (minX + 0.5) + w0dy * sy;
    let w1 = w1k + w1dx * (minX + 0.5) + w1dy * sy;
    let o = py * side + minX;
    for (let px = minX; px <= maxX; px++, o++, w0 += w0dx, w1 += w1dx) {
      const w2 = 1 - w0 - w1;
      if (w0 < 0 || w1 < 0 || w2 < 0) continue;
      const z = w0 * z0 + w1 * z1 + w2 * z2;
      if (z < -1 || z > 1 || z >= depth[o]) continue;
      depth[o] = z;
      const q = o * 3;
      rgb[q] = w0 * s.r[a] + w1 * s.r[b] + w2 * s.r[c];
      rgb[q + 1] = w0 * s.g[a] + w1 * s.g[b] + w2 * s.g[c];
      rgb[q + 2] = w0 * s.b[a] + w1 * s.b[b] + w2 * s.b[c];
    }
  }
}

/** Draws every segment of `data` into `target` (which the caller cleared). */
export function drawSegments(data: SegmentData, rgb: Float32Array, cam: Camera, target: RenderTarget): void {
  const scratch = new SegmentVertices();
  const seg = data.segIndex;
  for (let i = 0; i < data.nSeg; i++) {
    segmentVertices(data, seg[i * 4], rgb, cam, target.side, scratch);
    if (!scratch.ok) continue;
    for (let k = 0; k < TRIANGLES.length; k += 3) rasterTriangle(target, scratch, TRIANGLES[k], TRIANGLES[k + 1], TRIANGLES[k + 2]);
  }
}

/** Resolves the supersample to straight-alpha RGBA8; colour is clamped per sample, as the framebuffer does. */
export function resolve(target: RenderTarget): Uint8Array {
  const { size, supersample: ss, side, depth, rgb } = target;
  const out = new Uint8Array(size * size * 4);
  const full = ss * ss;
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      let r = 0, g = 0, b = 0, n = 0;
      for (let j = 0; j < ss; j++) {
        let o = (y * ss + j) * side + x * ss;
        for (let i = 0; i < ss; i++, o++) {
          if (depth[o] === Infinity) continue;
          r += Math.min(rgb[o * 3], 1); g += Math.min(rgb[o * 3 + 1], 1); b += Math.min(rgb[o * 3 + 2], 1);
          n++;
        }
      }
      if (n === 0) continue;
      const q = (y * size + x) * 4;
      out[q] = Math.round((r / n) * 255);
      out[q + 1] = Math.round((g / n) * 255);
      out[q + 2] = Math.round((b / n) * 255);
      out[q + 3] = Math.round((n / full) * 255);
    }
  }
  return out;
}
