# rAIn — notes for coding sessions

Static rain radar web app (Netherlands) on open data. See README.md for what it
does and the data sources. This file is about how it's built and what bites.

## Layout

| Path | What |
|---|---|
| `index.html`, `style.css`, `app.js` | The whole app. Plain JS, no build step, Leaflet 1.9.4 from unpkg. |
| `job/build.py` | Scheduled job: KNMI frames, index.json, coverage, calls the modules below. |
| `job/verify.py` | Forecast reliability (CSI at ~4 km, per ~40 km tile, last 6 h) → skill.json. |
| `job/compare.py` | KNMI vs Buienradar vs DWD (+ consensus) at 32 places → compare.json. |
| `job/dwd.py` | DWD RADVOR nowcast: NL-window images (graph line) and full-area map frames. |
| `job/mix.py` | KNMI + DWD merged: the default timeline (graph, headline, reliability, map "Both"). |
| `job/sky.py` | EUMETSAT satellite images (map "Clouds"), KNMI stations' sunshine (graph strip), model sun-forecast scores → sky.json. |
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
  = union outline. The app uses `mix` as its timeline when present; the map
  toggle Both | KNMI | DWD changes only the map. verify.py scores the merged
  forecast (backfilled runs are still KNMI-only).
- **Sky files:** `sat-*.jpg` (EUMETSAT GeoColour, radar grid's bbox, 1000×1072,
  every 10 min; listed in `index.json` → `sat`, entries with `gap` have no
  image), `sun.json` (stations' 10-min sunshine minutes, cloud okta, weather
  code for the last hour), `sky.json` (sun-forecast scores per model).
- **Job state** lives in `state/` on the `data` branch (run/obs coarse counts,
  scores, compare snapshots, coverage mask), not deployed.

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
- **Big images on iOS:** keep single images well under ~16 Mpx (that's why DWD map
  frames are one file per step, not one atlas).
- **EUMETSAT's WMS snaps any time to the nearest image** (`nearestValue="1"`,
  no warning header) and returns an old default without a time. The newest
  real time comes from the layer's own capabilities document; images arrive
  ~25–45 min late; copies of a neighbour are kept as gaps. Per time: the
  radar area in detail plus coarser tiers of wider areas (`sky.TIERS`:
  `sat-wide-*` to ~10°W–25°E, `sat-outer-*` to ~25°W–40°E; bboxes in
  `index.json` → `sat_tiers`). The app loads a tier only when the map view
  reaches beyond the finer one.
- **Cloud motion:** `sat-flow-{tier}-{t}.png` (per image, motion to the next
  one) and `sat-motion-{tier}-{t}.png` (mean of the last two, for drifting
  the newest image forward; `index.json` → `sat_motion`): 32×32 RGBA, R/G =
  128 + 4 × pixels per 10 min. The app warps between images and traces the
  drift back in 10-minute steps (a straight jump smears). A uniform cloud
  deck has no texture for optical flow: only its edges moved and drifted
  fronts got squashed along the wind. Hence CLAHE before the flow and
  texture-weighted filling (`FLOW_FILL`); bump `FLOW_VERSION` when the method
  changes so all fields are recomputed (file names carry it). Throttled at 20
  requests/min, which is why the job fetches, not the app.
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
- **Repository secret `KNMI_API_KEY`:** registered KNMI Open Data key (1000
  requests/hour). Without it the job uses KNMI's shared anonymous key (published
  by KNMI, valid until 2027-08-01), which is often rate limited.
- **Pages** source is "GitHub Actions". The deploy step retries once on GitHub's
  transient "no artifact found" error.
- GitHub disables scheduled workflows after 60 days without commits; the
  cron-job.org trigger is unaffected.

## Status and open threads (as of 2026-10-12)

- **Merged is the default since 2026-10-13** (user's call, before the
  benchmark is in). Known cost: averaging spreads rain out (wider, weaker,
  earlier starts where the two disagree; 0.3 + 0 → 0.15 = the rain threshold).
  The benchmark below decides whether to keep it.
- **Consensus forecasts** (`mean2` = KNMI+DWD, `mean3` = all three) are being
  scored in compare.py's `east` set since 2026-10-12. After a few rainy days,
  compare them with KNMI alone (compare.json → `east`). If one clearly wins,
  consider letting it drive the graph/verdict for eastern locations, and/or show
  source agreement as a "how sure right now" signal.
- **First head-to-head (3 days, 2026-10-09…12):** KNMI best up to ~30 min (part
  head start: scored against its own observations); Buienradar too wet (bias
  ~1.2–1.3) but beats KNMI beyond ~90 min as KNMI's smoothing dries out; DWD
  (east only) slightly better than KNMI from ~1 h on.
- **Reliability** is now rain/dry split with dry-by-distance classes (rebuilt
  2026-10-12). The headline and the reliability line share `turnOf()`. Thresholds
  (`SCALE`, `DRY_BINS`, "held up" = 0.6 rain) are educated guesses; an offline
  test on KNMI's archive (forecasts since 2024-08) could tune them.
- **Touch scrubbing on iOS** was fixed with raw touch events (2026-10-11); the
  user is monitoring whether it stays fixed.
- Unanswered: the user asked to offset "sp19 and 43" on the x axis — unclear
  what that refers to (possibly not this project).
- GitHub had an Actions incident on 2026-10-12 (push to `data` rejected with
  "fatal error in commit_refs"); the store step now retries 3×.

- **Sky (2026-10-13):** satellite "Clouds" layer and the measured-sunshine
  strip are live. The job scores 5 models' hourly sun forecasts (and
  persistence) against the stations in `sky.json`. After a week, check which
  (if any) beats persistence at 1–3 h and on whole days; only then add a
  forecast sun band or a day description ("showers with sunny spells" vs
  "grey, on and off drizzle"). The KNMI 10-min station data also carries
  weather codes (ww) that separate showers from drizzle.

## Conventions

- Commit messages: imperative summary, a short body explaining why.
- Before committing: scan the diff for local paths, emails, keys
  (the anonymous KNMI key in build.py is public by design).
- UI copy: plain, specific, sentence case; say what's uncertain.
