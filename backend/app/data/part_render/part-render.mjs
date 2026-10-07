import { createHash } from "node:crypto";
import { deflateSync } from "node:zlib";
//#region src/lib/gcodeToolpath.ts
/**
* Parses a G-code file into the per-layer form the vendored libvgcode renderer
* consumes (`src/lib/vendor/toolpathRenderer.js`).
*
* This is the piece that does not exist upstream. `three-slicer` renders its own
* slicing kernel's output and ships no G-code parser at all, so a preview of a
* *file* -- which is all BamDude ever has -- needs the toolpath reconstructed
* from the text.
*
* The renderer's input is one entry per layer:
*
*     { z, paths: Float32Array (stride 8), widths: number[] }
*
* where each stride-8 record is `x0, y0, z0, type, x1, y1, z1, _` and `type` is
* 0 for a travel move or a feature index otherwise. Layer height is derived by
* the renderer from the gaps between consecutive `z` values, so layers must
* arrive in print order.
*
* Deliberately hand-rolled rather than reusing `gcode-preview`'s parser: that
* one models moves for a line renderer and keeps `;TYPE:` only as an opaque
* comment string, so the feature classification below -- the thing that makes a
* preview readable -- would have to be written here anyway.
*/
/**
* Feature indices the renderer's palette is keyed on. Values are fixed by
* `TYPE_COLOR` in the vendored module, which took them from libvgcode; changing
* one silently recolours the preview.
*/
const ToolpathType = {
	travel: 0,
	wall: 1,
	sparseInfill: 2,
	solidInfill: 3,
	skirt: 4,
	support: 5,
	raft: 6,
	gapFill: 7,
	thinWall: 8,
	bridge: 9,
	ironing: 10,
	primeTower: 11
};
/**
* `;TYPE:` values as OrcaSlicer and BambuStudio emit them, lowercased.
*
* Both spell several of these differently across versions ("Overhang wall" vs
* "Overhang perimeter"), and PrusaSlicer-lineage names turn up in third-party
* files, so the table is deliberately generous. Anything unrecognised falls
* back to `wall`, which is visually neutral -- better a mis-coloured segment
* than a missing one, since an unknown type must never drop geometry.
*/
const FEATURE_BY_COMMENT = {
	"outer wall": ToolpathType.wall,
	"inner wall": ToolpathType.wall,
	perimeter: ToolpathType.wall,
	"external perimeter": ToolpathType.wall,
	"overhang wall": ToolpathType.bridge,
	"overhang perimeter": ToolpathType.bridge,
	"sparse infill": ToolpathType.sparseInfill,
	"internal infill": ToolpathType.sparseInfill,
	"solid infill": ToolpathType.solidInfill,
	"internal solid infill": ToolpathType.solidInfill,
	"top surface": ToolpathType.solidInfill,
	"top solid infill": ToolpathType.solidInfill,
	"bottom surface": ToolpathType.solidInfill,
	skirt: ToolpathType.skirt,
	"skirt/brim": ToolpathType.skirt,
	brim: ToolpathType.skirt,
	support: ToolpathType.support,
	"support material": ToolpathType.support,
	"support interface": ToolpathType.support,
	"support material interface": ToolpathType.support,
	"support transition": ToolpathType.support,
	raft: ToolpathType.raft,
	"gap fill": ToolpathType.gapFill,
	"gap infill": ToolpathType.gapFill,
	"thin wall": ToolpathType.thinWall,
	"floating vertical shell": ToolpathType.solidInfill,
	"internal bridge": ToolpathType.bridge,
	"bottom shell": ToolpathType.solidInfill,
	bridge: ToolpathType.bridge,
	"bridge infill": ToolpathType.bridge,
	"internal bridge infill": ToolpathType.bridge,
	ironing: ToolpathType.ironing,
	"prime tower": ToolpathType.primeTower,
	"wipe tower": ToolpathType.primeTower,
	custom: ToolpathType.wall
};
const OBJECT_START = /^; start printing object, unique label id: (\d+)$/;
const OBJECT_STOP = /^; stop printing object, unique label id: (\d+)$/;
const RECORD_STRIDE = 8;
const TAU = Math.PI * 2;
/** Chord flatness for arc interpolation, in mm. Below an extrusion width. */
const ARC_TOLERANCE_MM = .02;
/** Ceiling on chords per arc, so a huge radius cannot blow up the buffer. */
const ARC_MAX_CHORDS = 256;
/** Tool numbers above this are slicer sentinels, not filaments. */
const MAX_TOOL = 15;
/** Growable stride-8 record buffer; typed arrays cannot be pushed to. */
var PathBuffer = class {
	data = new Float32Array(1024 * RECORD_STRIDE);
	owners = /* @__PURE__ */ new Int32Array(1024);
	flagBytes = /* @__PURE__ */ new Uint8Array(1024);
	count = 0;
	widths = [];
	push(x0, y0, z0, type, x1, y1, z1, width, tool, objectId = -1, flags = 0) {
		if ((this.count + 1) * RECORD_STRIDE > this.data.length) {
			const grown = new Float32Array(this.data.length * 2);
			grown.set(this.data);
			this.data = grown;
			const owners = new Int32Array(this.owners.length * 2);
			owners.set(this.owners);
			this.owners = owners;
			const flagBytes = new Uint8Array(this.flagBytes.length * 2);
			flagBytes.set(this.flagBytes);
			this.flagBytes = flagBytes;
		}
		const o = this.count * RECORD_STRIDE;
		this.data[o] = x0;
		this.data[o + 1] = y0;
		this.data[o + 2] = z0;
		this.data[o + 3] = type;
		this.data[o + 4] = x1;
		this.data[o + 5] = y1;
		this.data[o + 6] = z1;
		this.data[o + 7] = tool;
		this.owners[this.count] = objectId;
		this.flagBytes[this.count] = flags;
		this.count += 1;
		this.widths.push(width);
	}
	get length() {
		return this.count;
	}
	/** Trimmed copy — the renderer walks the whole array, so slack would render. */
	toFloat32Array() {
		return this.data.slice(0, this.count * RECORD_STRIDE);
	}
	toObjectIds() {
		return this.owners.slice(0, this.count);
	}
	toFlags() {
		return this.flagBytes.slice(0, this.count);
	}
	toLayer(z) {
		return {
			z,
			paths: this.toFloat32Array(),
			widths: this.widths,
			objectIds: this.toObjectIds(),
			flags: this.toFlags()
		};
	}
};
/** Most frequently seen key, or undefined when the tally is empty. */
function modeOf(tally) {
	let best;
	let bestCount = 0;
	for (const [value, count] of tally) if (count > bestCount) {
		best = value;
		bestCount = count;
	}
	return best;
}
/** Reads a named axis out of a `G0`/`G1` line without allocating per token. */
function readAxis(line, axis) {
	const at = line.indexOf(axis);
	if (at < 0) return void 0;
	const value = Number.parseFloat(line.slice(at + 1));
	return Number.isFinite(value) ? value : void 0;
}
/**
* Parse G-code into per-layer toolpath records.
*
* Relative extrusion (`M83`) and absolute (`M82`) are both handled, because
* Bambu writes relative and plenty of third-party files do not. Anything the
* parser cannot make sense of is skipped rather than guessed at.
*/
function parseGcodeToolpath(gcode) {
	const layers = [];
	let x = 0;
	let y = 0;
	let z = 0;
	let e = 0;
	let relativeExtrusion = false;
	let feature = ToolpathType.wall;
	let width = 0;
	const widthTally = /* @__PURE__ */ new Map();
	let segmentCount = 0;
	let travelCount = 0;
	let current = new PathBuffer();
	let currentZ = 0;
	let sawLayerMarker = false;
	let pendingZ = null;
	let layerHasExtrusion = false;
	let tool = 0;
	let objectId = -1;
	let featureExplicit = false;
	let hasPosition = false;
	let minX = Infinity, minY = Infinity, minZ = Infinity;
	let maxX = -Infinity, maxY = -Infinity, maxZ = -Infinity;
	const flushLayer = () => {
		if (current.length === 0) return;
		layers.push(current.toLayer(currentZ));
		current = new PathBuffer();
		layerHasExtrusion = false;
		if (pendingZ !== null) {
			currentZ = pendingZ;
			pendingZ = null;
		}
	};
	/**
	* Record one straight run from the current position, updating bounds. Shared
	* by linear moves and by each chord an arc is flattened into.
	*/
	const emit = (nx, ny, nz, extruding) => {
		if (extruding) {
			if (!sawLayerMarker && layerHasExtrusion && nz !== currentZ) flushLayer();
			if (!layerHasExtrusion) {
				currentZ = nz;
				pendingZ = null;
			}
			layerHasExtrusion = true;
			if (!hasPosition) {
				hasPosition = true;
				x = nx;
				y = ny;
				z = nz;
				return;
			}
			const flags = (featureExplicit ? 1 : 0) | (sawLayerMarker ? 2 : 0);
			current.push(x, y, z, feature, nx, ny, nz, width, tool, objectId, flags);
			segmentCount += 1;
			if (nx < minX) minX = nx;
			if (ny < minY) minY = ny;
			if (nz < minZ) minZ = nz;
			if (nx > maxX) maxX = nx;
			if (ny > maxY) maxY = ny;
			if (nz > maxZ) maxZ = nz;
		} else if (hasPosition) {
			current.push(x, y, z, ToolpathType.travel, nx, ny, nz, 0, tool, objectId, 0);
			travelCount += 1;
		}
		x = nx;
		y = ny;
		z = nz;
		hasPosition = true;
	};
	for (const rawLine of gcode.split("\n")) {
		const line = rawLine.trim();
		if (line.length === 0) continue;
		if (line.charCodeAt(0) === 59) {
			const start = OBJECT_START.exec(line);
			if (start) {
				objectId = Number(start[1]);
				continue;
			}
			const stop = OBJECT_STOP.exec(line);
			if (stop) {
				if (Number(stop[1]) === objectId) objectId = -1;
				continue;
			}
			const body = line.slice(1).trimStart();
			const colon = body.indexOf(":");
			const key = (colon >= 0 ? body.slice(0, colon) : body).trim().toUpperCase();
			const value = colon >= 0 ? body.slice(colon + 1).trim() : "";
			if (key === "FEATURE" || key === "TYPE") {
				const name = value.toLowerCase();
				const known = FEATURE_BY_COMMENT[name];
				feature = known ?? ToolpathType.wall;
				featureExplicit = known !== void 0 && name !== "custom";
			} else if (key === "LINE_WIDTH" || key === "WIDTH") {
				const parsed = Number.parseFloat(value);
				if (Number.isFinite(parsed) && parsed > 0) {
					width = parsed;
					widthTally.set(parsed, (widthTally.get(parsed) ?? 0) + 1);
				}
			} else if (key === "CHANGE_LAYER" || key === "LAYER_CHANGE") {
				sawLayerMarker = true;
				flushLayer();
			} else if (key === "Z_HEIGHT" || key === "Z") {
				const parsed = Number.parseFloat(value);
				if (Number.isFinite(parsed)) pendingZ = parsed;
			}
			continue;
		}
		if (line.startsWith("M83")) {
			relativeExtrusion = true;
			continue;
		}
		if (line.startsWith("M82")) {
			relativeExtrusion = false;
			continue;
		}
		if (line.startsWith("G92")) {
			const resetE = readAxis(line, "E");
			if (resetE !== void 0) e = resetE;
			continue;
		}
		if (line.charCodeAt(0) === 84) {
			const picked = Number.parseInt(line.slice(1), 10);
			if (Number.isFinite(picked) && picked >= 0 && picked <= MAX_TOOL) tool = picked;
			continue;
		}
		const isArc = line.startsWith("G2 ") || line.startsWith("G3 ") || line.startsWith("G2") || line.startsWith("G3");
		const isLinear = line.startsWith("G1") || line.startsWith("G0");
		if (!isLinear && !isArc) continue;
		if (isArc && !/^G[23](\s|$)/.test(line)) continue;
		if (isLinear && !/^G[01](\s|$)/.test(line)) continue;
		const nx = readAxis(line, "X") ?? x;
		const ny = readAxis(line, "Y") ?? y;
		const nz = readAxis(line, "Z") ?? z;
		const rawE = readAxis(line, "E");
		let extruded = 0;
		if (rawE !== void 0) {
			extruded = relativeExtrusion ? rawE : rawE - e;
			e = rawE;
		}
		const extruding = extruded > 0;
		if (isArc) {
			const i = readAxis(line, "I") ?? 0;
			const j = readAxis(line, "J") ?? 0;
			const cx = x + i;
			const cy = y + j;
			const radius = Math.hypot(i, j);
			if (radius > 0) {
				const startAngle = Math.atan2(y - cy, x - cx);
				const endAngle = Math.atan2(ny - cy, nx - cx);
				const clockwise = line.charCodeAt(1) === 50;
				let sweep = endAngle - startAngle;
				if (clockwise) {
					while (sweep >= 0) sweep -= TAU;
					while (sweep < -TAU) sweep += TAU;
				} else {
					while (sweep <= 0) sweep += TAU;
					while (sweep > TAU) sweep -= TAU;
				}
				if (nx === x && ny === y) {
					const turns = Math.max(1, Math.round(readAxis(line, "P") ?? 1));
					sweep = (clockwise ? -TAU : TAU) * turns;
				}
				const maxStep = 2 * Math.acos(Math.max(-1, Math.min(1, 1 - ARC_TOLERANCE_MM / radius)));
				const steps = Math.max(1, Math.min(ARC_MAX_CHORDS, Math.ceil(Math.abs(sweep) / Math.max(maxStep, .001))));
				for (let step = 1; step <= steps; step += 1) {
					const fraction = step / steps;
					const angle = startAngle + sweep * fraction;
					emit(cx + radius * Math.cos(angle), cy + radius * Math.sin(angle), z + (nz - z) * fraction, extruding);
				}
				continue;
			}
		}
		if (nx !== x || ny !== y || nz !== z) emit(nx, ny, nz, extruding);
	}
	flushLayer();
	return {
		layers,
		segmentCount,
		travelCount,
		defaultWidth: modeOf(widthTally) ?? .42,
		bounds: segmentCount > 0 ? {
			min: [
				minX,
				minY,
				minZ
			],
			max: [
				maxX,
				maxY,
				maxZ
			]
		} : null
	};
}
//#endregion
//#region src/lib/vendor/toolpathRenderer.js
const U = {
	0: [
		.42,
		.45,
		.5
	],
	1: [
		.85,
		.51,
		.17
	],
	2: [
		.21,
		.45,
		.76
	],
	3: [
		.35,
		.75,
		.85
	],
	4: [
		.16,
		.68,
		.4
	],
	5: [
		.66,
		.42,
		.85
	],
	6: [
		.55,
		.45,
		.35
	],
	7: [
		.95,
		.85,
		.25
	],
	8: [
		.9,
		.35,
		.65
	],
	9: [
		.9,
		.25,
		.25
	],
	10: [
		.6,
		.82,
		.55
	],
	11: [
		.3,
		.72,
		.7
	]
};
function ie(t) {
	const i = Math.round(t[0] * 255), s = Math.round(t[1] * 255), n = Math.round(t[2] * 255);
	return i << 16 | s << 8 | n;
}
function ue(t, i) {
	const s = t.length, n = i > 0 ? i : .42, l = new Array(s);
	for (let e = 0; e < s; e++) {
		const o = t[e].z;
		l[e] = Math.max(.02, e === 0 ? o : o - t[e - 1].z);
	}
	const a = [], h = [], _ = [], y = [], v = [], r = [], x = [], b = [], A = [], M = [], L = /* @__PURE__ */ new Float64Array(16), P = [], k = [];
	let W = -1, $ = 0, R = 0, H = 0, E = -1, c = 0;
	const d = 1e-4, u = (e, o, g, m, p, w) => (a.push(e), h.push(o), _.push(g), r.push(m), y.push(p), v.push(w), x.push(c), b.push(!1), a.length - 1);
	for (let e = 0; e < s; e++) {
		const o = t[e].paths, g = t[e].widths, m = l[e];
		if (c = e, !!o) for (let p = 0; p < o.length; p += 8) {
			const w = o[p + 3], F = o[p], I = o[p + 1], G = o[p + 2], S = o[p + 4], V = o[p + 5], B = o[p + 6];
			if (w === 0) {
				P.push(F, I, G, S, V, B), k.push(e);
				continue;
			}
			const O = g && g[p / 8] > 0 ? g[p / 8] : n;
			w < 16 && (L[w] += Math.hypot(S - F, V - I));
			let D;
			W >= 0 && E === w && Math.abs($ - F) < d && Math.abs(R - I) < d && Math.abs(H - G) < d ? (D = W, u(S, V, B, w, m, O)) : (D = u(F, I, G, w, m, O), u(S, V, B, w, m, O)), b[D] = !0, W = D + 1, $ = S, R = V, H = B, E = w, A.push(D), M.push(e);
		}
	}
	const f = a.length, C = A.length, z = new Float32Array(f * 4), Y = new Float32Array(f * 4);
	let ee = 0, ne = !1, j = Infinity, T = Infinity, X = Infinity, Z = -Infinity, q = -Infinity, J = -Infinity;
	for (let e = 0; e < f; e++) {
		const o = y[e], g = a[e], m = h[e], p = _[e] - .5 * o;
		z[e * 4] = g, z[e * 4 + 1] = m, z[e * 4 + 2] = p;
		const w = e > 0 && b[e - 1], F = b[e];
		let I = 0;
		if (w || F) {
			const G = w ? a[e] - a[e - 1] : 0, S = w ? h[e] - h[e - 1] : 0, V = w ? _[e] - _[e - 1] : 0, B = F ? a[e + 1] - a[e] : 0, O = F ? h[e + 1] - h[e] : 0, D = F ? _[e + 1] - _[e] : 0;
			I = Math.atan2(G * O - S * B, G * B + S * O + V * D);
		}
		Y[e * 4] = o, Y[e * 4 + 1] = v[e], Y[e * 4 + 2] = I, Y[e * 4 + 3] = ie(U[r[e]] || U[1]), (!Number.isFinite(g) || !Number.isFinite(m) || !Number.isFinite(p) || !Number.isFinite(I)) && (ne = !0), ee = Math.max(ee, Math.abs(g), Math.abs(m), Math.abs(p)), g < j && (j = g), g > Z && (Z = g), m < T && (T = m), m > q && (q = m), p < X && (X = p), p > J && (J = p);
	}
	const te = new Uint32Array(C * 4);
	for (let e = 0; e < C; e++) te[e * 4] = A[e], te[e * 4 + 1] = M[e];
	const K = {
		vType: new Uint8Array(f),
		vWidth: new Float32Array(f),
		vHeight: new Float32Array(f),
		vLayer: new Int32Array(f)
	};
	for (let e = 0; e < f; e++) K.vType[e] = r[e], K.vWidth[e] = v[e], K.vHeight[e] = y[e], K.vLayer[e] = x[e];
	const oe = new Int32Array(s + 1);
	{
		let e = 0;
		for (let o = 0; o < s; o++) {
			for (; e < C && M[e] === o;) e++;
			oe[o + 1] = e;
		}
	}
	const Q = k.length, N = new Float32Array(Q * 6);
	for (let e = 0; e < N.length; e++) N[e] = P[e];
	for (let e = 0; e < N.length; e += 3) {
		const o = N[e], g = N[e + 1], m = N[e + 2];
		o < j && (j = o), o > Z && (Z = o), g < T && (T = g), g > q && (q = g), m < X && (X = m), m > J && (J = m);
	}
	const re = new Int32Array(s + 1);
	{
		let e = 0;
		for (let o = 0; o < s; o++) {
			for (; e < Q && k[e] === o;) e++;
			re[o + 1] = e;
		}
	}
	const se = f + Q > 0 ? {
		min: [
			j,
			T,
			X
		],
		max: [
			Z,
			q,
			J
		]
	} : null;
	return {
		position: z,
		hwa: Y,
		segIndex: te,
		nV: f,
		nSeg: C,
		layerSegPrefix: oe,
		travelPos: N,
		travelPrefix: re,
		nTrav: Q,
		layerCount: s,
		maxAbs: ee,
		hasNaN: ne,
		meta: K,
		typeLengths: L,
		bbox: se
	};
}
[
	[
		11,
		44,
		122
	],
	[
		19,
		89,
		133
	],
	[
		28,
		136,
		145
	],
	[
		4,
		214,
		15
	],
	[
		170,
		242,
		0
	],
	[
		252,
		249,
		3
	],
	[
		245,
		206,
		10
	],
	[
		227,
		136,
		32
	],
	[
		209,
		104,
		48
	],
	[
		194,
		82,
		60
	],
	[
		148,
		38,
		22
	]
].map((t) => [
	t[0] / 255,
	t[1] / 255,
	t[2] / 255
]);
//#endregion
//#region src/part-render/protocol.ts
/** Largest manifest / error payload (spec §5.6). */
const MANIFEST_MAX = 1024 * 1024;
/**
* An allocation the heap could not satisfy -- a memory limit, not a crash and not a parse
* failure (consilium N6). Asked by name, not instanceof: the engine's RangeError can come
* from another realm (vitest's jsdom environment).
*/
function isAllocationFailure(error) {
	if (typeof error !== "object" || error === null || error.name !== "RangeError") return false;
	return /allocation failed|invalid (typed )?array length/i.test(String(error.message));
}
var RenderError = class extends Error {
	reason;
	constructor(reason, message) {
		super(message);
		this.name = "RenderError";
		this.reason = reason;
	}
};
//#endregion
//#region src/part-render/raster.ts
const LTX = -.4574957, LTY = .4574957, LTZ = .7624929;
const TOP_DIFFUSE = .6 * .8;
const TOP_SPECULAR = .6 * .125;
const TOP_SHININESS = 20;
const LFX = .6985074, LFY = .1397015, LFZ = .6985074;
const FRONT_DIFFUSE = .6 * .3;
const AMBIENT = .3;
const EMISSION = .15;
/** horizontal_vertical_view_signs_array: vertex_id + 8 * is_vertical_view -> (x, y). */
const SIGN_X = [
	1,
	0,
	0,
	0,
	0,
	1,
	0,
	0,
	0,
	-1,
	0,
	1,
	1,
	0,
	-1,
	0
];
const SIGN_Y = [
	0,
	1,
	0,
	-1,
	-1,
	0,
	1,
	0,
	1,
	0,
	0,
	0,
	0,
	1,
	0,
	0
];
/** The segment template's triangles (VERTEX_DATA). */
const TRIANGLES = [
	0,
	1,
	2,
	0,
	2,
	3,
	0,
	3,
	4,
	0,
	4,
	5,
	0,
	5,
	6,
	0,
	6,
	1,
	5,
	4,
	7,
	5,
	7,
	6
];
/** The viewer's (0.7, 0.65, 0.7), expressed in data space and normalised in frameCamera. */
const VIEW_DIRECTION = [
	.7,
	-.7,
	.65
];
/** Perspective 35deg, distance 1.10 x radius / sin(fov/2) from the centre of the bounds (spec §5.2). */
function frameCamera(bounds) {
	const [x0, y0, z0] = bounds.min;
	const [x1, y1, z1] = bounds.max;
	const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2, cz = (z0 + z1) / 2;
	const radius = Math.max(Math.hypot(x1 - x0, y1 - y0, z1 - z0) / 2, .001);
	const half = 35 * Math.PI / 360;
	const distance = 1.1 * radius / Math.sin(half);
	const [dx0, dy0, dz0] = VIEW_DIRECTION;
	const dl = Math.hypot(dx0, dy0, dz0);
	const ex = cx + dx0 / dl * distance, ey = cy + dy0 / dl * distance, ez = cz + dz0 / dl * distance;
	let fx = cx - ex, fy = cy - ey, fz = cz - ez;
	const fl = Math.hypot(fx, fy, fz);
	fx /= fl;
	fy /= fl;
	fz /= fl;
	let sx = fy, sy = -fx;
	const sl = Math.hypot(sx, sy);
	sx /= sl;
	sy /= sl;
	const sz = 0;
	const ux = sy * fz - sz * fy, uy = sz * fx - sx * fz, uz = sx * fy - sy * fx;
	return {
		ex,
		ey,
		ez,
		sx,
		sy,
		sz,
		ux,
		uy,
		uz,
		fx,
		fy,
		fz,
		focal: 1 / Math.tan(half),
		near: Math.max(distance / 1e3, .01),
		far: distance + radius * 4
	};
}
/**
* Bounds of the DRAWN geometry, not of the toolpath axes (spec §5.2): every
* vertex widened by max(line width, layer height), which covers the half-width
* sides, the pointed caps and the half-height top and bottom of the bead. Framing
* the axes alone cut small parts off or lost them (consilium N3).
*/
function geometryBounds(data) {
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
	return {
		min: [
			x0,
			y0,
			z0
		],
		max: [
			x1,
			y1,
			z1
		]
	};
}
/** True when no sample of the resolved image is covered. */
function isEmpty(rgba) {
	for (let i = 3; i < rgba.length; i += 4) if (rgba[i] !== 0) return false;
	return true;
}
/** The shader's `lighting(eye_position, eye_normal)`; the normal must be unit length. */
function lighting(px, py, pz, nx, ny, nz) {
	const top = nx * LTX + ny * LTY + nz * LTZ;
	const front = nx * LFX + ny * LFY + nz * LFZ;
	const pl = Math.sqrt(px * px + py * py + pz * pz) || 1;
	const d = -top;
	const rx = .4574957 - 2 * d * nx, ry = -.4574957 - 2 * d * ny, rz = -.7624929 - 2 * d * nz;
	const spec = Math.max(-(px * rx + py * ry + pz * rz) / pl, 0);
	return AMBIENT + TOP_DIFFUSE * Math.max(top, 0) + FRONT_DIFFUSE * Math.max(front, 0) + TOP_SPECULAR * Math.pow(spec, TOP_SHININESS) + EMISSION;
}
/** Scratch for the 8 template vertices of one segment: screen x, y, ndc z, rgb. */
var SegmentVertices = class {
	x = /* @__PURE__ */ new Float64Array(8);
	y = /* @__PURE__ */ new Float64Array(8);
	z = /* @__PURE__ */ new Float64Array(8);
	r = /* @__PURE__ */ new Float64Array(8);
	g = /* @__PURE__ */ new Float64Array(8);
	b = /* @__PURE__ */ new Float64Array(8);
	/** False when a vertex sits at or behind the near plane -- the segment is skipped. */
	ok = true;
};
/**
* The vertex shader for one segment (vertices `ia` and `ia + 1` of `data`),
* with POINTY_CAPS and FIX_TWISTING. `rgb` holds the base colour per vertex;
* `pixels` is the side of the square target in samples.
*/
function segmentVertices(data, ia, rgb, cam, pixels, out) {
	const pos = data.position, hwa = data.hwa;
	const ib = ia + 1;
	const ax = pos[ia * 4], ay = pos[ia * 4 + 1], az = pos[ia * 4 + 2];
	const bx = pos[ib * 4], by = pos[ib * 4 + 1], bz = pos[ib * 4 + 2];
	let lx = bx - ax, ly = by - ay, lz = bz - az;
	const ll = Math.sqrt(lx * lx + ly * ly + lz * lz);
	if (ll < 1e-4) {
		lx = 1;
		ly = 0;
		lz = 0;
	} else {
		lx /= ll;
		ly /= ll;
		lz /= ll;
	}
	let rx, ry, rz;
	if (Math.abs(lz) > .9) {
		rx = 0;
		ry = -lz;
		rz = ly;
	} else {
		rx = ly;
		ry = -lx;
		rz = 0;
	}
	const rl = Math.sqrt(rx * rx + ry * ry + rz * rz);
	rx /= rl;
	ry /= rl;
	rz /= rl;
	let upx = ry * lz - rz * ly, upy = rz * lx - rx * lz, upz = rx * ly - ry * lx;
	const ul = Math.sqrt(upx * upx + upy * upy + upz * upz);
	upx /= ul;
	upy /= ul;
	upz /= ul;
	const dax = ax - cam.ex, day = ay - cam.ey, daz = az - cam.ez;
	const dbx = bx - cam.ex, dby = by - cam.ey, dbz = bz - cam.ez;
	const closerA = dax * dax + day * day + daz * daz < dbx * dbx + dby * dby + dbz * dbz;
	const cid = closerA ? ia : ib;
	let vx = closerA ? dax : dbx, vy = closerA ? day : dby, vz = closerA ? daz : dbz;
	const vl = Math.sqrt(vx * vx + vy * vy + vz * vz);
	vx /= vl;
	vy /= vl;
	vz /= vl;
	const ch = hwa[cid * 4], cw = hwa[cid * 4 + 1];
	let gx = ch * upx + cw * rx, gy = ch * upy + cw * ry, gz = ch * upz + cw * rz;
	const gl = Math.sqrt(gx * gx + gy * gy + gz * gz);
	gx /= gl;
	gy /= gl;
	gz /= gl;
	const vUp = vx * upx + vy * upy + vz * upz, vRight = vx * rx + vy * ry + vz * rz;
	const vertical = Math.abs(vUp) / Math.abs(gx * upx + gy * upy + gz * upz) > Math.abs(vRight) / Math.abs(gx * rx + gy * ry + gz * rz);
	const rightSign = Math.sign(-vRight), topSign = Math.sign(-vUp);
	const signBase = vertical ? 8 : 0;
	const near = cam.near, far = cam.far, focal = cam.focal;
	const za = (far + near) / (far - near), zb = 2 * far * near / (far - near);
	out.ok = true;
	for (let v = 0; v < 8; v++) {
		const id = v < 4 ? ia : ib;
		const px0 = v < 4 ? ax : bx, py0 = v < 4 ? ay : by, pz0 = v < 4 ? az : bz;
		const halfH = .5 * hwa[id * 4], halfW = .5 * hwa[id * 4 + 1], angle = hwa[id * 4 + 2];
		const hs = SIGN_X[v + signBase] * rightSign, vs = SIGN_Y[v + signBase] * topSign;
		let px = px0 + hs * halfW * rx + vs * halfH * upx;
		let py = py0 + hs * halfW * ry + vs * halfH * upy;
		let pz = pz0 + hs * halfW * rz + vs * halfH * upz;
		if (v === 2 || v === 7) {
			const along = v === 2 ? -1 : 1;
			if (angle === 0) {
				px += along * lx * halfW;
				py += along * ly * halfW;
				pz += along * lz * halfW;
			} else {
				const s = along * halfW * Math.sin(Math.abs(angle) * .5);
				const c = Math.sign(angle) * halfW * Math.cos(Math.abs(angle) * .5);
				px += s * lx + c * rx;
				py += s * ly + c * ry;
				pz += s * lz + c * rz;
			}
		}
		const dx = px - cam.ex, dy = py - cam.ey, dz = pz - cam.ez;
		const ex = dx * cam.sx + dy * cam.sy + dz * cam.sz;
		const ey = dx * cam.ux + dy * cam.uy + dz * cam.uz;
		const ez = -(dx * cam.fx + dy * cam.fy + dz * cam.fz);
		let nx = px - px0, ny = py - py0, nz = pz - pz0;
		const nl = Math.sqrt(nx * nx + ny * ny + nz * nz);
		if (nl > 0) {
			nx /= nl;
			ny /= nl;
			nz /= nl;
		} else {
			nx = lx;
			ny = ly;
			nz = lz;
		}
		const light = lighting(ex, ey, ez, nx * cam.sx + ny * cam.sy + nz * cam.sz, nx * cam.ux + ny * cam.uy + nz * cam.uz, -(nx * cam.fx + ny * cam.fy + nz * cam.fz));
		out.r[v] = rgb[id * 3] * light;
		out.g[v] = rgb[id * 3 + 1] * light;
		out.b[v] = rgb[id * 3 + 2] * light;
		const w = -ez;
		if (!(w > near)) out.ok = false;
		out.x[v] = (focal * ex / w * .5 + .5) * pixels;
		out.y[v] = (.5 - focal * ey / w * .5) * pixels;
		out.z[v] = (za * ez + zb) / ez;
	}
}
/** Depth and colour samples of one render target (side = size x supersample). */
var RenderTarget = class {
	side;
	depth;
	rgb;
	size;
	supersample;
	constructor(size, supersample) {
		this.size = size;
		this.supersample = supersample;
		this.side = size * supersample;
		this.depth = new Float32Array(this.side * this.side);
		this.rgb = new Float32Array(this.side * this.side * 3);
	}
	clear() {
		this.depth.fill(Infinity);
		this.rgb.fill(0);
	}
};
/** One Gouraud triangle of `s` into `t` with the depth test (nearer ndc z wins); exported for tests. */
function rasterTriangle(t, s, a, b, c) {
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
	const w0dx = (y1 - y2) * inv, w0dy = (x2 - x1) * inv, w0k = (x1 * y2 - x2 * y1) * inv;
	const w1dx = (y2 - y0) * inv, w1dy = (x0 - x2) * inv, w1k = (x2 * y0 - x0 * y2) * inv;
	const z0 = s.z[a], z1 = s.z[b], z2 = s.z[c];
	const depth = t.depth, rgb = t.rgb;
	for (let py = minY; py <= maxY; py++) {
		const sy = py + .5;
		let w0 = w0k + w0dx * (minX + .5) + w0dy * sy;
		let w1 = w1k + w1dx * (minX + .5) + w1dy * sy;
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
function drawSegments(data, rgb, cam, target) {
	const scratch = new SegmentVertices();
	const seg = data.segIndex;
	for (let i = 0; i < data.nSeg; i++) {
		segmentVertices(data, seg[i * 4], rgb, cam, target.side, scratch);
		if (!scratch.ok) continue;
		for (let k = 0; k < TRIANGLES.length; k += 3) rasterTriangle(target, scratch, TRIANGLES[k], TRIANGLES[k + 1], TRIANGLES[k + 2]);
	}
}
/** Resolves the supersample to straight-alpha RGBA8; colour is clamped per sample, as the framebuffer does. */
function resolve(target) {
	const { size, supersample: ss, side, depth, rgb } = target;
	const out = new Uint8Array(size * size * 4);
	const full = ss * ss;
	for (let y = 0; y < size; y++) for (let x = 0; x < size; x++) {
		let r = 0, g = 0, b = 0, n = 0;
		for (let j = 0; j < ss; j++) {
			let o = (y * ss + j) * side + x * ss;
			for (let i = 0; i < ss; i++, o++) {
				if (depth[o] === Infinity) continue;
				r += Math.min(rgb[o * 3], 1);
				g += Math.min(rgb[o * 3 + 1], 1);
				b += Math.min(rgb[o * 3 + 2], 1);
				n++;
			}
		}
		if (n === 0) continue;
		const q = (y * size + x) * 4;
		out[q] = Math.round(r / n * 255);
		out[q + 1] = Math.round(g / n * 255);
		out[q + 2] = Math.round(b / n * 255);
		out[q + 3] = Math.round(n / full * 255);
	}
	return out;
}
//#endregion
//#region src/part-render/png.ts
/**
* Minimal PNG encoder for the part-render script (spec §5.2): 8-bit RGBA,
* filter type 0 on every row, one zlib stream from node:zlib. Deterministic:
* the same pixels give the same bytes.
*/
const CRC_TABLE = (() => {
	const table = /* @__PURE__ */ new Uint32Array(256);
	for (let n = 0; n < 256; n++) {
		let c = n;
		for (let k = 0; k < 8; k++) c = c & 1 ? 3988292384 ^ c >>> 1 : c >>> 1;
		table[n] = c >>> 0;
	}
	return table;
})();
function crc32(bytes) {
	let c = 4294967295;
	for (let i = 0; i < bytes.length; i++) c = CRC_TABLE[(c ^ bytes[i]) & 255] ^ c >>> 8;
	return (c ^ 4294967295) >>> 0;
}
function chunk(type, data) {
	const out = new Uint8Array(12 + data.length);
	const view = new DataView(out.buffer);
	view.setUint32(0, data.length);
	for (let i = 0; i < 4; i++) out[4 + i] = type.charCodeAt(i);
	out.set(data, 8);
	view.setUint32(8 + data.length, crc32(out.subarray(4, 8 + data.length)));
	return out;
}
const SIGNATURE = Uint8Array.of(137, 80, 78, 71, 13, 10, 26, 10);
function encodePng(rgba, width, height) {
	if (rgba.length !== width * height * 4) throw new RangeError(`expected ${width * height * 4} bytes, got ${rgba.length}`);
	const header = /* @__PURE__ */ new Uint8Array(13);
	const hv = new DataView(header.buffer);
	hv.setUint32(0, width);
	hv.setUint32(4, height);
	header[8] = 8;
	header[9] = 6;
	const stride = width * 4;
	const raw = new Uint8Array(height * (stride + 1));
	for (let y = 0; y < height; y++) raw.set(rgba.subarray(y * stride, (y + 1) * stride), y * (stride + 1) + 1);
	const idat = new Uint8Array(deflateSync(raw, { level: 9 }));
	const parts = [
		SIGNATURE,
		chunk("IHDR", header),
		chunk("IDAT", idat),
		chunk("IEND", /* @__PURE__ */ new Uint8Array(0))
	];
	const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
	let at = 0;
	for (const p of parts) {
		out.set(p, at);
		at += p.length;
	}
	return out;
}
//#endregion
//#region src/part-render/select.ts
/** Spec §5.2: travels and every service toolpath, the whole brim family included. */
const HIDDEN_TYPES = /* @__PURE__ */ new Set([
	ToolpathType.travel,
	ToolpathType.skirt,
	ToolpathType.support,
	ToolpathType.raft,
	ToolpathType.primeTower
]);
const MODEL_FLAGS = 3;
/**
* The records one picture is made of. `object` keeps what the slicer marked
* with that id; `model` -- only for a plate with one object and no markers --
* keeps unowned records that carry an explicit feature and come after the
* first layer marker, which drops start G-code and the purge line (spec §5.3).
* The record type is re-keyed to `tool + 1`: vertices of different filaments
* then never merge, and the colour pass reads the filament back from it.
*/
function selectSegments(parsed, target) {
	const min = [
		Infinity,
		Infinity,
		Infinity
	];
	const max = [
		-Infinity,
		-Infinity,
		-Infinity
	];
	const tools = {};
	let segments = 0;
	return {
		layers: parsed.layers.map((layer) => {
			const paths = [];
			const widths = [];
			for (let i = 0; i < layer.paths.length; i += 8) {
				const k = i / 8;
				const type = layer.paths[i + 3];
				if (HIDDEN_TYPES.has(type)) continue;
				const owner = layer.objectIds?.[k] ?? -1;
				const flags = layer.flags?.[k] ?? 0;
				if (!(target.kind === "object" ? owner === target.id : owner === -1 && (flags & MODEL_FLAGS) === MODEL_FLAGS)) continue;
				const tool = layer.paths[i + 7];
				tools[tool] = (tools[tool] ?? 0) + 1;
				segments += 1;
				for (let j = 0; j < 8; j += 1) paths.push(layer.paths[i + j]);
				paths[paths.length - 5] = tool + 1;
				widths.push(layer.widths[k] ?? 0);
				for (const at of [i, i + 4]) for (let axis = 0; axis < 3; axis += 1) {
					const v = layer.paths[at + axis];
					if (v < min[axis]) min[axis] = v;
					if (v > max[axis]) max[axis] = v;
				}
			}
			return {
				z: layer.z,
				paths: new Float32Array(paths),
				widths
			};
		}),
		segments,
		tools,
		bounds: segments > 0 ? {
			min,
			max
		} : null
	};
}
const PALETTE_LINE = /^; filament_colour = (.+)$/m;
/** One palette slot: #RRGGBB, optionally #RRGGBBAA. Not global -- exec() keeps no state between slots. */
const SLOT_COLOUR = /^#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?/;
/** The header sits at the top; a palette line deep in the body would be someone's comment. */
const PALETTE_SCAN_BYTES = 2e5;
/** `filament_colour` from the G-code header, indexed by the original T number (gaps kept). */
function paletteFromGcode(gcode) {
	const match = PALETTE_LINE.exec(gcode.slice(0, PALETTE_SCAN_BYTES));
	if (!match) return [];
	return match[1].split(";").map((entry) => {
		const colour = SLOT_COLOUR.exec(entry.trim());
		return colour ? colour[0].slice(0, 7).toUpperCase() : "";
	});
}
/** Whether selected bounds lie inside an object box from plate_N.json, in mm, with tolerance. */
function fitsBox(bounds, box, tolMm = 2) {
	const [x0, y0, x1, y1] = box;
	return bounds.min[0] >= x0 - tolMm && bounds.min[1] >= y0 - tolMm && bounds.max[0] <= x1 + tolMm && bounds.max[1] <= y1 + tolMm;
}
function parseHex(hex) {
	const n = Number.parseInt(hex.slice(1, 7), 16);
	return [
		(n >> 16 & 255) / 255,
		(n >> 8 & 255) / 255,
		(n & 255) / 255
	];
}
/**
* Calls `emit(id, png)` for every rendered object, in job order, and returns the manifest.
* `draw` is the rasterizer; tests pass another one to reach the empty-render branch.
*/
function runJob(job, gcode, emit, draw = drawSegments) {
	let parsed, palette;
	try {
		parsed = parseGcodeToolpath(gcode);
		palette = paletteFromGcode(gcode);
	} catch (error) {
		throw new RenderError(isAllocationFailure(error) ? "memory_limit" : "parse_failed", String(error));
	}
	const ss = job.supersample ?? 2;
	const target = new RenderTarget(job.size, ss);
	const objects = [];
	let spent = 0;
	for (const want of job.objects) {
		const selection = selectSegments(parsed, want.mode === "model" ? { kind: "model" } : {
			kind: "object",
			id: want.id
		});
		if (selection.segments === 0 || selection.bounds === null) {
			objects.push({
				id: want.id,
				method: "missing",
				reason: "empty_selection"
			});
			continue;
		}
		if (want.mode === "model" && !(want.bbox && fitsBox(selection.bounds, want.bbox))) {
			objects.push({
				id: want.id,
				method: "missing",
				reason: "model_unproven"
			});
			continue;
		}
		const tools = Object.keys(selection.tools).map(Number).sort((a, b) => a - b);
		const uncoloured = tools.filter((t) => !palette[t]);
		if (uncoloured.length > 0) throw new RenderError("parse_failed", `no filament colour for T${uncoloured.join(", T")}`);
		const data = ue(selection.layers, parsed.defaultWidth);
		if (data.hasNaN) throw new RenderError("parse_failed", "segment geometry contains NaN");
		const rgb = new Float32Array(data.nV * 3);
		for (let v = 0; v < data.nV; v++) {
			const [r, g, b] = parseHex(palette[data.meta.vType[v] - 1]);
			rgb[v * 3] = r;
			rgb[v * 3 + 1] = g;
			rgb[v * 3 + 2] = b;
		}
		const drawn = geometryBounds(data);
		if (drawn === null) {
			objects.push({
				id: want.id,
				method: "missing",
				reason: "empty_selection"
			});
			continue;
		}
		target.clear();
		draw(data, rgb, frameCamera(drawn), target);
		const rgba = resolve(target);
		if (isEmpty(rgba)) {
			objects.push({
				id: want.id,
				method: "missing",
				reason: "empty_render"
			});
			continue;
		}
		const png = encodePng(rgba, job.size, job.size);
		spent += png.length + 9;
		if (spent + 1048581 > job.outputBytes) throw new RenderError("invalid_output", `PNGs exceed the attempt budget of ${job.outputBytes} bytes`);
		emit(want.id, png);
		objects.push({
			id: want.id,
			method: want.mode,
			width: job.size,
			height: job.size,
			tools,
			bounds: selection.bounds,
			segments: selection.segments,
			sha256: createHash("sha256").update(png).digest("hex"),
			bytes: png.length
		});
	}
	return {
		renderer: 2,
		palette,
		objects
	};
}
//#endregion
//#region src/part-render/io.ts
/**
* stdin/stdout protocol of the part-render script (spec §5.6). stdin: one JSON line (the
* RenderJob), then the plate's G-code. stdout: frames `kind(1) | length(u32 BE) | payload`:
*   'P' -- id (u32 BE) + PNG bytes, one per rendered object, in job order
*   'M' -- the manifest JSON, last
*   'E' -- {reason, message} JSON instead of 'M' when the job fails
* Exit code 0 with 'M', 1 with 'E'. No file system, network or child process.
*/
function frame(kind, payload) {
	const out = new Uint8Array(5 + payload.length);
	out[0] = kind.charCodeAt(0);
	new DataView(out.buffer).setUint32(1, payload.length);
	out.set(payload, 5);
	return out;
}
const encoder = new TextEncoder();
async function readAll(stream) {
	const chunks = [];
	for await (const chunk of stream) chunks.push(chunk);
	return Buffer.concat(chunks);
}
function write(stream, bytes) {
	return new Promise((done, fail) => stream.write(bytes, (error) => error ? fail(error) : done()));
}
/**
* One byte per character (ISO-8859-1). The renderer reads only ASCII -- moves, markers, hex colours --
* while a UTF-8 decode turns the WHOLE plate into a two-byte V8 string as soon as one name is Cyrillic
* or one byte is cp1251, doubling its memory. Buffer's latin1, not TextDecoder('latin1'): the WHATWG
* label means windows-1252, which maps 0x80-0x9F above 0xFF and is two-byte again.
*/
function decodeGcode(bytes) {
	return Buffer.from(bytes.buffer, bytes.byteOffset, bytes.byteLength).toString("latin1");
}
function reasonOf(error) {
	if (error instanceof RenderError) return error.reason;
	if (isAllocationFailure(error)) return "memory_limit";
	return "crashed";
}
async function main(stdin, stdout) {
	const pending = [];
	try {
		const input = await readAll(stdin);
		const newline = input.indexOf(10);
		if (newline < 0) throw new RenderError("invalid_output", "no job line on stdin");
		let job;
		try {
			job = JSON.parse(new TextDecoder().decode(input.subarray(0, newline)));
		} catch (error) {
			throw new RenderError("invalid_output", `unreadable job line: ${String(error)}`);
		}
		const gcode = decodeGcode(input.subarray(newline + 1));
		const manifest = runJob(job, gcode, (id, png) => {
			const payload = new Uint8Array(4 + png.length);
			new DataView(payload.buffer).setUint32(0, id);
			payload.set(png, 4);
			pending.push(write(stdout, frame("P", payload)));
		});
		await Promise.all(pending);
		const body = encoder.encode(JSON.stringify(manifest));
		if (body.length > 1048576) throw new RenderError("invalid_output", `manifest of ${body.length} bytes exceeds ${MANIFEST_MAX}`);
		await write(stdout, frame("M", body));
		return 0;
	} catch (error) {
		await Promise.allSettled(pending);
		const reason = reasonOf(error);
		await write(stdout, frame("E", encoder.encode(JSON.stringify({
			reason,
			message: String(error).slice(0, 500)
		}))));
		return 1;
	}
}
//#endregion
//#region src/part-render/entry.ts
/** Node entry of the part-render bundle (spec §5.5-5.6): the bundle is the entry, so it always runs. */
main(process.stdin, process.stdout).then((code) => {
	process.exitCode = code;
});
//#endregion
export {};
