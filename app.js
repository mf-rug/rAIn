'use strict';

// KNMI's keyless WMS. Its GetFeatureInfo is exact for observed rain but returns
// wrong values for forecast time series, so forecast amounts are read back from
// the rendered frames: the smooth style is a continuous log colour ramp
// (0.1–40 mm/h) that inverts cleanly.
const WMS = 'https://anonymous.api.dataplatform.knmi.nl/wms/adaguc-server';
const STYLE = 'precip-smooth-white-to-purple/bilinear';
const OBS = { dataset: 'radar_reflectivity_composites', layer: 'precipitation' };
const FC = { dataset: 'radar_forecast', layer: 'precipitationfc' };

// Full radar domain in Web Mercator, as advertised by the WMS. A pixel is 1.15
// Mercator km, which at Dutch latitudes is ~0.7 km on the ground, so this
// slightly oversamples the native 1 km radar grid.
const X0 = 0, Y0 = 6257115.396264, X1 = 1208534.924976, Y1 = 7553162.131589;
const W = 1050, H = 1126;

const STEP = 5 * 60e3;
const PAST = 12;           // observed frames before "now" (1 hour)
const AHEAD = 24;          // forecast frames (2 hours)
const WET = 0.15;          // mm/h; below this counts as dry
const CONCURRENCY = 4;
const CHECK_EVERY = 4.5 * 60e3;
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
const state = {
  point: null,          // {lat, lng, label}
  frames: [],           // ordered by time
  observed: new Map(),  // time → mm/h at the point, from GetFeatureInfo
  nowIdx: PAST,
  pos: PAST,            // continuous position on the timeline, in frames
  t0: null, ref: null,
  lastCheck: 0,
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

async function loadImage(url) {
  const blob = await (await get(url)).blob();
  return createImageBitmap(blob);
}

// Legend bar: 0.1 mm/h at y=363, 40 mm/h at y=20, logarithmic.
async function loadLegend() {
  const img = await loadImage(`${WMS}?DATASET=${FC.dataset}&SERVICE=WMS&VERSION=1.3.0&REQUEST=GetLegendGraphic&LAYER=${FC.layer}&STYLE=${STYLE}&FORMAT=image/png`);
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

function frameUrl(f) {
  const src = f.kind === 'obs' ? OBS : FC;
  let u = `${WMS}?DATASET=${src.dataset}&SERVICE=WMS&VERSION=1.3.0&REQUEST=GetMap&LAYERS=${src.layer}&STYLES=${STYLE}` +
    `&CRS=EPSG:3857&BBOX=${X0},${Y0},${X1},${Y1}&WIDTH=${W}&HEIGHT=${H}&FORMAT=image/png&TRANSPARENT=true&TIME=${iso(f.t)}`;
  if (f.kind === 'fc') u += `&DIM_REFERENCE_TIME=${iso(f.ref)}`;
  return u;
}

// Fetch one frame and decode it to the byte encoding.
async function loadFrame(f) {
  const img = await loadImage(frameUrl(f));
  const ctx = new OffscreenCanvas(W, H).getContext('2d', { willReadFrequently: true });
  ctx.drawImage(img, 0, 0, W, H);
  const d = ctx.getImageData(0, 0, W, H).data;
  const bytes = new Uint8Array(W * H);
  for (let i = 0, p = 0; i < bytes.length; i++, p += 4) {
    if (d[p + 3] >= 64) bytes[i] = invert(state.lut, d[p], d[p + 1], d[p + 2]);
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

// Forecast rain at a point: mean of the 3×3 pixels around it (~3.5 km box).
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
const BLOCK = 16;          // half block size, quarter pixels (~45 km on the ground)
const SEARCH = 4;          // max shift per 5 min, quarter pixels (~11 km, i.e. ~135 km/h)

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

function computeFlows() {
  const fr = state.frames;
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
  const fr = state.frames;
  if (!fr.length) return gpu.draw(null);
  const i = Math.min(Math.floor(state.pos), fr.length - 1);
  const t = state.pos - i;
  const a = fr[i], b = fr[i + 1];
  if (a?.tex && b?.tex && t > 0.001) gpu.draw(a.tex, b.tex, a.flowTex, t);
  else if (a?.tex && t <= 0.5) gpu.draw(a.tex);
  else if (b?.tex) gpu.draw(b.tex);
  else gpu.draw(a?.tex ?? null);
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

// ---------- loading ----------

function showStatus(msg) {
  $('status').textContent = msg;
  $('status').hidden = !msg;
}

const hhmm = (t) => new Date(t).toLocaleTimeString('nl-NL', { hour: '2-digit', minute: '2-digit' });

async function loadObserved() {
  const pt = state.point;
  if (!pt || !state.t0) return;
  try {
    const series = await observedSeries(pt, state.t0 - PAST * STEP, state.t0);
    if (state.point === pt) { state.observed = series; render(); }
  } catch (e) {
    console.warn('observed series failed', e);
  }
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
  const slow = Date.now() - loading.started > 12e3 ? ' · KNMI is slow' : '';
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
  state.lastCheck = Date.now();
  loading.error = '';
  let obs, fc;
  try {
    if (!state.lut) state.lut = await loadLegend();
    [obs, fc] = await Promise.all([latestTimes(OBS), latestTimes(FC)]);
  } catch (e) {
    return failed(e);
  }
  // Nothing new and nothing missing: done. (Missing frames from a failed load are retried.)
  if (obs.time === state.t0 && fc.ref === state.ref && state.frames.every((f) => f.bytes)) return;

  const t0 = obs.time, ref = fc.ref ?? t0;
  const wanted = [];
  for (let k = PAST; k >= 1; k--) wanted.push({ kind: 'obs', t: t0 - k * STEP });
  wanted.push({ kind: 'obs', t: t0 });
  for (let k = 1; k <= AHEAD; k++) {
    const t = ref + k * STEP;
    if (t > t0) wanted.push({ kind: 'fc', t, ref });
  }

  // Keep frames we already have (observed frames never change). Flows are
  // recomputed, since a frame's neighbour may have changed.
  const key = (f) => `${f.kind}|${f.t}|${f.ref ?? ''}`;
  const have = new Map(state.frames.map((f) => [key(f), f]));
  const next = wanted.map((w) => have.get(key(w)) ?? { ...w });
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
  state.lastCheck = Date.now() - CHECK_EVERY + 60e3; // retry in a minute
  loading.error = 'KNMI isn’t responding · retrying in a minute';
  updateLoading();
  showStatus(e instanceof RateLimited
    ? 'KNMI’s shared radar service is busy right now. Trying again in a minute.'
    : 'KNMI’s radar service didn’t respond. Trying again in a minute.');
}

setInterval(() => {
  if (document.visibilityState === 'visible' && Date.now() - state.lastCheck > CHECK_EVERY) refresh();
}, 15e3);
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && Date.now() - state.lastCheck > CHECK_EVERY) refresh();
});

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
  drawReadout(series);
}

function drawGraph(series) {
  const w = graph.clientWidth || 360, h = graph.clientHeight || 132;
  const top = 14, base = h - 22, plot = base - top;
  graph.setAttribute('viewBox', `0 0 ${w} ${h}`);
  graph.replaceChildren();
  const n = Math.max(series.length, PAST + 1 + AHEAD);
  const slot = w / n, bw = Math.max(2, slot * 0.7);
  const yOf = (v) => base - Math.max(3, Math.min(1, Math.log10(v / 0.1) / Math.log10(200)) * plot);
  const cx = (i) => i * slot + slot / 2;

  for (const [v, label] of [[1, 'moderate'], [5, 'heavy']]) {
    const y = yOf(v);
    el('line', { x1: 0, x2: w, y1: y, y2: y, class: 'grid', 'stroke-dasharray': '2 4' }, graph);
    el('text', { x: w, y: y - 4, 'text-anchor': 'end', class: 'band' }, graph).textContent = label;
  }
  el('line', { x1: 0, x2: w, y1: base, y2: base, class: 'grid' }, graph);

  series.forEach((v, i) => {
    const x = i * slot + (slot - bw) / 2;
    const cls = i < state.nowIdx ? 'past' : '';
    if (v === null) {
      // Negative delay keeps the pulse in phase across redraws.
      el('rect', { x, y: base - 3, width: bw, height: 3, class: 'pending', style: `animation-delay:-${performance.now() % 1400}ms` }, graph);
    } else if (v < WET) {
      el('rect', { x, y: base - 2, width: bw, height: 2, class: `dry ${cls}` }, graph);
    } else {
      const y = yOf(v);
      el('rect', { x, y, width: bw, height: base - y, rx: Math.min(2, bw / 3), fill: cssColor(v), class: cls }, graph);
    }
  });

  el('line', { x1: cx(state.nowIdx), x2: cx(state.nowIdx), y1: 2, y2: base, class: 'now' }, graph);
  el('text', { x: cx(state.nowIdx), y: h - 4, 'text-anchor': 'middle', class: 'tick now-label' }, graph).textContent = 'now';
  state.frames.forEach((f, i) => {
    if (Math.abs(i - state.nowIdx) < 4) return;
    if (new Date(f.t).getMinutes() % 30 !== 0) return;
    const anchor = i > n - 3 ? 'end' : i < 2 ? 'start' : 'middle';
    el('text', { x: cx(i), y: h - 4, 'text-anchor': anchor, class: 'tick' }, graph).textContent = hhmm(f.t);
  });

  if (Math.abs(state.pos - state.nowIdx) > 0.01 && state.frames.length) {
    const x = state.pos * slot + slot / 2;
    el('line', { x1: x, x2: x, y1: 2, y2: base, class: 'cursor' }, graph);
    const v = valueAt(series, state.pos);
    el('circle', { cx: x, cy: v >= WET ? yOf(v) : base, r: 3.5, class: 'cursor-dot' }, graph);
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
  const ahead = series.slice(state.nowIdx);
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
  $('readout').textContent = `${when} · ${what}`;
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
startLoading();
locate();
refresh();
