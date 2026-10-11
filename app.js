'use strict';

// KNMI's keyless WMS. Its GetFeatureInfo is exact for observed rain but returns
// wrong values for forecast time series, so forecast amounts are read back from
// the rendered frames: the smooth style is a continuous log colour ramp
// (0.1–40 mm/h) that inverts cleanly.
const WMS = 'https://anonymous.api.dataplatform.knmi.nl/wms/adaguc-server';
// Frames prepared every 5 minutes by job/build.py (GitHub Actions) and deployed
// with the site under data/, one small greyscale PNG each. Used whenever fresh;
// KNMI's WMS is the fallback. (Not raw.githubusercontent.com: its CDN serves a
// stale index for minutes after each update.)
// Local development (localhost) reads the live site's data.
const DATA = /^(localhost|127\.0\.0\.1)$/.test(location.hostname)
  ? 'https://mf-rug.github.io/rAIn/data/'
  : new URL('data/', location.href).href;
const DATA_STALE = 20 * 60e3;
const STYLE = 'precip-smooth-white-to-purple/bilinear';
const OBS = { dataset: 'radar_reflectivity_composites', layer: 'precipitation' };
const FC = { dataset: 'radar_forecast', layer: 'precipitationfc' };

// Full radar domain in Web Mercator, as advertised by the WMS. A pixel is 1.73
// Mercator km, which at Dutch latitudes is ~1.06 km on the ground: about the
// native 1 km radar grid. Must match job/build.py.
const X0 = 0, Y0 = 6257115.396264, X1 = 1208534.924976, Y1 = 7553162.131589;
const W = 700, H = 751;

const STEP = 5 * 60e3;
const PAST = 12;           // observed frames before "now" (1 hour)
const AHEAD = 24;          // forecast frames (2 hours)
const WET = 0.15;          // mm/h; below this counts as dry
const CONCURRENCY = 4;
// New frames appear ~4–5 minutes after their nominal time (KNMI, or the job), so
// nothing new can exist before (latest frame + 5 min + lag). Until then reloads
// skip the check.
const PUBLISH_LAG = 5 * 60e3;
const POLL_EVERY = 30e3;   // once new data is due, check this often until it lands
const PLAY_MS = 450;       // playback time per 5-minute step
const FALLBACK = { lat: 52.101, lng: 5.177, name: 'De Bilt' };

// Rain palette, in log space: drizzle blue → Delft blue → violet → magenta.
const STOPS = [
  [0.1, [142, 197, 222], 110],
  [0.5, [95, 168, 211], 165],
  [1, [46, 111, 191], 195],
  [2, [27, 63, 139], 215],
  [5, [91, 42, 134], 228],
  [10, [164, 35, 122], 238],
  [30, [224, 17, 95], 248],
];

const $ = (id) => document.getElementById(id);

// ?demo: fake showery rain at the point and fake reliability (good for ~50
// minutes, then dropping off), to preview how the forecast display looks.
const DEMO = new URLSearchParams(location.search).has('demo') ? {
  skill: (() => {
    const fss = Array.from({ length: 24 }, (_, k) => (k < 6 ? 97 - 3 * k : Math.max(8, 80 - (k - 5) * 5)));
    return { hours: 6, scored: 72, cell: 4, tile: 10, nx: 18, ny: 19, nl: fss, tiles: Array(18 * 19).fill(fss) };
  })(),
  rain(m) { // minutes from now → mm/h
    const bump = (c, w, h) => h * Math.exp(-(((m - c) / w) ** 2));
    // Showery: short bursts of varying strength every 15–25 minutes.
    return [[-50, 0.8], [-28, 1.6], [-8, 0.6], [12, 1.4], [30, 2.8], [47, 0.9], [66, 3.2], [84, 1.3], [103, 2.2], [120, 0.9]]
      .reduce((sum, [c, h]) => sum + bump(c, 4.5, h), 0);
  },
} : null;
const state = {
  mapSource: 'knmi',     // radar shown on the map: 'knmi' or 'dwd'
  coverage: { knmi: null, dwd: null },
  compare: null,        // KNMI vs Buienradar scores, from job/compare.py
  skill: null,          // recent forecast reliability, from job/verify.py
  src: null,            // 'data' (job output) or 'wms'
  point: null,          // {lat, lng, label}
  frames: [],           // ordered by time
  observed: new Map(),  // time → mm/h at the point, from GetFeatureInfo
  nowIdx: PAST,
  pos: PAST,            // continuous position on the timeline, in frames
  t0: null, ref: null,
  nextCheck: 0,
  refreshing: false,
  playing: false,
  userScrubbed: false,
  lut: null,
};

// ---------- rain encoding ----------
// Frames are stored as one byte per pixel: 0 = dry, 1..255 = 0.1..40 mm/h on a
// log scale. Interpolating in this space is interpolating in log intensity.

const LOG_SPAN = Math.log10(400);
const toByte = (v) => (v < 0.1 ? 0 : Math.min(255, 1 + Math.round((Math.log10(v) + 1) / LOG_SPAN * 254)));
const fromByte = (b) => (b === 0 ? 0 : 10 ** (-1 + (b - 1) / 254 * LOG_SPAN));

function rampColor(v) {
  const lv = Math.log10(v);
  let k = 0;
  while (k < STOPS.length - 2 && lv > Math.log10(STOPS[k + 1][0])) k++;
  const [va, ca, aa] = STOPS[k], [vb, cb, ab] = STOPS[k + 1];
  const f = Math.min(1, Math.max(0, (lv - Math.log10(va)) / (Math.log10(vb) - Math.log10(va))));
  return [...ca.map((c, i) => c + (cb[i] - c) * f), aa + (ab - aa) * f];
}

// 256-entry palette indexed by the byte encoding; the lowest bytes fade in so
// rain edges stay soft when frames are blended.
const PALETTE = (() => {
  const out = new Uint8Array(256 * 4);
  for (let b = 1; b < 256; b++) {
    const [r, g, bl, a] = rampColor(fromByte(b));
    out.set([r, g, bl, a * Math.min(1, b / 10)], b * 4);
  }
  return out;
})();

function cssColor(v) {
  const [r, g, b] = rampColor(Math.max(v, 0.1));
  return `rgb(${r | 0} ${g | 0} ${b | 0})`;
}

// ---------- KNMI requests ----------

class RateLimited extends Error {}

async function get(url, opts) {
  const res = await fetch(url, opts);
  if (res.status === 429) throw new RateLimited();
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res;
}

const iso = (t) => new Date(t).toISOString().replace('.000', '');

async function latestTimes(src) {
  const xml = await (await get(`${WMS}?DATASET=${src.dataset}&SERVICE=WMS&REQUEST=GetCapabilities`)).text();
  const dim = (name) => {
    const m = xml.match(new RegExp(`<Dimension name="${name}"[^>]*default="([^"]+)"`));
    return m ? Date.parse(m[1]) : null;
  };
  return { time: dim('time'), ref: dim('reference_time') };
}

// Observed rain at the point for a time range, in one request.
async function observedSeries(pt, from, to) {
  const d = 0.005;
  const url = `${WMS}?DATASET=${OBS.dataset}&SERVICE=WMS&VERSION=1.3.0&REQUEST=GetFeatureInfo` +
    `&LAYERS=${OBS.layer}&QUERY_LAYERS=${OBS.layer}&CRS=EPSG:4326` +
    `&BBOX=${pt.lat - d},${pt.lng - d},${pt.lat + d},${pt.lng + d}&WIDTH=3&HEIGHT=3&I=1&J=1` +
    `&INFO_FORMAT=application/json&TIME=${iso(from)}/${iso(to)}`;
  const [layer] = await (await get(url)).json();
  let data = layer.data;
  const first = Object.values(data)[0];
  if (first && typeof first === 'object') data = first;
  const out = new Map();
  for (const [t, v] of Object.entries(data)) {
    const n = parseFloat(v);
    if (Number.isFinite(n)) out.set(Date.parse(t), n);
  }
  return out;
}

// ---------- persistent cache ----------
// A frame URL names a fixed time (and forecast run), so its image never
// changes: keep it in the Cache API until it drops off the timeline. Browsers'
// HTTP caches (iOS especially) evict far more eagerly than this.

const CACHE = 'rain-frames-v1';
const openCache = () => ('caches' in self ? caches.open(CACHE).catch(() => null) : Promise.resolve(null));

async function cachedImage(url, opts) {
  const cache = await openCache();
  const hit = cache && await cache.match(url).catch(() => null);
  if (hit) return createImageBitmap(await hit.blob(), opts);
  const res = await get(url);
  if (cache) cache.put(url, res.clone()).catch(() => {});
  return createImageBitmap(await res.blob(), opts);
}

async function pruneCache(keep) {
  const cache = await openCache();
  if (!cache) return;
  for (const req of await cache.keys()) {
    if (!keep.has(req.url) && !req.url.includes('GetLegendGraphic')) cache.delete(req);
  }
}

const store = {
  get(k) { try { return JSON.parse(localStorage.getItem(`rain.${k}`)); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(`rain.${k}`, JSON.stringify(v)); } catch { /* private mode */ } },
};
const dueAfter = (t0, ref) => Math.max(t0, ref ?? t0) + STEP + PUBLISH_LAG;

// Legend bar: 0.1 mm/h at y=363, 40 mm/h at y=20, logarithmic.
async function loadLegend() {
  const img = await cachedImage(`${WMS}?DATASET=${FC.dataset}&SERVICE=WMS&VERSION=1.3.0&REQUEST=GetLegendGraphic&LAYER=${FC.layer}&STYLE=${STYLE}&FORMAT=image/png`);
  const ctx = new OffscreenCanvas(img.width, img.height).getContext('2d');
  ctx.drawImage(img, 0, 0);
  const col = ctx.getImageData(10, 0, 1, img.height).data;
  const entries = [];
  for (let y = 20; y <= 363; y++) {
    const v = 10 ** (Math.log10(40) - (y - 20) * LOG_SPAN / 343);
    entries.push([col[y * 4], col[y * 4 + 1], col[y * 4 + 2], toByte(v)]);
  }
  return { entries, cache: new Map() };
}

function invert(lut, r, g, b) {
  const key = (r << 16) | (g << 8) | b;
  let v = lut.cache.get(key);
  if (v === undefined) {
    let best = Infinity;
    for (const [lr, lg, lb, lv] of lut.entries) {
      const d = (lr - r) ** 2 + (lg - g) ** 2 + (lb - b) ** 2;
      if (d < best) { best = d; v = lv; }
    }
    lut.cache.set(key, v);
  }
  return v;
}

function wmsUrl(kind, t, ref) {
  const src = kind === 'obs' ? OBS : FC;
  let u = `${WMS}?DATASET=${src.dataset}&SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap&LAYERS=${src.layer}&STYLES=${STYLE}` +
    `&CRS=EPSG:3857&BBOX=${X0},${Y0},${X1},${Y1}&WIDTH=${W}&HEIGHT=${H}&FORMAT=image/png&TRANSPARENT=true&TIME=${iso(t)}`;
  if (kind === 'fc') u += `&DIM_REFERENCE_TIME=${iso(ref)}`;
  return u;
}

// Fetch one frame and decode it to the byte encoding. The job's PNGs already
// hold the bytes; WMS frames are read back through the legend colours.
async function loadFrame(f) {
  const data = f.src === 'data';
  const img = await cachedImage(f.url, data ? { colorSpaceConversion: 'none', premultiplyAlpha: 'none' } : undefined);
  const ctx = new OffscreenCanvas(W, H).getContext('2d', { willReadFrequently: true });
  ctx.drawImage(img, 0, 0, W, H);
  const d = ctx.getImageData(0, 0, W, H).data;
  const bytes = new Uint8Array(W * H);
  for (let i = 0, p = 0; i < bytes.length; i++, p += 4) {
    if (data) bytes[i] = d[p];
    else if (d[p + 3] >= 64) bytes[i] = invert(state.lut, d[p], d[p + 1], d[p + 2]);
  }
  f.bytes = bytes;
  f.tex = gpu.frameTexture(bytes);
  computeFlows();
  requestDraw();
}

function pixelOf(pt) {
  const x = pt.lng * 20037508.34 / 180;
  const y = Math.log(Math.tan((90 + pt.lat) * Math.PI / 360)) * 6378137;
  return [Math.floor((x - X0) / (X1 - X0) * W), Math.floor((Y1 - y) / (Y1 - Y0) * H)];
}

// Rain at a point: mean of the 3×3 pixels around it (~3 km box).
function sample(f, pt) {
  if (!f.bytes || !pt) return null;
  const [cx, cy] = pixelOf(pt);
  let sum = 0, n = 0;
  for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) {
    const px = cx + dx, py = cy + dy;
    if (px < 0 || py < 0 || px >= W || py >= H) continue;
    sum += fromByte(f.bytes[py * W + px]); n++;
  }
  return n ? sum / n : null;
}

// ---------- motion between frames ----------
// Block matching on a quarter-resolution copy: for a grid of regions, find the
// shift that best maps frame A onto frame B, then smooth the vectors into a
// field. The shader warps both frames along it to draw in-between moments.

const QW = W >> 2, QH = H >> 2;
const GX = 14, GY = 15;    // flow grid
const BLOCK = 12;          // half block size, quarter pixels (~50 km on the ground)
const SEARCH = 3;          // max shift per 5 min, quarter pixels (~13 km, i.e. ~150 km/h)

function quarter(bytes) {
  const q = new Uint8Array(QW * QH);
  for (let y = 0; y < QH; y++) for (let x = 0; x < QW; x++) {
    let s = 0;
    for (let dy = 0; dy < 4; dy++) {
      const row = (y * 4 + dy) * W + x * 4;
      s += bytes[row] + bytes[row + 1] + bytes[row + 2] + bytes[row + 3];
    }
    q[y * QW + x] = s >> 4;
  }
  return q;
}

function estimateFlow(qa, qb) {
  const n = 2 * SEARCH + 1;
  const vx = new Float32Array(GX * GY), vy = new Float32Array(GX * GY);
  const known = new Uint8Array(GX * GY);
  const sad = new Float64Array(n * n);
  for (let gy = 0; gy < GY; gy++) for (let gx = 0; gx < GX; gx++) {
    const cx = Math.round((gx + 0.5) / GX * QW), cy = Math.round((gy + 0.5) / GY * QH);
    const x0 = Math.max(SEARCH, cx - BLOCK), x1 = Math.min(QW - SEARCH, cx + BLOCK);
    const y0 = Math.max(SEARCH, cy - BLOCK), y1 = Math.min(QH - SEARCH, cy + BLOCK);
    let wet = 0;
    for (let y = y0; y < y1; y++) for (let x = x0; x < x1; x++) if (qa[y * QW + x] > 8) wet++;
    if (wet < 40) continue;
    for (let dy = -SEARCH; dy <= SEARCH; dy++) for (let dx = -SEARCH; dx <= SEARCH; dx++) {
      let s = 0;
      for (let y = y0; y < y1; y++) {
        const ra = y * QW, rb = (y + dy) * QW + dx;
        for (let x = x0; x < x1; x++) s += Math.abs(qa[ra + x] - qb[rb + x]);
      }
      sad[(dy + SEARCH) * n + dx + SEARCH] = s;
    }
    let bi = 0;
    for (let i = 1; i < sad.length; i++) if (sad[i] < sad[bi]) bi = i;
    const bx = bi % n, by = (bi / n) | 0;
    // Sub-pixel refinement: fit a parabola through the minimum and its neighbours.
    const refine = (m, c, p) => { const den = m - 2 * c + p; return den > 0 ? (m - p) / (2 * den) : 0; };
    let fx = bx - SEARCH, fy = by - SEARCH;
    if (bx > 0 && bx < n - 1) fx += refine(sad[by * n + bx - 1], sad[bi], sad[by * n + bx + 1]);
    if (by > 0 && by < n - 1) fy += refine(sad[(by - 1) * n + bx], sad[bi], sad[(by + 1) * n + bx]);
    const k = gy * GX + gx;
    vx[k] = fx; vy[k] = fy; known[k] = 1;
  }

  // Regions without rain take the median motion, then the field is smoothed.
  const med = (a) => { const s = a.filter((_, i) => known[i]).sort((p, q) => p - q); return s.length ? s[s.length >> 1] : 0; };
  const mx = med(vx), my = med(vy);
  for (let k = 0; k < GX * GY; k++) if (!known[k]) { vx[k] = mx; vy[k] = my; }
  for (let pass = 0; pass < 2; pass++) {
    const sx = vx.slice(), sy = vy.slice();
    for (let gy = 0; gy < GY; gy++) for (let gx = 0; gx < GX; gx++) {
      let ax = 0, ay = 0, c = 0;
      for (let j = -1; j <= 1; j++) for (let i = -1; i <= 1; i++) {
        const x = gx + i, y = gy + j;
        if (x < 0 || y < 0 || x >= GX || y >= GY) continue;
        ax += sx[y * GX + x]; ay += sy[y * GX + x]; c++;
      }
      vx[gy * GX + gx] = ax / c; vy[gy * GX + gx] = ay / c;
    }
  }

  // Pack as bytes: 128 = no motion, 16 steps per quarter pixel.
  const out = new Uint8Array(GX * GY * 4);
  for (let k = 0; k < GX * GY; k++) {
    out[k * 4] = Math.max(0, Math.min(255, Math.round(128 + vx[k] * 16)));
    out[k * 4 + 1] = Math.max(0, Math.min(255, Math.round(128 + vy[k] * 16)));
    out[k * 4 + 3] = 255;
  }
  return out;
}

function computeFlows(fr = state.frames) {
  for (let i = 0; i < fr.length - 1; i++) {
    const a = fr[i], b = fr[i + 1];
    if (a.flowTex || !a.bytes || !b.bytes) continue;
    a.q ??= quarter(a.bytes);
    b.q ??= quarter(b.bytes);
    a.flowTex = gpu.flowTexture(estimateFlow(a.q, b.q));
  }
}

// ---------- WebGL renderer ----------

const gpu = (() => {
  const canvas = document.createElement('canvas');
  canvas.width = W; canvas.height = H;
  const gl = canvas.getContext('webgl', { premultipliedAlpha: true, alpha: true, antialias: false });
  if (!gl) return { canvas, ok: false, frameTexture() {}, flowTexture() {}, draw() {}, drop() {} };

  const compile = (type, src) => {
    const s = gl.createShader(type);
    gl.shaderSource(s, src); gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
    return s;
  };
  const prog = gl.createProgram();
  gl.attachShader(prog, compile(gl.VERTEX_SHADER, `
    attribute vec2 p;
    varying vec2 uv;
    void main() { uv = vec2(p.x * 0.5 + 0.5, 0.5 - p.y * 0.5); gl_Position = vec4(p, 0.0, 1.0); }`));
  gl.attachShader(prog, compile(gl.FRAGMENT_SHADER, `
    precision highp float;
    varying vec2 uv;
    uniform sampler2D a, b, flow, pal;
    uniform float t;
    uniform vec2 scale, px;
    // Small 4-tap blur, so real frames are as soft as blended in-between ones
    // and playback doesn't pulse in sharpness every 5 minutes.
    float soft(sampler2D s, vec2 p) {
      return 0.25 * (texture2D(s, p + vec2(px.x, px.y)).r + texture2D(s, p + vec2(-px.x, px.y)).r
                   + texture2D(s, p + vec2(px.x, -px.y)).r + texture2D(s, p - px).r);
    }
    void main() {
      // Rain at uv came from uv - f·t in frame A and is headed for uv + f·(1-t) in B.
      vec2 f = (texture2D(flow, uv).rg - 128.0 / 255.0) * scale;
      float va = soft(a, uv - f * t);
      float vb = soft(b, uv + f * (1.0 - t));
      float v = mix(va, vb, t);
      vec4 c = texture2D(pal, vec2(v * 255.0 / 256.0 + 0.5 / 256.0, 0.5));
      gl_FragColor = vec4(c.rgb * c.a, c.a);
    }`));
  gl.linkProgram(prog);
  gl.useProgram(prog);

  gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
  const loc = gl.getAttribLocation(prog, 'p');
  gl.enableVertexAttribArray(loc);
  gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
  gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);

  const texture = (w, h, format, data, filter = gl.LINEAR) => {
    const tex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, filter);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, filter);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.texImage2D(gl.TEXTURE_2D, 0, format, w, h, 0, format, gl.UNSIGNED_BYTE, data);
    return tex;
  };

  const palTex = texture(256, 1, gl.RGBA, PALETTE);
  const still = texture(1, 1, gl.RGBA, new Uint8Array([128, 128, 0, 255]));
  const u = (n) => gl.getUniformLocation(prog, n);
  gl.uniform1i(u('a'), 0); gl.uniform1i(u('b'), 1); gl.uniform1i(u('flow'), 2); gl.uniform1i(u('pal'), 3);
  // Flow byte → uv offset: (byte-128)/16 quarter pixels × 4 → pixels → uv.
  gl.uniform2f(u('scale'), 255 * 4 / 16 / W, 255 * 4 / 16 / H);
  gl.uniform2f(u('px'), 0.75 / W, 0.75 / H);
  const uT = u('t');
  gl.viewport(0, 0, W, H);

  return {
    canvas, ok: true,
    frameTexture: (bytes) => texture(W, H, gl.LUMINANCE, bytes),
    flowTexture: (bytes) => texture(GX, GY, gl.RGBA, bytes),
    drop: (tex) => tex && gl.deleteTexture(tex),
    draw(a, b, flowTex, t) {
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      if (!a) return;
      const bind = (unit, tex) => { gl.activeTexture(gl.TEXTURE0 + unit); gl.bindTexture(gl.TEXTURE_2D, tex); };
      bind(0, a); bind(1, b || a); bind(2, flowTex || still); bind(3, palTex);
      gl.uniform1f(uT, b ? t : 0);
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    },
  };
})();

// Draw the current timeline position, warping between the two nearest frames.
function drawRadar() {
  // DWD when chosen and loaded; otherwise KNMI rather than an empty map.
  const fr = state.mapSource === 'dwd' && dwd.frames.length ? dwd.frames : state.frames;
  if (!fr.length) return gpu.draw(null);
  const i = Math.min(Math.floor(state.pos), fr.length - 1);
  const t = state.pos - i;
  const a = fr[i], b = fr[i + 1];
  if (a?.tex && b?.tex && t > 0.001) gpu.draw(a.tex, b.tex, a.flowTex, t);
  else if (a?.tex && t <= 0.5) gpu.draw(a.tex);
  else if (b?.tex) gpu.draw(b.tex);
  else gpu.draw(a?.tex ?? null);
  // Further-ahead forecasts fade with today's reliability.
  const trust = state.mapSource === 'knmi' ? trustAt(state.pos) : null;   // KNMI's reliability
  gpu.canvas.style.opacity = trust == null ? 1 : trustOpacity(trust, 0.35);
}

let drawQueued = false;
function requestDraw() {
  if (drawQueued) return;
  drawQueued = true;
  requestAnimationFrame(() => { drawQueued = false; drawRadar(); render(); });
}

// ---------- map ----------

const BOUNDS = L.latLngBounds(
  L.CRS.EPSG3857.unproject(L.point(X0, Y0)),
  L.CRS.EPSG3857.unproject(L.point(X1, Y1)),
);

// An image overlay whose "image" is our WebGL canvas.
const CanvasOverlay = L.ImageOverlay.extend({
  _initImage() {
    const el = this._image = this._url;
    L.DomUtil.addClass(el, 'leaflet-image-layer');
    if (this._zoomAnimated) L.DomUtil.addClass(el, 'leaflet-zoom-animated');
    if (this.options.className) L.DomUtil.addClass(el, this.options.className);
    el.onselectstart = L.Util.falseFn;
    el.onmousemove = L.Util.falseFn;
  },
});

const map = L.map('map', { zoomControl: false, attributionControl: false, minZoom: 6, maxZoom: 12 })
  .setView([FALLBACK.lat, FALLBACK.lng], 8);
map.createPane('labels');
map.getPane('labels').style.zIndex = 450;
map.getPane('labels').style.pointerEvents = 'none';
new CanvasOverlay(gpu.canvas, BOUNDS, { className: 'radar-frame', interactive: false }).addTo(map);

const dark = matchMedia('(prefers-color-scheme: dark)');
let baseLayers = [];
function setBasemap() {
  baseLayers.forEach((l) => l.remove());
  const canvas = `https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_${dark.matches ? 'Dark' : 'Light'}_Gray`;
  baseLayers = [
    L.tileLayer(`${canvas}_Base/MapServer/tile/{z}/{y}/{x}`),
    L.tileLayer(`${canvas}_Reference/MapServer/tile/{z}/{y}/{x}`, { pane: 'labels' }),
  ];
  baseLayers.forEach((l) => l.addTo(map));
}
setBasemap();
dark.addEventListener('change', setBasemap);

// Home country: everything outside the country you're in gets a light veil.
// The veil sits below the radar, so rain stays equally visible everywhere.
map.createPane('veil');
map.getPane('veil').style.zIndex = 350;
map.getPane('veil').style.pointerEvents = 'none';
const countries = fetch('countries.geojson').then((r) => r.json()).catch(() => null);
let homeLayers = [], homeName = null;

function inRing(ring, x, y) {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i], [xj, yj] = ring[j];
    if ((yi > y) !== (yj > y) && x < (xj - xi) * (y - yi) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

async function highlightCountry(pt) {
  const fc = await countries;
  if (!fc || state.point !== pt) return;
  const home = fc.features.find((f) => f.geometry.coordinates.some(([outer, ...holes]) =>
    inRing(outer, pt.lng, pt.lat) && !holes.some((h) => inRing(h, pt.lng, pt.lat))));
  if ((home?.properties.name ?? null) === homeName) return;
  homeName = home?.properties.name ?? null;
  homeLayers.forEach((l) => l.remove());
  homeLayers = [];
  if (!home) return;
  const css = getComputedStyle(document.documentElement);
  const flip = (ring) => ring.map(([x, y]) => [y, x]);
  const world = [[30, -40], [30, 50], [75, 50], [75, -40]];
  homeLayers = [
    L.polygon([world, ...home.geometry.coordinates.map(([outer]) => flip(outer))], {
      pane: 'veil', stroke: false, fillColor: css.getPropertyValue('--sky').trim(), fillOpacity: 0.55, interactive: false,
    }),
    L.polygon(home.geometry.coordinates.map(([outer]) => flip(outer)), {
      pane: 'veil', fill: false, color: css.getPropertyValue('--muted').trim(), weight: 1.2, opacity: 0.6, interactive: false,
    }),
  ];
  homeLayers.forEach((l) => l.addTo(map));
}
dark.addEventListener('change', () => { homeName = null; if (state.point) highlightCountry(state.point); });

// Radar coverage: outside the area the radars see there is no data, which must
// not look like "dry". Hatched, under the radar layer like the country veil.
let coverageLayers = [], coverageKey = '';

function showCoverage() {
  drawCoverage(state.mapSource === 'dwd' ? state.coverage.dwd : state.coverage.knmi);
}

function drawCoverage(ring) {
  if (!ring?.length) return;
  const key = `${ring.length}|${ring[0]}|${dark.matches}`;
  if (key === coverageKey) return;
  coverageKey = key;
  coverageLayers.forEach((l) => l.remove());
  const css = getComputedStyle(document.documentElement);
  const world = [[-85, -180], [-85, 180], [85, 180], [85, -180]];
  coverageLayers = [
    L.polygon([world, ring], { pane: 'veil', stroke: false, fillColor: 'url(#nodata-hatch)', fillOpacity: 1, interactive: false }),
    L.polygon(ring, { pane: 'veil', fill: false, color: css.getPropertyValue('--muted').trim(), weight: 1, opacity: 0.6, dashArray: '4 3', interactive: false }),
  ];
  coverageLayers.forEach((l) => l.addTo(map));
  // The hatch pattern has to live in the same SVG as the polygon.
  const svg = map.getRenderer(coverageLayers[0])._container;
  svg.querySelector('#nodata-hatch')?.parentNode.remove();
  const defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
  defs.innerHTML = `<pattern id="nodata-hatch" width="7" height="7" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
    <rect width="7" height="7" fill="${css.getPropertyValue('--sky').trim()}" fill-opacity="0.6"/>
    <line x1="0" y1="0" x2="0" y2="7" stroke="${css.getPropertyValue('--muted').trim()}" stroke-width="1.5" stroke-opacity="0.35"/></pattern>`;
  svg.insertBefore(defs, svg.firstChild);
}
dark.addEventListener('change', () => showCoverage());

const marker = L.marker([FALLBACK.lat, FALLBACK.lng], {
  icon: L.divIcon({ className: 'here', iconSize: [14, 14] }),
  keyboard: false, interactive: false,
});

map.on('click', (e) => {
  setPoint({ lat: e.latlng.lat, lng: e.latlng.lng, label: `Pinned ${e.latlng.lat.toFixed(2)}, ${e.latlng.lng.toFixed(2)}` }, false);
  $('locate').hidden = false;
});

// ---------- location ----------

function setPoint(pt, recenter) {
  state.point = pt;
  marker.setLatLng([pt.lat, pt.lng]).addTo(map);
  if (recenter) map.setView([pt.lat, pt.lng], 8);
  $('place').textContent = pt.label;
  state.observed = new Map();
  loadObserved();
  loadBuienradar(true);
  highlightCountry(pt);
  render();
}

function locate() {
  if (!navigator.geolocation) return useFallback('This browser has no location access.');
  // The geolocation timeout only starts once permission is granted, so show
  // De Bilt meanwhile if the permission prompt goes unanswered.
  const waiting = setTimeout(() => { if (!state.point) useFallback('Waiting for location access. Showing De Bilt until then.'); }, 6000);
  navigator.geolocation.getCurrentPosition(
    (p) => {
      clearTimeout(waiting);
      showStatus('');
      $('locate').hidden = true;
      setPoint({ lat: p.coords.latitude, lng: p.coords.longitude, label: 'Your location' }, true);
    },
    (err) => {
      clearTimeout(waiting);
      // Browsers give no second prompt once location is denied, so say where to fix it.
      const why = {
        1: 'Location is blocked for this browser, so this shows De Bilt. On iPhone, allow it under Settings → Privacy & Security → Location Services → (your browser) → While Using the App. Or tap the map to pick a spot.',
        2: 'Your location isn’t available right now, so this shows De Bilt. Tap the map to pick a spot.',
        3: 'Finding your location took too long, so this shows De Bilt. Try “Use my location” again, or tap the map.',
      }[err.code] ?? 'Location is off, so this shows De Bilt. Tap the map to pick a spot.';
      useFallback(why);
    },
    { enableHighAccuracy: false, timeout: 10000, maximumAge: 10 * 60e3 },
  );
}

function useFallback(msg) {
  setPoint({ ...FALLBACK, label: `${FALLBACK.name} (location off)` }, true);
  $('locate').hidden = false;
  showStatus(msg);
}

$('locate').addEventListener('click', locate);

// Refresh: there's no browser reload in a home-screen app. Re-fetch the app's
// own files past the browser cache (so a new version shows up right away),
// forget the stored data list (so data is checked now), then reload.
$('reload').addEventListener('click', async () => {
  $('reload').classList.add('spinning');
  await Promise.all(['./', 'app.js', 'style.css', 'countries.geojson', 'manifest.webmanifest']
    .map((u) => fetch(u, { cache: 'reload' }).catch(() => {})));
  store.set('plan', null);
  location.reload();
});

// ---------- loading ----------

function showStatus(msg) {
  $('status').textContent = msg;
  $('status').hidden = !msg;
}

const hhmm = (t) => new Date(t).toLocaleTimeString('nl-NL', { hour: '2-digit', minute: '2-digit' });

async function loadObserved() {
  const pt = state.point;
  // The job's frames carry exact values; only WMS frames need the point query.
  if (!pt || !state.t0 || state.src !== 'wms') return;
  // Same spot (to ~100 m) and same latest frame: the stored series is still exact.
  const key = `${pt.lat.toFixed(3)},${pt.lng.toFixed(3)}|${state.t0}`;
  const kept = store.get('observed');
  if (kept?.key === key) { state.observed = new Map(kept.series); render(); return; }
  try {
    const series = await observedSeries(pt, state.t0 - PAST * STEP, state.t0);
    store.set('observed', { key, series: [...series] });
    if (state.point === pt) { state.observed = series; render(); }
  } catch (e) {
    console.warn('observed series failed', e);
  }
}

// ---------- Buienradar comparison ----------
// Buienradar's own point forecast, drawn as a thin line over the KNMI curve.
// Their feed terms: free use with credit and a link to buienradar.nl (footer).

const BUIENRADAR = 'https://graphdata.buienradar.nl/2.0/forecast/geo/RainHistoryForecast';
const br = { key: null, at: 0, series: new Map() };

async function loadBuienradar(force) {
  const pt = state.point;
  if (!pt) return;
  const key = `${pt.lat.toFixed(2)},${pt.lng.toFixed(2)}`;
  if (!force && br.key === key && Date.now() - br.at < 4 * 60e3) return;
  try {
    const res = await fetch(`${BUIENRADAR}?lat=${pt.lat.toFixed(2)}&lon=${pt.lng.toFixed(2)}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const d = await res.json();
    if (state.point !== pt) return;
    br.key = key;
    br.at = Date.now();
    br.series = new Map(d.forecasts.map((f) => [Date.parse(`${f.utcdatetime}Z`), f.precipitation]));
    render();
  } catch (e) {
    console.warn('Buienradar unavailable', e);
  }
}

// ---------- DWD ----------
// DWD's nowcast (job/dwd.py) for the Netherlands window at ~1 km: the forecast
// (+5 … +120 min) stacked in one image, plus each run's analysis kept as an
// observed frame for the past hour. 255 = no data (DWD covers only the east).
// Feeds the comparison line on the graph and, when switched on, the map.

const dwd = { key: null, meta: null, fc: null, obs: new Map(), frames: [], built: new Map() };

async function greyImage(url) {
  const img = await cachedImage(url, { colorSpaceConversion: 'none', premultiplyAlpha: 'none' });
  const ctx = new OffscreenCanvas(img.width, img.height).getContext('2d', { willReadFrequently: true });
  ctx.drawImage(img, 0, 0);
  const d = ctx.getImageData(0, 0, img.width, img.height).data;
  const bytes = new Uint8Array(img.width * img.height);
  for (let i = 0; i < bytes.length; i++) bytes[i] = d[i * 4];
  return bytes;
}

async function loadDwd(meta) {
  if (!meta?.fc) return;
  const key = [meta.fc, ...meta.obs.map((o) => o.file)].join();
  if (key === dwd.key) return;
  try {
    const [fc, ...obs] = await Promise.all([meta.fc, ...meta.obs.map((o) => o.file)].map((f) => greyImage(meta.base + f)));
    Object.assign(dwd, { key, meta, fc, obs: new Map(meta.obs.map((o, i) => [Date.parse(o.t), obs[i]])) });
    state.coverage.dwd = meta.coverage;
    $('src-dwd').hidden = false;
    syncDwdFrames();
    if (state.mapSource === 'dwd') showCoverage();
    render();
  } catch (e) {
    console.warn('DWD unavailable', e);
  }
}

// DWD's window image (observed or forecast) for time t, or null.
function dwdWindow(t) {
  const m = dwd.meta;
  if (!m || !dwd.fc) return null;
  const ref = Date.parse(m.ref);
  if (t <= ref) return dwd.obs.get(t) ?? null;
  const i = Math.round((t - ref) / STEP) - 1;
  return i >= 0 && i < 24 ? dwd.fc.subarray(i * m.w * m.h, (i + 1) * m.w * m.h) : null;
}

// DWD's values at the point: time → mm/h (no entries where DWD has no data).
function dwdSeries(pt) {
  const out = new Map();
  const m = dwd.meta;
  if (!m || !dwd.fc || !pt) return out;
  const [px, py] = pixelOf(pt);
  const ax = px - m.x0, ay = py - m.y0;
  if (ax < 0 || ay < 0 || ax >= m.w || ay >= m.h) return out;
  for (const f of state.frames) {
    const win = dwdWindow(f.t);
    const b = win?.[ay * m.w + ax];
    if (b != null && b !== 255) out.set(f.t, fromByte(b));
  }
  return out;
}

// Map frames from DWD on the same timeline as KNMI's (built only when shown).
function syncDwdFrames() {
  if (state.mapSource !== 'dwd' || !dwd.meta) return;
  const m = dwd.meta, keep = new Map();
  dwd.frames = state.frames.map((f) => {
    const win = dwdWindow(f.t);
    const id = `${dwd.key}|${f.t}`;
    let fr = dwd.built.get(id);
    if (!fr) {
      fr = { t: f.t };
      if (win) {
        const bytes = new Uint8Array(W * H);
        for (let y = 0; y < m.h; y++) {
          for (let x = 0; x < m.w; x++) {
            const b = win[y * m.w + x];
            if (b !== 255) bytes[(m.y0 + y) * W + m.x0 + x] = b;
          }
        }
        fr.bytes = bytes;
        fr.tex = gpu.frameTexture(bytes);
      }
    }
    keep.set(id, fr);
    return fr;
  });
  for (const [id, fr] of dwd.built) {
    if (!keep.has(id)) { gpu.drop(fr.tex); gpu.drop(fr.flowTex); }
  }
  dwd.built = keep;
  for (const fr of dwd.frames) { gpu.drop(fr.flowTex); fr.flowTex = null; }
  computeFlows(dwd.frames);
  requestDraw();
}

// Map source toggle: KNMI (default) or DWD. Remembered per device.
function setMapSource(src) {
  state.mapSource = src === 'dwd' && dwd.meta ? 'dwd' : 'knmi';
  store.set('map', state.mapSource);
  for (const b of document.querySelectorAll('.source button')) {
    b.setAttribute('aria-pressed', String(b.dataset.src === state.mapSource));
  }
  syncDwdFrames();
  showCoverage();
  requestDraw();
}
for (const b of document.querySelectorAll('.source button')) {
  b.addEventListener('click', () => setMapSource(b.dataset.src));
}

// ---------- loading indicator ----------

const loading = { started: 0, timer: null, error: '' };

function updateLoading() {
  const fr = state.frames;
  const done = fr.filter((f) => f.bytes).length;
  if (fr.length && done === fr.length) {
    $('loading').hidden = true;
    clearInterval(loading.timer);
    loading.timer = null;
    return;
  }
  $('loading').hidden = false;
  const slow = state.src === 'wms' && Date.now() - loading.started > 12e3 ? ' · KNMI is slow' : '';
  $('loading-text').textContent = loading.error
    || (fr.length ? `Loading radar ${done}/${fr.length}${slow}` : 'Connecting to KNMI…');
  $('loading-bar').style.width = fr.length ? `${done / fr.length * 100}%` : '0%';
}

function startLoading() {
  loading.started = Date.now();
  loading.error = '';
  clearInterval(loading.timer);
  loading.timer = setInterval(updateLoading, 1000);
  updateLoading();
}

async function refresh() {
  if (state.refreshing) return;
  state.refreshing = true;
  try { await update(); } finally { state.refreshing = false; }
}

// Latest times and where to get each frame: the job's index when fresh,
// otherwise KNMI's WMS.
async function fetchPlan() {
  try {
    const res = await fetch(`${DATA}index.json?v=${Math.floor(Date.now() / 30e3)}`, { cache: 'no-store' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const idx = await res.json();
    const t0 = Date.parse(idx.t0);
    if (Date.now() - t0 < DATA_STALE) {
      const entries = (list, kind) => list.map((e) => [`${kind}|${Date.parse(e.t)}`, DATA + e.file]);
      const extra = (name) => fetch(`${DATA}${name}?v=${Math.floor(Date.now() / 30e3)}`, { cache: 'no-store' })
        .then((r) => (r.ok ? r.json() : null)).catch(() => null);
      const [skill, coverage, compare] = await Promise.all([extra('skill.json'), extra('coverage.json'), extra('compare.json')]);
      const dwd = idx.dwd?.fc ? { ...idx.dwd, base: DATA } : null;
      return { t0, ref: Date.parse(idx.ref), data: [...entries(idx.obs, 'obs'), ...entries(idx.fc, 'fc')], skill, coverage, compare, dwd };
    }
    console.warn('job output is stale; using KNMI directly');
  } catch (e) {
    console.warn('job output unavailable; using KNMI directly', e);
  }
  const [obs, fc] = await Promise.all([latestTimes(OBS), latestTimes(FC)]);
  return { t0: obs.time, ref: fc.ref ?? obs.time, data: null };
}

// The timeline is a regular 5-minute grid; any frame the job lacks comes from the WMS.
function planFrames({ t0, ref, data }) {
  const lookup = new Map(data ?? []);
  const spec = (kind, t, r) => {
    const url = lookup.get(`${kind}|${t}`);
    return url ? { kind, t, ref: r, src: 'data', url } : { kind, t, ref: r, src: 'wms', url: wmsUrl(kind, t, r) };
  };
  const frames = [];
  for (let k = PAST; k >= 0; k--) frames.push(spec('obs', t0 - k * STEP, null));
  // A forecast run can be newer than the latest observation; start right after it.
  for (let t = t0 + STEP; t <= ref + AHEAD * STEP; t += STEP) frames.push(spec('fc', t, ref));
  return frames;
}

async function update() {
  loading.error = '';
  let plan, wanted;
  const known = store.get('plan');
  try {
    // Fresh page, but nothing newer can have been published yet: reuse.
    plan = !state.t0 && known && Date.now() < dueAfter(known.t0, known.ref) ? known : await fetchPlan();
    wanted = planFrames(plan);
    if (!state.lut && wanted.some((f) => f.src === 'wms')) state.lut = await loadLegend();
  } catch (e) {
    return failed(e);
  }
  store.set('plan', plan);
  state.skill = plan.skill ?? null;
  state.compare = plan.compare ?? null;
  loadDwd(plan.dwd);
  if (plan.coverage) store.set('coverage', plan.coverage);
  state.coverage.knmi = plan.coverage ?? store.get('coverage');
  showCoverage();
  const { t0, ref } = plan;
  const due = dueAfter(t0, ref);
  state.nextCheck = Date.now() < due ? due : Date.now() + POLL_EVERY;
  // Nothing new and nothing missing: done. (Missing frames from a failed load are retried.)
  if (t0 === state.t0 && ref === state.ref && state.frames.every((f) => f.bytes)) return;
  state.src = plan.data ? 'data' : 'wms';
  loadBuienradar(false);

  // Keep frames we already have (observed frames never change). Flows are
  // recomputed, since a frame's neighbour may have changed.
  const have = new Map(state.frames.map((f) => [f.url, f]));
  const next = wanted.map((w) => have.get(w.url) ?? w);
  for (const f of state.frames) {
    gpu.drop(f.flowTex); f.flowTex = null;
    if (!next.includes(f)) gpu.drop(f.tex);
  }

  const atNow = !state.userScrubbed || Math.abs(state.pos - state.nowIdx) < 0.01;
  state.frames = next;
  state.nowIdx = PAST;
  state.t0 = t0; state.ref = ref;
  if (atNow) state.pos = state.nowIdx;
  $('updated').textContent = `radar ${hhmm(t0)}`;
  loadObserved();
  computeFlows();
  requestDraw();
  pruneCache(new Set([...next.map((f) => f.url), ...(plan.dwd ? [plan.dwd.fc, ...plan.dwd.obs.map((o) => o.file)].map((f) => DATA + f) : [])]));
  syncDwdFrames();

  // Now first, then the forecast in order, then the past.
  const idx = next.map((_, i) => i);
  const order = [state.nowIdx, ...idx.filter((i) => i > state.nowIdx), ...idx.filter((i) => i < state.nowIdx).reverse()]
    .map((i) => next[i]).filter((f) => !f.bytes);
  if (order.length && !loading.timer) startLoading();
  let error = null;
  const worker = async () => {
    while (order.length) {
      const f = order.shift();
      try { await loadFrame(f); } catch (e) { error = e; }
      updateLoading();
    }
  };
  await Promise.all(Array.from({ length: CONCURRENCY }, worker));
  if (error) failed(error);
  else if (!/De Bilt/.test($('status').textContent)) showStatus('');
}

function failed(e) {
  console.warn(e);
  state.nextCheck = Date.now() + 60e3; // retry in a minute
  loading.error = 'KNMI isn’t responding · retrying in a minute';
  updateLoading();
  showStatus(e instanceof RateLimited
    ? 'KNMI’s shared radar service is busy right now. Trying again in a minute.'
    : 'KNMI’s radar service didn’t respond. Trying again in a minute.');
}

const checkIfDue = () => {
  if (document.visibilityState === 'visible' && Date.now() >= state.nextCheck) refresh();
};
setInterval(checkIfDue, 10e3);
document.addEventListener('visibilitychange', checkIfDue);

// ---------- timeline & playback ----------

function seek(pos) {
  state.pos = Math.max(0, Math.min(Math.max(state.frames.length - 1, 0), pos));
  requestDraw();
}

function stop() {
  state.playing = false;
  $('play').setAttribute('aria-pressed', 'false');
  $('play').setAttribute('aria-label', 'Play radar animation');
}

$('play').addEventListener('click', () => {
  if (state.playing) return stop();
  state.userScrubbed = true;
  state.playing = true;
  $('play').setAttribute('aria-pressed', 'true');
  $('play').setAttribute('aria-label', 'Pause radar animation');
  const last = state.frames.length - 1;
  if (state.pos >= last) seek(0);
  let prev = performance.now(), hold = 0;
  const tick = (now) => {
    if (!state.playing) return;
    const dt = now - prev; prev = now;
    const end = state.frames.length - 1;
    if (state.pos >= end) {
      hold += dt;
      if (hold > 1200) { hold = 0; seek(0); }
    } else {
      seek(Math.min(end, state.pos + dt / PLAY_MS));
    }
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
});

// ---------- forecast reliability ----------
// job/verify.py scores how well recent forecasts held up per lead time: the
// share of rain forecast in the right place at ~4 km (CSI, 0–100), for ~40 km
// tiles around each place. Here: the values for the tile the point is in, or for
// the whole country when the local area had too little rain.

const GOOD = 60;   // CSI×100 from which a forecast "held up": 60% of rain moments right

function reliability(pt) {
  const sk = DEMO?.skill ?? state.skill;
  if (!sk || !pt) return null;
  const [px, py] = pixelOf(pt);
  const tx = Math.floor(px / sk.cell / sk.tile), ty = Math.floor(py / sk.cell / sk.tile);
  const local = tx >= 0 && ty >= 0 && tx < sk.nx && ty < sk.ny ? sk.tiles[ty * sk.nx + tx] : null;
  const known = (a) => a && a.filter((v) => v >= 0).length >= a.length / 2;
  if (known(local)) return { fss: local, area: 'here' };
  if (known(sk.nl)) return { fss: sk.nl, area: 'the Netherlands' };
  return { fss: null, area: null };
}

// Minutes ahead for which forecasts kept scoring GOOD (0 if not even 5 min).
function horizon(fss) {
  let k = 0;
  while (k < fss.length && fss[k] >= GOOD) k++;
  return k * 5;
}

// Reliability (CSI×100) at a timeline position, interpolated between lead
// times; null for the past or when unknown.
function trustAt(pos) {
  const r = reliability(state.point);
  if (!r?.fss || pos <= state.nowIdx) return null;
  const lead = pos - state.nowIdx;                    // in 5-minute steps
  if (lead < 1) return r.fss[0] < 0 ? null : 100 + (r.fss[0] - 100) * lead;
  const k = Math.min(r.fss.length - 1, lead - 1), i = Math.floor(k);
  const a = r.fss[i], b = r.fss[Math.min(i + 1, r.fss.length - 1)];
  return a < 0 || b < 0 ? null : a + (b - a) * (k - i);
}

const trustOpacity = (v, floor = 0.25) => Math.max(floor, Math.min(1, floor + (1 - floor) * (v - 20) / (GOOD - 20)));
const trustWord = (v) => `${v >= 70 ? 'high' : v >= 50 ? 'moderate' : v >= 30 ? 'low' : 'very low'} reliability`;

// KNMI vs Buienradar, 1 hour ahead, over the last days (job/compare.py).
function drawCompare() {
  const c = state.compare, p = $('versus');
  const k = c?.knmi?.[12], b = c?.br?.[12];
  if (!k || !b) { p.hidden = true; return; }
  p.hidden = false;
  if (k.events < 150 || k.csi == null || b.csi == null) {
    p.textContent = `Comparing KNMI and Buienradar forecasts: ${k.events} rain moments scored so far, results after a few rainy days.`;
    return;
  }
  const pct = (v) => `${Math.round(v * 100)}%`;
  const days = c.days === 1 ? 'today' : `last ${c.days} days`;
  const e = c.east, ek = e?.knmi?.[12], eb = e?.br?.[12], ed = e?.dwd?.[12];
  const east = ek && ed && ek.events >= 150 && ek.csi != null && eb.csi != null && ed.csi != null
    ? ` Eastern NL, where DWD covers too: KNMI ${pct(ek.csi)} · Buienradar ${pct(eb.csi)} · DWD ${pct(ed.csi)}.` : '';
  p.textContent = `Rain correctly forecast 1 hour ahead (${days}): KNMI ${pct(k.csi)} · Buienradar ${pct(b.csi)}.${east}`;
}

function drawTrust() {
  const r = reliability(state.point);
  const p = $('trust');
  if (!r) { p.hidden = true; return; }
  p.hidden = false;
  if (!r.fss) {
    p.textContent = `Too little rain in the last ${(DEMO?.skill ?? state.skill).hours} hours to judge how reliable forecasts are today.`;
    return;
  }
  const m = horizon(r.fss);
  const where = r.area === 'here' ? '' : ` across ${r.area}`;
  p.textContent = m >= 120 ? `Today's forecasts${where} held up well for the full 2 hours.`
    : m >= 10 ? `Today's forecasts${where} held up well for about ${m} minutes; less reliable beyond.`
    : `Today's forecasts${where} have been unreliable, even 10 minutes ahead.`;
}

// ---------- graph & verdict ----------

const NS = 'http://www.w3.org/2000/svg';
const graph = $('graph');
const el = (tag, attrs, parent) => {
  const n = document.createElementNS(NS, tag);
  for (const k in attrs) n.setAttribute(k, attrs[k]);
  parent.appendChild(n);
  return n;
};

function intensity(v) {
  return v < 1 ? 'light' : v < 5 ? 'moderate' : 'heavy';
}

// Rain at the point per frame: measured for the past and now, forecast after.
function pointSeries() {
  if (DEMO) return state.frames.map((f) => DEMO.rain((f.t - state.t0) / 60e3));
  return state.frames.map((f, i) => {
    if (i <= state.nowIdx) {
      const v = state.observed.get(f.t);
      return v !== undefined ? v : sample(f, state.point);
    }
    return sample(f, state.point);
  });
}

function render() {
  const series = pointSeries();
  drawGraph(series);
  drawVerdict(series);
  drawTrust();
  drawCompare();
  drawReadout(series);
}

// Monotone cubic spline (Fritsch–Carlson) through [x, y] points: smooth, but
// never overshoots, so dry stretches stay flat on the baseline.
function monotonePath(pts) {
  const n = pts.length;
  if (n < 2) return n ? `M${pts[0][0]},${pts[0][1]}` : '';
  const dx = [], m = [], t = new Array(n);
  for (let i = 0; i < n - 1; i++) {
    dx[i] = pts[i + 1][0] - pts[i][0];
    m[i] = (pts[i + 1][1] - pts[i][1]) / dx[i];
  }
  t[0] = m[0]; t[n - 1] = m[n - 2];
  for (let i = 1; i < n - 1; i++) t[i] = m[i - 1] * m[i] <= 0 ? 0 : (m[i - 1] + m[i]) / 2;
  for (let i = 0; i < n - 1; i++) {
    if (m[i] === 0) { t[i] = t[i + 1] = 0; continue; }
    const a = t[i] / m[i], b = t[i + 1] / m[i], s = a * a + b * b;
    if (s > 9) { const k = 3 / Math.sqrt(s); t[i] = k * a * m[i]; t[i + 1] = k * b * m[i]; }
  }
  let d = `M${pts[0][0]},${pts[0][1]}`;
  for (let i = 0; i < n - 1; i++) {
    const [x0, y0] = pts[i], [x1, y1] = pts[i + 1], h = dx[i] / 3;
    d += `C${x0 + h},${y0 + t[i] * h},${x1 - h},${y1 - t[i + 1] * h},${x1},${y1}`;
  }
  return d;
}

function drawGraph(series) {
  const w = graph.clientWidth || 360, h = graph.clientHeight || 132;
  const top = 14, base = h - 30, plot = base - top;
  graph.setAttribute('viewBox', `0 0 ${w} ${h}`);
  graph.replaceChildren();
  const n = Math.max(series.length, PAST + 1 + AHEAD);
  const slot = w / n;
  const yOf = (v) => (v < 0.1 ? base : base - Math.min(1, Math.log10(v / 0.1) / Math.log10(200)) * plot);
  const cx = (i) => i * slot + slot / 2;
  const nowX = cx(state.nowIdx);

  // Fill colour follows height, and height follows intensity, so a vertical
  // gradient through the rain palette colours the curve by intensity.
  const defs = el('defs', {}, graph);
  const grad = el('linearGradient', { id: 'rainfill', gradientUnits: 'userSpaceOnUse', x1: 0, y1: base, x2: 0, y2: top }, defs);
  for (const v of [0.1, 0.3, 0.6, 1, 2, 4, 8, 20]) {
    el('stop', { offset: (base - yOf(v)) / plot, 'stop-color': cssColor(v) }, grad);
  }
  el('rect', { x: 0, y: 0, width: nowX, height: h }, el('clipPath', { id: 'clip-past' }, defs));
  el('rect', { x: nowX, y: 0, width: w - nowX, height: h }, el('clipPath', { id: 'clip-ahead' }, defs));
  // Forecast fades out with today's reliability at each lead time.
  const rel = reliability(state.point);
  if (rel?.fss) {
    const fade = el('linearGradient', { id: 'trustfade', gradientUnits: 'userSpaceOnUse', x1: nowX, y1: 0, x2: w, y2: 0 }, defs);
    el('stop', { offset: 0, 'stop-color': '#fff' }, fade);
    rel.fss.forEach((v, k) => {
      const x = cx(state.nowIdx + k + 1);
      if (x > w || v < 0) return;
      const op = trustOpacity(v);
      el('stop', { offset: Math.max(0, Math.min(1, (x - nowX) / (w - nowX))), 'stop-color': '#fff', 'stop-opacity': op }, fade);
    });
    el('rect', { x: nowX, y: 0, width: w - nowX, height: h, fill: 'url(#trustfade)' }, el('mask', { id: 'trust-mask', maskUnits: 'userSpaceOnUse', x: 0, y: 0, width: w, height: h }, defs));
  }

  for (const [v, label] of [[1, 'moderate'], [5, 'heavy']]) {
    const y = yOf(v);
    el('line', { x1: 0, x2: w, y1: y, y2: y, class: 'grid', 'stroke-dasharray': '2 4' }, graph);
    el('text', { x: w, y: y - 4, 'text-anchor': 'end', class: 'band' }, graph).textContent = label;
  }
  el('line', { x1: 0, x2: w, y1: base, y2: base, class: 'grid' }, graph);

  // Reliability strip under the forecast: solid where today's forecasts held
  // up, fading gradually as they held up less.
  if (rel?.fss) {
    const strip = el('linearGradient', { id: 'truststrip', gradientUnits: 'userSpaceOnUse', x1: nowX, y1: 0, x2: w, y2: 0 }, defs);
    const color = getComputedStyle(document.documentElement).getPropertyValue('--rain').trim();
    el('stop', { offset: 0, 'stop-color': color }, strip);
    rel.fss.forEach((v, k) => {
      const x = cx(state.nowIdx + k + 1);
      if (x > w || v < 0) return;
      el('stop', { offset: Math.max(0, Math.min(1, (x - nowX) / (w - nowX))), 'stop-color': color, 'stop-opacity': trustOpacity(v, 0.1) }, strip);
    });
    const sy = base + 5;
    el('rect', { x: nowX, y: sy, width: w - nowX, height: 4, rx: 2, fill: 'url(#truststrip)' }, graph);
    el('text', { x: nowX - 6, y: sy + 4, 'text-anchor': 'end', class: 'band' }, graph).textContent = 'reliability';
  }

  // Curve per run of loaded values; frames still loading pulse on the baseline.
  let run = [];
  const flush = () => {
    if (run.length > 1) {
      const line = monotonePath(run);
      const area = `${line}L${run[run.length - 1][0]},${base}L${run[0][0]},${base}Z`;
      for (const [clip, cls] of [['clip-past', 'past'], ['clip-ahead', '']]) {
        const g = el('g', { 'clip-path': `url(#${clip})`, class: cls }, graph);
        if (clip === 'clip-ahead' && rel?.fss) g.setAttribute('mask', 'url(#trust-mask)');
        el('path', { d: area, class: 'area' }, g);
        el('path', { d: line, class: 'curve' }, g);
      }
    }
    run = [];
  };
  series.forEach((v, i) => {
    if (v === null) {
      flush();
      // Negative delay keeps the pulse in phase across redraws.
      el('line', { x1: cx(i) - slot / 2, x2: cx(i) + slot / 2, y1: base - 1.5, y2: base - 1.5, class: 'pending', style: `animation-delay:-${performance.now() % 1400}ms` }, graph);
    } else {
      run.push([cx(i), yOf(v)]);
    }
  });
  flush();

  // Buienradar's forecast for the same spot, for comparison.
  const brPts = [];
  state.frames.forEach((f, i) => {
    const v = br.series.get(f.t);
    if (v != null) brPts.push([cx(i), yOf(v)]);
  });
  if (brPts.length > 1) {
    el('path', { d: monotonePath(brPts), class: 'br' }, graph);
    el('line', { x1: w - 142, x2: w - 128, y1: 5, y2: 5, class: 'br' }, graph);
    el('text', { x: w - 124, y: 8, class: 'band' }, graph).textContent = 'Buienradar';
  }
  // DWD's forecast, where it covers the point (eastern Netherlands).
  const dw = dwdSeries(state.point);
  const dwPts = [];
  state.frames.forEach((f, i) => {
    const v = dw.get(f.t);
    if (v != null && i >= state.nowIdx) dwPts.push([cx(i), yOf(v)]);
  });
  if (dwPts.length > 1) {
    el('path', { d: monotonePath(dwPts), class: 'dwd' }, graph);
    el('line', { x1: w - 42, x2: w - 28, y1: 5, y2: 5, class: 'dwd' }, graph);
    el('text', { x: w, y: 8, 'text-anchor': 'end', class: 'band' }, graph).textContent = 'DWD';
  }

  el('line', { x1: nowX, x2: nowX, y1: 2, y2: base, class: 'now' }, graph);
  el('text', { x: nowX, y: h - 4, 'text-anchor': 'middle', class: 'tick now-label' }, graph).textContent = 'now';
  // Labels every 15 minutes, thinned to every 30 where they'd touch (narrow screens).
  const gap = 27;
  const every = slot * 3 >= gap ? 15 : 30;
  state.frames.forEach((f, i) => {
    const x = cx(i);
    if (Math.abs(x - nowX) < gap || x < gap / 2 || x > w - gap / 2) return;
    if (new Date(f.t).getMinutes() % every !== 0) return;
    el('text', { x, y: h - 4, 'text-anchor': 'middle', class: 'tick' }, graph).textContent = hhmm(f.t);
  });

  if (Math.abs(state.pos - state.nowIdx) > 0.01 && state.frames.length) {
    const x = state.pos * slot + slot / 2;
    el('line', { x1: x, x2: x, y1: 2, y2: base, class: 'cursor' }, graph);
    const v = valueAt(series, state.pos);
    el('circle', { cx: x, cy: v == null ? base : yOf(v), r: 3.5, class: 'cursor-dot' }, graph);
  }
}

// Point value between frames, interpolated in log space like the map.
function valueAt(series, pos) {
  const i = Math.floor(pos), t = pos - i;
  const a = series[i], b = series[i + 1];
  if (a == null) return b ?? null;
  if (b == null || t < 0.001) return a;
  return fromByte(Math.round(toByte(a) + (toByte(b) - toByte(a)) * t));
}

function drawVerdict(series) {
  let ahead = series.slice(state.nowIdx);
  // KNMI unavailable: fall back to Buienradar's forecast for the verdict.
  if (ahead.some((v) => v === null) && loading.error) {
    const alt = state.frames.slice(state.nowIdx).map((f) => br.series.get(f.t) ?? null);
    if (alt.length && alt.every((v) => v !== null)) {
      ahead = alt;
      verdictFor(ahead);
      $('detail').textContent += ' (Buienradar forecast; KNMI data unavailable.)';
      return;
    }
  }
  verdictFor(ahead);
}

function verdictFor(ahead) {
  const t = (i) => `<span class="t">${hhmm(state.frames[state.nowIdx + i].t)}</span>`;
  if (!state.point || !ahead.length || ahead.some((v) => v === null)) {
    $('verdict').textContent = 'Reading the radar…';
    $('detail').textContent = '';
    return;
  }
  const until = ahead.length - 1;
  const endTime = hhmm(state.frames[state.frames.length - 1].t);
  const wet = (v) => v >= WET;
  // Rain stops when the next 10 minutes are dry.
  const dryFrom = (from) => {
    for (let i = from; i < ahead.length; i++) if (!wet(ahead[i]) && !wet(ahead[i + 1] ?? 0)) return i;
    return -1;
  };

  if (wet(ahead[0])) {
    const j = dryFrom(1);
    const peak = Math.max(...ahead.slice(0, j < 0 ? undefined : j));
    $('verdict').innerHTML = j < 0 ? `Rain until at least ${t(until)}` : `Dry from ${t(j)}`;
    $('detail').textContent = `${cap(intensity(ahead[0]))} rain now, up to ${peak.toFixed(1)}\u00a0mm/h.`;
    return;
  }
  // Ignore lone 5-minute drizzle blips; they're mostly radar noise.
  const s = ahead.findIndex((v, i) => wet(v) && (v >= 0.5 || wet(ahead[i + 1] ?? 0)));
  if (s < 0) {
    $('verdict').textContent = 'Dry for the next 2 hours';
    $('detail').textContent = `No rain expected here until at least ${endTime}.`;
    return;
  }
  const e = dryFrom(s + 1);
  const spell = ahead.slice(s, e < 0 ? undefined : e);
  const peak = Math.max(...spell);
  const mins = spell.length * 5;
  $('verdict').innerHTML = `Dry until ${t(s)}`;
  $('detail').textContent = e < 0
    ? `Then ${intensity(peak)} rain, up to ${peak.toFixed(1)}\u00a0mm/h, lasting past ${endTime}.`
    : `Then ${intensity(peak)} rain for about ${mins} minutes, up to ${peak.toFixed(1)}\u00a0mm/h.`;
}

const cap = (s) => s[0].toUpperCase() + s.slice(1);

function drawReadout(series) {
  const fr = state.frames;
  if (!fr.length) return;
  const atNow = Math.abs(state.pos - state.nowIdx) < 0.01;
  const tm = fr[0].t + state.pos * STEP;
  const v = valueAt(series, state.pos);
  const kind = state.pos > state.nowIdx ? 'forecast' : 'radar';
  const when = atNow ? `now (${hhmm(tm)})` : `${hhmm(tm)} ${kind}`;
  const what = v === null ? 'loading…' : v < WET ? 'dry' : `${intensity(v)} rain, ${v.toFixed(1)}\u00a0mm/h`;
  const trust = trustAt(state.pos);
  const sure = trust == null || atNow ? '' : ` · ${trustWord(trust)}`;
  const bv = br.series.get(fr[Math.round(state.pos)]?.t);
  const dv = dwdSeries(state.point).get(fr[Math.round(state.pos)]?.t);
  const fmt = (x) => (x < WET ? 'dry' : `${x.toFixed(1)}\u00a0mm/h`);
  const other = (bv == null ? '' : ` · Buienradar ${fmt(bv)}`) + (dv == null || Math.round(state.pos) < state.nowIdx ? '' : ` · DWD ${fmt(dv)}`);
  $('readout').textContent = `${when} · ${what}${sure}${other}`;
}

// Scrub the shared timeline by dragging across the graph; it moves continuously.
function scrubTo(e) {
  const r = graph.getBoundingClientRect();
  const n = Math.max(state.frames.length, 1);
  seek((e.clientX - r.left) / r.width * n - 0.5);
}
graph.setAttribute('tabindex', '0');
graph.addEventListener('pointerdown', (e) => {
  stop();
  state.userScrubbed = true;
  graph.setPointerCapture(e.pointerId);
  scrubTo(e);
});
graph.addEventListener('pointermove', (e) => { if (graph.hasPointerCapture(e.pointerId)) scrubTo(e); });
graph.addEventListener('keydown', (e) => {
  if (e.key === 'Home') { stop(); seek(state.nowIdx); e.preventDefault(); return; }
  const d = { ArrowLeft: -1, ArrowRight: 1 }[e.key];
  if (!d) return;
  stop();
  state.userScrubbed = true;
  seek(Math.round(state.pos) + d);
  e.preventDefault();
});
graph.addEventListener('dblclick', () => seek(state.nowIdx));
addEventListener('resize', render);

// ---------- start ----------

if (!gpu.ok) showStatus('This browser can’t draw the radar map (WebGL is unavailable).');
state.mapSource = store.get('map') === 'dwd' ? 'dwd' : 'knmi';
for (const b of document.querySelectorAll('.source button')) b.setAttribute('aria-pressed', String(b.dataset.src === state.mapSource));
startLoading();
locate();
refresh();
