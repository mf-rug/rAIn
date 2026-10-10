"""Fetch KNMI radar files and publish them as small per-frame PNGs.

Runs every 5 minutes (GitHub Actions). Reads the previous output from OUT,
adds the newest observed frame and forecast run, drops frames that fell off
the timeline, and writes index.json. Frames are reprojected to the app's Web
Mercator grid and stored as one byte per pixel: 0 = dry, 1..255 = 0.1..40 mm/h
on a log scale (the same encoding app.js uses).
"""

import io
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import h5py
import numpy as np
from PIL import Image
from pyproj import Transformer

from verify import Verifier

API = "https://api.dataplatform.knmi.nl/open-data/v1/datasets"
OBS = "nl_rdr_data_rtcor_5m/versions/1.0"      # gauge-corrected 5-min rain, real time
FC = "radar_forecast/versions/2.0"             # 2-hour nowcast from the same product line
# KNMI's published key for unregistered use (shared quota); a registered key in
# the KNMI_API_KEY secret takes precedence.
ANON_KEY = "eyJvcmciOiI1ZTU1NGUxOTI3NGE5NjAwMDEyYTNlYjEiLCJpZCI6IjUzYTg1ZDBhMmQ5YzRkYzJiYWNlNzQ4NTQ2Zjk4ODExIiwiaCI6Im11cm11cjEyOCJ9"
KEY = os.environ.get("KNMI_API_KEY") or ANON_KEY

# Must match app.js.
X0, Y0, X1, Y1 = 0, 6257115.396264, 1208534.924976, 7553162.131589
W, H = 700, 751
PAST = 12
STEP = timedelta(minutes=5)

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "out")


def api(path):
    for attempt in range(4):
        req = urllib.request.Request(f"{API}/{path}", headers={"Authorization": KEY})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == 3:
                raise
            time.sleep(15)


def download(dataset, name):
    url = api(f"{dataset}/files/{name}/url")["temporaryDownloadUrl"]
    with urllib.request.urlopen(url, timeout=60) as r:
        return h5py.File(io.BytesIO(r.read()))


def latest(dataset):
    files = api(f"{dataset}/files?maxKeys=1&orderBy=created&sorting=desc")["files"]
    return files[0]["filename"]


def stamp(name):
    return datetime.strptime(name.rsplit("_", 1)[1][:12], "%Y%m%d%H%M").replace(tzinfo=timezone.utc)


def iso(t):
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


# Nearest-neighbour lookup from each Mercator pixel centre into KNMI's polar
# stereographic 1 km grid (700 × 765, top-left at y = -3650 km).
def build_lookup():
    to_stere = Transformer.from_crs(
        "EPSG:3857",
        "+proj=stere +lat_0=90 +lon_0=0 +lat_ts=60 +a=6378137 +b=6356752 +x_0=0 +y_0=0 +units=km",
        always_xy=True,
    )
    gx, gy = np.meshgrid(X0 + (np.arange(W) + 0.5) * (X1 - X0) / W, Y1 - (np.arange(H) + 0.5) * (Y1 - Y0) / H)
    sx, sy = to_stere.transform(gx, gy)
    rows = np.floor(-sy - 3650).astype(int)
    cols = np.floor(sx).astype(int)
    inside = (rows >= 0) & (rows < 765) & (cols >= 0) & (cols < 700)
    return np.where(inside, rows, 0), np.where(inside, cols, 0), inside


LOOKUP = None
STERE = "+proj=stere +lat_0=90 +lon_0=0 +lat_ts=60 +a=6378137 +b=6356752 +x_0=0 +y_0=0 +units=km"
COVERAGE = None   # outline of the area the radars actually see, from the latest observation


def coverage_outline(valid):
    """Outline of the valid pixels of a KNMI grid as [lat, lon] points.

    The covered area is the union of the radars' ranges clipped to the grid,
    so every row is one interval: trace the left edges down, the right edges up.
    """
    rows = np.flatnonzero(valid.any(axis=1))
    if not len(rows):
        return None
    picks = sorted(set(rows[::4].tolist()) | {int(rows[0]), int(rows[-1])})
    left, right = [], []
    for r in picks:
        cols = np.flatnonzero(valid[r])
        left.append((cols[0], r))
        right.append((cols[-1] + 1, r))
    pts = left + right[::-1]
    to_ll = Transformer.from_crs(STERE, "EPSG:4326", always_xy=True)
    lon, lat = to_ll.transform([x for x, _ in pts], [-3650 - r for _, r in pts])
    return [[round(a, 4), round(o, 4)] for a, o in zip(lat, lon)]


def encode(raw):
    """KNMI image (mm per 5 min × 100, uint16) → Mercator byte grid."""
    global LOOKUP
    LOOKUP = LOOKUP or build_lookup()
    rows, cols, inside = LOOKUP
    pv = raw[rows, cols].astype(np.float64)
    mmh = np.where(inside & (pv < 65534), pv * 0.01 * 12, 0.0)
    out = np.zeros(mmh.shape, np.uint8)
    wet = mmh >= 0.1
    out[wet] = np.clip(1 + np.round((np.log10(mmh[wet]) + 1) / math.log10(400) * 254), 1, 255)
    return out


def save(grid, name):
    Image.fromarray(grid, "L").save(OUT / name, optimize=True)


def fetch_obs(t):
    """Observed frame at t as a byte grid, from our own PNG if we still have it."""
    png = OUT / f"obs-{t:%Y%m%d%H%M}.png"
    if png.exists():
        return np.array(Image.open(png))
    global COVERAGE
    try:
        f = download(OBS, f"RAD_NL25_RAC_RT_{t:%Y%m%d%H%M}.h5")
    except urllib.error.HTTPError as e:
        if e.code in (403, 404):
            raise FileNotFoundError(t) from e
        raise
    raw = f["image1/image_data"][:]
    COVERAGE = COVERAGE or coverage_outline(raw < 65534)
    return encode(raw)


def fetch_run(ref):
    """Byte grids for a forecast run's leads 1..24 (+5 … +120 min)."""
    try:
        f = download(FC, f"RAD_NL25_RAC_FM_{ref:%Y%m%d%H%M}.h5")
    except urllib.error.HTTPError as e:
        if e.code in (403, 404):
            raise FileNotFoundError(ref) from e
        raise
    return [encode(f[f"image{i}/image_data"][:]) for i in range(2, 26)]


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    old = {}
    if (OUT / "index.json").exists():
        old = json.loads((OUT / "index.json").read_text())
    verifier = Verifier(OUT, fetch_obs, fetch_run)

    # Newest observed frame; KNMI publishes ~2 min after the nominal time. If
    # the next frame is due but not out yet, wait for it (up to 2 min). If it
    # isn't due yet (e.g. a duplicate trigger right after a run), stop here.
    t0 = stamp(latest(OBS))
    if old.get("t0") == iso(t0):
        due = t0 + STEP + timedelta(minutes=1)
        if datetime.now(timezone.utc) < due:
            return finish(False, f"nothing new before {due:%H:%M}; t0 {iso(t0)}")
        deadline = time.time() + 120
        while old.get("t0") == iso(t0) and time.time() < deadline:
            time.sleep(20)
            t0 = stamp(latest(OBS))
    obs = []
    for k in range(PAST, -1, -1):
        t = t0 - k * STEP
        name = f"obs-{t:%Y%m%d%H%M}.png"
        if not (OUT / name).exists():
            try:
                grid = fetch_obs(t)
            except FileNotFoundError:
                print("obs missing", name)
                continue
            save(grid, name)
            verifier.add_obs(t, grid)
            print("obs", name)
        obs.append({"t": iso(t), "file": name})

    # Newest forecast run: image1 is the analysis at the run time, 2..25 are
    # +5..+120 min. image1 is kept because a run can be newer than the latest
    # observed frame, and it fills that slot.
    fc_name = latest(FC)
    ref = stamp(fc_name)
    fc = []
    if old.get("ref") == iso(ref) and all((OUT / e["file"]).exists() for e in old.get("fc", [])):
        fc = old["fc"]
    else:
        f = download(FC, fc_name)
        grids = [encode(f[f"image{i}/image_data"][:]) for i in range(1, 26)]
        for i, grid in enumerate(grids):
            t = ref + i * STEP
            name = f"fc-{ref:%Y%m%d%H%M}-{t:%Y%m%d%H%M}.png"
            save(grid, name)
            fc.append({"t": iso(t), "file": name})
        verifier.add_run(ref, grids[1:])
        print("forecast run", iso(ref))

    changed = old.get("t0") != iso(t0) or old.get("ref") != iso(ref) or len(obs) != len(old.get("obs", []))
    if not changed:
        return finish(False, f"nothing new; t0 {iso(t0)} ref {iso(ref)}")

    if COVERAGE:
        (OUT / "coverage.json").write_text(json.dumps(COVERAGE, separators=(",", ":")))

    index = {"generated": iso(datetime.now(timezone.utc)), "t0": iso(t0), "ref": iso(ref), "obs": obs, "fc": fc}
    (OUT / "index.json").write_text(json.dumps(index, separators=(",", ":")))

    # How well recent forecasts held up (needs the frames above, before pruning).
    scored, pending = verifier.update(t0)
    verifier.save()
    skill = verifier.summary(t0)
    if skill:
        (OUT / "skill.json").write_text(json.dumps(skill, separators=(",", ":")))

    keep = {"index.json"} | {e["file"] for e in obs + fc}
    for p in OUT.glob("*.png"):
        if p.name not in keep:
            p.unlink()
    finish(True, f"t0 {index['t0']} ref {index['ref']} frames {len(obs) + len(fc)}; "
                 f"verified {scored} new times, {pending} pending, {verifier.budget} downloads left")


def finish(changed, msg):
    print(msg)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as gh:
            gh.write(f"changed={'true' if changed else 'false'}\n")


if __name__ == "__main__":
    try:
        main()
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
        # KNMI rate limit or outage: skip this round rather than fail (and
        # email); the next run, 5 minutes later, catches up. Bugs still fail.
        if isinstance(e, urllib.error.HTTPError) and e.code not in (429, 500, 502, 503, 504):
            raise
        finish(False, f"KNMI unavailable ({e}); skipping this round")
