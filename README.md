# rAIn

A rain radar and two-hour rain forecast for the Netherlands, built on open data.
No ads, no cookies, no tracking. A single static web page you can add to a phone's
home screen.

**Live:** https://mf-rug.github.io/rAIn/ (add `?demo` to see the forecast display
with fake showery rain)

## What it shows

- **The answer first:** "Dry until 18:20", "Dry from 17:45", plus intensity and duration.
- **Rain graph** for your location: the past hour (measured) and the next two
  hours (forecast), as a curve coloured by intensity. Drag across it to move the
  map through time; the map animates smoothly between radar frames.
- **Reliability:** how far ahead today's forecasts actually held up near you,
  shown as a sentence, a fading strip under the graph, a fading curve and map,
  and a label while scrubbing ("high / moderate / low / very low reliability").
- **KNMI and DWD merged:** the forecast is the average of KNMI's and DWD's
  nowcasts where both cover (one source alone elsewhere, blended at the edges);
  the past hour is measured. While scrubbing, both components are shown.
- **Comparison line:** Buienradar's forecast for the same spot.
- **Head-to-head scores:** how often KNMI, Buienradar and DWD correctly forecast
  rain one hour ahead, over the last days.
- **Sky:** measured sunshine over the past hour (nearest KNMI station) along the
  top of the graph, and a "Clouds" layer on the map: where it's cloudy, from
  satellite (EUMETSAT's cloud mask from Meteosat Third Generation and
  infrared, every 10 minutes, 10–20 minutes behind), moving with the wind into the forecast. The difference
  between a grey drizzly day and sun between showers, which radar can't show.
- **Sun ahead:** ECMWF's sun forecast (via Open-Meteo) as a paler strip after
  "now" and a line for the rest of the daylight.
- **Drizzle the radar misses:** KNMI stations' weather reports (drizzle, rain)
  as dots on the map and marks on the graph; near a drizzling station the
  headline says so, with "drizzle possible" until the clouds clear.
- **Map:** the merged radar by default; switch to KNMI alone or DWD alone (all
  of Germany and surroundings).
  Areas without radar coverage are hatched; your country is subtly highlighted.

## How it works

```
KNMI open data ─┐                    every 5 min (cron-job.org → GitHub Actions)
DWD open data ──┼─► job/build.py ──► frames (PNG), index.json, skill.json,
Buienradar ─────┘                    compare.json, coverage.json
                                          │  deployed with the app to GitHub Pages
                                          ▼
                              index.html + app.js (static, in the browser)
```

- **The job** (`job/`, run by `.github/workflows/radar.yml`) downloads KNMI's raw
  radar files, reprojects each 5-minute frame onto the app's map grid, and stores
  it as a small greyscale PNG (one byte per pixel: rain on a log scale). It also
  scores recent forecasts (`verify.py`), logs and scores Buienradar and DWD
  against KNMI's observations (`compare.py`), and prepares DWD's nowcast
  (`dwd.py`). Each run deploys the app plus the data as the GitHub Pages site.
  Between runs, the job keeps its state on the `data` branch (one commit,
  replaced each time).
- **The app** (`app.js`) loads the frames, decodes them on the GPU (WebGL),
  estimates motion between frames and draws in-between moments, so playback
  and scrubbing are continuous. If the job's data is older than 20 minutes, the
  app falls back to KNMI's public map service directly (slower, throttled).
- **Caching:** frames are immutable and kept in the browser's Cache API. Since
  KNMI publishes about 4–5 minutes after each radar time, a reload before new
  data can exist makes no network requests at all.

## Data sources and licences

| Source | Used for | Licence / terms |
|---|---|---|
| [KNMI Data Platform](https://dataplatform.knmi.nl) | Radar observations (`nl_rdr_data_rtcor_5m`), 2-hour nowcast (`radar_forecast` v2.0, pySTEPS); WMS as fallback | CC BY 4.0 |
| [DWD Open Data](https://opendata.dwd.de/weather/radar/composite/rv/) | RADVOR nowcast (`composite_rv`) for the comparison line and the DWD map | CC BY 4.0 |
| [Buienradar](https://www.buienradar.nl) | Point forecast for the comparison line and scoring | Free use with credit and a link |
| [EUMETSAT](https://view.eumetsat.int) | Cloud mask (MTG, from the Data Store; free account) and infrared (MTG, WMS) for the cloud layer | Core data, CC BY 4.0: "Contains modified EUMETSAT Meteosat data" |
| [KNMI Data Platform](https://dataplatform.knmi.nl) | 10-minute station observations (sunshine) | CC BY 4.0 |
| [Open-Meteo](https://open-meteo.com) | Weather models' sun forecasts, scored by the job (not shown yet) | CC BY 4.0 |
| [Natural Earth](https://www.naturalearthdata.com) | Country highlight, borders and coasts | Public domain |
| [OpenFreeMap](https://openfreemap.org), © OpenMapTiles, © OpenStreetMap contributors | Basemap (vector tiles, our own colours: land, water, labels; no roads) | Free, no key; attribution |
| Esri, HERE, Garmin, © OpenStreetMap contributors | Fallback basemap when MapLibre can't run (Esri World Gray Canvas) | Esri basemap terms |

## Setup (for a fork)

1. **GitHub Pages:** Settings → Pages → Source: *GitHub Actions*.
2. **KNMI API key** (free, recommended; otherwise the job shares KNMI's anonymous
   quota): register at https://developer.dataplatform.knmi.nl, request an *Open
   Data API* key, and add it as the repository secret `KNMI_API_KEY`.
3. **Punctual runs:** GitHub's own schedule often starts late, so the workflow is
   triggered every 5 minutes by [cron-job.org](https://cron-job.org):
   - a fine-grained GitHub token limited to this repository, permission
     *Actions: Read and write* (max. 1 year validity);
   - cron job: `POST https://api.github.com/repos/<owner>/<repo>/actions/workflows/radar.yml/dispatches`,
     schedule `3,8,13,18,23,28,33,38,43,48,53,58 * * * *`, body `{"ref":"main"}`,
     headers `Authorization: Bearer <token>`, `Accept: application/vnd.github+json`,
     `X-GitHub-Api-Version: 2022-11-28`, `Content-Type: application/json`;
     a successful test run returns HTTP 204.
4. Paths in `app.js` (`DATA` for local development) assume the site lives at
   `mf-rug.github.io/rAIn`; adjust for a fork.

## Local development

```bash
python3 -m http.server 8000      # then open http://localhost:8000
```

On localhost the app reads the live site's data, so only the app files are local.
The job can be run locally with `python3 job/build.py <outdir>` (needs `h5py`,
`numpy`, `pyproj`, `pillow`, `shapely`).
