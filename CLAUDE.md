# rAIn — notes for coding sessions

Static rain radar web app (Netherlands) on open data. See README.md for what it
does and the data sources. This file is about how it's built and what bites.

## Layout

| Path | What |
|---|---|
| `index.html`, `style.css`, `app.js` | The whole app. Plain JS, no build step, Leaflet 1.9.4 from unpkg; basemap via MapLibre 5.24 + maplibre-gl-leaflet. |
| `job/build.py` | Scheduled job: KNMI frames, index.json, coverage, calls the modules below. |
| `job/verify.py` | Forecast reliability (CSI at ~4 km, per ~40 km tile, last 6 h) → skill.json. |
| `job/compare.py` | KNMI vs Buienradar vs DWD (+ consensus) at 32 places → compare.json. |
| `job/dwd.py` | DWD RADVOR nowcast: NL-window images (graph line) and full-area map frames. |
| `job/mix.py` | KNMI + DWD merged: the default timeline (graph, headline, reliability, map "Both"). |
| `job/sky.py` | Cloud field from satellite (map "Clouds"), KNMI stations' sunshine (graph strip), model sun-forecast scores → sky.json. |
| `.github/workflows/radar.yml` | Runs the job, stores state on the `data` branch, deploys app + data to Pages. |
| `countries.geojson`, `borders.geojson` | Natural Earth, pre-cut to the radar areas. |
| `icon-source.svg` | Source of `icon-*.png` (letters are text in Bricolage Grotesque; render in a browser). |

## Data flow and formats

- **Grid:** KNMI frames use a Web Mercator grid, bbox `X0,Y0,X1,Y1` = KNMI's WMS
  domain, `W×H = 700×751` (~1.06 km on the ground at NL latitudes). These
  constants exist in `app.js`, `build.py`, `compare.py` and `dwd.py` and **must
  stay identical**.
- **Byte encoding (everywhere):** 0 = dry, 1..255 = 0.1..40 mm/h on a log scale
  (`toByte` / `fromByte` in app.js). DWD images reserve 255 for "no data" (rain
  capped at 254).
- **KNMI products:** observations `nl_rdr_data_rtcor_5m` (gauge-corrected,
  mm per 5 min × 100, `image1`), forecast `radar_forecast` v2.0 (pySTEPS, images
  1..25 = +0..+120 min). Both: polar stereographic 700×765 km grid, row offset
  3650, `65534/65535` = missing/outside (forecast files never mark missing).
- **Published files** (Pages, under `data/`): `index.json` (frame list, t0, ref,
  `dwd` block), `obs-*.png`, `fc-REF-T.png`, `skill.json`, `compare.json`,
  `coverage.json`, DWD files (`dwd-fc-*`, `dwd-obs-*` = NL window; `dwd-map-*` =
  full DWD area on its own 1113×1043 grid).
- **Merged timeline (`index.json` → `mix`, since 2026-10-13):** `mix-obs-*`
  (KNMI's observation where KNMI covers, DWD's analysis outside; not averaged)
  and `mix-fc-{knmiref}-{dwdref|none}-{t}` (mean mm/h where both cover, one
  source alone elsewhere, weights ramped over ~20 km at each coverage edge;
  DWD left out when its run is >15 min older than KNMI's), on KNMI's grid.
  `knmi-win-*` = KNMI's forecast in the NL window for the readout; `coverage`
  = union outline over both areas. On the map, "Both" draws the merged frames
  plus DWD's full-area frames around them (the DWD renderer leaves KNMI's
  grid transparent: `setHole`). The app uses `mix` as its timeline when present; the map
  toggle Both | KNMI | DWD changes only the map. verify.py scores the merged
  forecast (backfilled runs are still KNMI-only).
- **Sky files:** `cloud-{t}.webp` (cloud field 0..255, one grid ~25°W–40°E /
  34–66°N, 1200×969, every 10 min; 15 with the WMS fallback), `cloud-flow-{t}-{next}.png` (motion to the
  next field) and `cloud-motion-{t}.png` (mean of the last two, for the drift);
  `index.json` → `clouds` {bbox, frames, motion}. `sun.json` (stations' 10-min
  sunshine minutes, cloud okta, weather code for the last hour; `cloud`: the
  drifted cloud field over stations reporting drizzle/rain, +0..+120 min),
  `sky.json` (sun-forecast scores per model).
- **Job state** lives in `state/` on the `data` branch (run/obs coarse counts,
  scores, compare snapshots, coverage mask; `wet-log.json`: per station every
  10 min [weather code, merged radar byte (max ~3 km), cloud field byte], 14
  days, for checking the drizzle feature), not deployed.

## Things that bit before (don't repeat)

- **Raw radar forecasts extend rain beyond coverage** (KNMI and DWD alike). The
  job clips forecasts to the latest observation's coverage (`clip()` in build.py,
  end of `dwd.fetch()`).
- **KNMI WMS GetFeatureInfo is wrong for forecast time series** (range queries
  repeat a bogus 0.036). It is exact for observations. The WMS fallback reads
  forecast values back from rendered frames via the legend colours.
- **raw.githubusercontent.com serves a stale index for minutes** after a push
  → data is deployed with Pages instead (deploys purge the CDN).
- **Buienradar's RainHistoryForecast starts with ~3 observed steps**, then a gap,
  then the forecast. Its first step is not its issue time. compare.py anchors all
  sources at the same moment F (snapshot) instead.
- **Reliability, twice wrong before:** FSS over 13 km neighbourhoods was far too
  generous (0.68 at 1 h vs 31–40% right at the spot); then a single CSI was too
  harsh on dry, calm days (it ignores correct dry forecasts). Now two per-mille
  scores per lead and tile: *rain* (forecast rain → rained) and *dry* (forecast
  dry → stayed dry), each with its own scale (`SCALE` in app.js). Dry is also
  scored nationally by distance to the nearest forecast rain (`DRY_BINS`), and
  the app picks the class per frame (`dryBinAt`): a dry gap between showers is
  much less certain than a spot with no rain around. Check claims against
  compare.json (point-based) when in doubt.
- **iOS touch:** shapes inside `#graph` are recreated on every move, so they have
  `pointer-events: none`, and touch scrubbing uses raw touch events with
  `preventDefault` (iOS otherwise takes the gesture over as a scroll).
  `touch-action` on SVG isn't reliable in Safari.
- **iOS fires gesture events next to touch events** — the Safari pinch handler is
  only attached when `navigator.maxTouchPoints === 0`.
- **Trackpad pinch** = ctrl+wheel (Chrome/Firefox) or gesture events (Safari);
  handled directly, proportional to the pinch. Leaflet's wheel zoom loses the speed.
- **The graph's grid ends at ~10.9°E** (KNMI's, which the merged timeline
  uses), while "Both" on the map reaches DWD's whole area. Points beyond it
  (e.g. Berlin) read DWD's full-area frames (`offGrid`, `loadDwdPoint`);
  their forecast ends where DWD's run does (`aheadOf`). Fixed 2026-10-13:
  before, the graph showed dry there while the map showed rain.
- **Never read data back from a canvas.** Safari (on by default since iOS 26)
  adds noise to canvas readback (getImageData) against fingerprinting: DWD's
  "no data" (255) came back as 254 etc. and showed as speckled rain in the
  hatched area, on phones only. Data PNGs are decoded in JS (`pngBytes`:
  8-bit, no palette, DecompressionStream); the canvas path is only a fallback
  (and for WMS legend/frames). Textures straight from images are fine.
- **Basemap = OpenFreeMap vector tiles in our own colours** (since 2026-10-16;
  `MAP_COLORS` in app.js: light "sage", dark "warm"), because Esri's grey map
  and grey clouds blended together. Roads, railways, buildings and the style's
  own borders are dropped (`MAP_DROP`); our Natural Earth borders and the home
  veil stay. Two MapLibre maps via maplibre-gl-leaflet: land/water in the tile
  pane, labels only in the labels pane (over clouds and radar). MapLibre 6 has
  no UMD build, hence 5.x. Falls back to Esri raster tiles if MapLibre fails.
- **Big images on iOS:** keep single images well under ~16 Mpx (that's why DWD map
  frames are one file per step, not one atlas).
- **Clouds are drawn from a cloud field, not the satellite photo** (rebuilt
  2026-10-13). Animating GeoColour moved clouds and ground together (city
  lights and coasts dragged, low clouds stuck, seams between image tiers), and
  every fix was a heuristic with failure cases, by day too. Now: a cloud
  mask (catches low cloud/fog that infrared misses: 137 vs 75 of 184
  overcast station-times) × infrared shading (`mtg_fd:ir105_hrfi`, grey
  52→70) on one grid; the app colours it (grey low → white high, per theme).
  Drift beats frozen only modestly
  (60 min: 10.2% vs 10.9% wrong), as much change is cloud forming/dissolving.
- **The mask is MTG's own (FCI-2-CLM) from EUMETSAT's Data Store** (since
  2026-10-13; secrets `EUMETSAT_KEY`/`EUMETSAT_SECRET`), every 10 min,
  published ~10 min after its slot; frames are labelled with the slot's end
  (NL is scanned near the end). Station check, 679 station-times on
  2026-10-13: correlation 0.82 vs 0.73 for MSG's WMS mask (`msg_fes:clm`) and
  0.69 for MSG Rapid Scan's; night: 42/73 overcast caught, 8/132 clear called
  cloudy (MSG 38, 16). Classes: 2 = partial/thin (density 0.8), 3 = opaque;
  opaque alone misses half the overcast. **The file's x attributes are wrong
  in sign:** columns run west → east (checked on the day/night boundary);
  reading them as stated gave correlation ~0. The WMS still lags the Data
  Store by ~20 min, so the infrared (WMS only) is the newest available at
  first and the field is redone when its own arrives (`ir` in the entry).
  Without the secrets, or when the Data Store fails, the job falls back to
  MSG's WMS mask (15 min, ~25–45 min late).
- **EUMETSAT's WMS snaps any time to the nearest image** (`nearestValue="1"`,
  no warning header) and returns an old default without a time: the newest
  real time comes from the layer's own capabilities; a frame whose mask equals
  its neighbour's (hash `m`) is a gap. WMS images arrive ~25–45 min late; the
  service is throttled at 20 requests/min (the job fetches, not the app).
- **Cloud motion:** optical flow needs texture; a uniform deck only moves at
  its edges (fronts got squashed), hence CLAHE first and texture-weighted
  filling. The app traces the drift back in 10-minute steps (a straight jump
  smears).
- **Radar misses drizzle from low cloud** (below the beam, drops too small).
  KNMI stations' weather code (WaWa 50–58) catches it: 2026-10-18 Eelde
  reported drizzle 59 of 60 min while radar was 0 around four stations.
  Checked that day (821 station-times vs the MTG mask): drizzle under opaque
  cloud 94%, clear sky had drizzle 1/81 (so clearing ends it), but opaque
  cloud drizzled only 18% and infrared doesn't separate it; nearest station
  (~33 km) agreed on drizzle only 20% (patchy: no filled-in map); still wet
  60 min later 58% vs 23% baseline. So the app shows station dots (past hour
  to now; dry ones as rings), strip marks, "Drizzle now" when a station ≤20 km (`WET_KM`)
  reports it and the radar is dry here, and "drizzle possible" until the
  drifted cloud over that station drops below `CLEAR_BELOW`. WaWa 22
  ("drizzle in the past hour, not now") counts as drizzle "on and off"
  (paler): on 2026-10-18 Eelde's sensor showed such gaps when drops got too
  fine to detect, while it felt like being inside the cloud all day.
- **Weather models' sun at a point was poor on the first check** (2026-10-13:
  HARMONIE missed a sunny afternoon in the south, correlation ~0 with the
  stations). No model sun is shown until sky.json's scores say one beats
  persistence.
- **Local Python:** shapely + numpy 2 clash in the system Python (MultiPolygon
  creation fails). Use a fresh venv for job code that uses shapely.
- **`job/__pycache__/` once got committed** with absolute local paths. It's in
  .gitignore; check `git status` before committing.
- **Old state formats:** when a stored format changes, the loader must detect and
  discard the old one (see `Verifier.__init__`, `Compare.__init__`).

## Verifying changes

- **App:** `python3 -m http.server 8000`; on localhost the app reads the live
  site's data. Headless checks with Playwright (Chromium needs
  `--use-gl=angle --use-angle=swiftshader --enable-unsafe-swiftshader` for WebGL).
  `?demo` gives fake rain and reliability. Touch emulation does not reproduce iOS
  Safari quirks — say so when claiming a touch fix.
- **Job:** `python3 job/build.py <dir>` (KNMI's anonymous key is often rate
  limited locally; DWD needs no key). After pushing, check the next run:
  `gh run list --workflow radar.yml`, then `index.json` / `skill.json` /
  `compare.json` on the live site.
- Don't trust a single run: the job exits early ("nothing new") on duplicate
  triggers, so a fix often only shows on the next run with new data.

## External setup (not in the repo)

- **cron-job.org** triggers `radar.yml` every 5 minutes (`3,8,…,58 * * * *`) with
  a fine-grained GitHub token (Actions read/write on this repo only).
  **The token expires 2027-10-10** — renew it and update the cron job, or runs
  fall back to GitHub's late 15-minute schedule.
- **Repository secrets `EUMETSAT_KEY` / `EUMETSAT_SECRET`:** Data Store API
  key (free EUMETSAT account) for MTG's cloud mask. Locally: `.secrets/eumetsat`
  (key, secret on two lines; gitignored).
- **Repository secret `KNMI_API_KEY`:** registered KNMI Open Data key (1000
  requests/hour). Without it the job uses KNMI's shared anonymous key (published
  by KNMI, valid until 2027-08-01), which is often rate limited.
- **Pages** source is "GitHub Actions". The deploy step retries once on GitHub's
  transient "no artifact found" error.
- GitHub disables scheduled workflows after 60 days without commits; the
  cron-job.org trigger is unaffected.

## Status and open threads (as of 2026-10-21)

- **Benchmarks after ~10 days (checked 2026-10-21; rainy days 10-13, 16, 17,
  18, 20, 21; all scored against KNMI's gauge-corrected radar, which favours
  KNMI and penalises DWD/consensus, so their gains are conservative).** East
  set, CSI (rain ≥0.3 mm/h) at 5–30 / 35–60 / 65–90 / 95–120 min:
  KNMI .67/.45/.35/.28, Buienradar .52/.42/.34/.31 (bias ~1.15, too wet),
  DWD .57/.43/.34/.27, mean2 (KNMI+DWD ≈ the merged default) .66/.47/.37/.30,
  mean3 (all three) .65/.48/.38/.32 (bias ~1.0). Per day: mean2 beat KNMI
  6/6 at 95–120 but only 3/6 at 35–60 (ties); mean3 never worst. → Merged
  default kept. Option raised: mean3 for the point (graph/headline), since
  Buienradar exists only per point (no map).
  **Sun:** ECMWF best (MAE 0.19, sunny-hour CSI .73 at 1–12 h), ICON-D2 next,
  HARMONIE (knmi_seamless) worst (.23/.52); persistence .21/.64 at 1–3 h then
  collapses (.31/.49 at 4–6 h). Caveat: persistence is scored on fewer hours
  (needs the last measured hour) than the models. → A model sun band
  (ECMWF) is defensible beyond ~3 h; offered, not built.
  **Drizzle (wet-log, 3 days, 13.7k station-times):** 46% of drizzle reports
  had radar 0 (WaWa 22: 95%); clear cloud field (<128) had drizzle 4 of 3677;
  drizzling stations still wet 60 min on 74% (dry → wet 6%). "Clearing ends
  it" untested (cloud cleared within 60 min only 9 times). → Feature kept.
  **Clouds vs stations (13 h, 2026-10-21, mostly overcast again):** overcast
  caught 84% by day, 80% at night; only 34 clear samples, so the clear/mixed
  day re-check is still open.
- **Clouds now come from MTG's mask** (see above; switched 2026-10-13, one
  day of station checks, mostly overcast: re-check on a clear and a mixed
  day). Later options (not started): shading from cloud-top height instead
  of the late WMS infrared (MSG CTH in the Data Store: 0.5 MB, ~14 min; MTG's
  is 83 MB per slot); KNMI's MSG-CPP (cloud properties, e.g. optical
  thickness); MSG Rapid Scan's mask (5 min, ~5 min late, 0.12 MB, but
  scored below MTG and MSG 0° on the stations); neighbouring countries' station cloud cover (DWD
  10-min) to correct the satellite at night / validate; model cloud cover as
  forecast beyond the drift (only if the sun scoring shows skill). The app
  drifts the newest field to now ("drifted" label; ~10–20 min with MTG).
  EUMETSAT's WMS sometimes lacks a slot or answers 500; the job retries each run.
- **Merged is the default since 2026-10-13** (user's call; the 2026-10-21
  benchmark supports keeping it, see above). Known cost: averaging spreads rain out (wider, weaker,
  earlier starts where the two disagree; 0.3 + 0 → 0.15 = the rain threshold).
  "Both" on the map = merged
  frames on KNMI's grid plus DWD's frames around them (union of both areas).
- **Consensus forecasts** (`mean2`, `mean3`) are scored in compare.py's
  `east` set since 2026-10-12 (results above).
- **Reliability** is rain/dry split with dry-by-distance classes; verify.py
  scores the merged forecast since 2026-10-13. The fades (strip, curve, map
  radar, clouds) all use one smoothed confidence (`confSeries`, Gaussian 3
  frames); words and sentences use the unsmoothed values. Thresholds (`SCALE`,
  `DRY_BINS`, "held up" = 0.6) are educated guesses; an offline test on KNMI's
  archive (forecasts since 2024-08) could tune them.
- **Sky:** cloud layer, measured-sunshine strip, map label are live; model
  sun scores in `sky.json` (results above). A forecast sun band or a day
  description ("showers with sunny spells" vs "grey, on and off drizzle") is
  the next step if wanted.
- **Drizzle from stations** (since 2026-10-18; first check above). Still
  open: whether clearing cloud ends drizzle (`CLEAR_BELOW`), needs cases
  where the cloud clears over a drizzling station.
- **Touch scrubbing on iOS** was fixed with raw touch events (2026-10-11); the
  user is monitoring whether it stays fixed.
- GitHub had an Actions incident on 2026-10-12 (push to `data` rejected with
  "fatal error in commit_refs"); the store step now retries 3×.

## Conventions

- Commit messages: imperative summary, a short body explaining why.
- Before committing: scan the diff for local paths, emails, keys
  (the anonymous KNMI key in build.py is public by design).
- UI copy: plain, specific, sentence case; say what's uncertain.
