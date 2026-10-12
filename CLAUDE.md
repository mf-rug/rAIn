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
  dry → stayed dry), each with its own scale (`SCALE` in app.js). Check claims
  against compare.json (point-based) when in doubt.
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

## Conventions

- Commit messages: imperative summary, a short body explaining why.
- Before committing: scan the diff for local paths, emails, keys
  (the anonymous KNMI key in build.py is public by design).
- UI copy: plain, specific, sentence case; say what's uncertain.
