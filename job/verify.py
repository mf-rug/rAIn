"""How well did recent forecasts hold up, per lead time and region?

Each observed frame at time t is compared with what the forecast runs issued
5, 10, … 120 minutes earlier predicted for t, at ~4 km: within each 4 km cell,
wet pixels forecast and observed overlap (hits) or not (misses, false alarms);
the rest of the covered pixels are correct "dry" forecasts.

Two reliabilities are published, because they answer different questions:
  rain: when rain was forecast, how often it rained   = hits / forecast wet
  dry:  when dry was forecast, how often it stayed dry = correct dry / forecast dry
On a day with a few small showers, rain forecasts beyond ~20 min are poor while
dry forecasts are nearly always right; a single score can't say both.

(Earlier versions used the Fractions Skill Score over ~13 km neighbourhoods,
far too optimistic on showery days (0.68 vs 0.31 at the spot at 1 hour), and
then a CSI, which ignores correct dry forecasts and so called dry, calm days
"unreliable".)

Scores are kept per ~40 km tile for the last HOURS and published as
skill.json: for every tile (combined with its neighbours) and lead time, the
recency-weighted rain and dry reliabilities (per mille), or -1 where too little
rain was forecast or fell to say.

State lives in OUT/state (stored on the data branch, not deployed):
  run-YYYYmmddHHMM.npz   coarse wet counts for a forecast run's 24 lead frames
  obs-YYYYmmddHHMM.npz   coarse wet counts for an observed frame
  scores.npz             per scored time: hit, forecast-wet, observed-wet and covered
                         pixel sums per lead and tile
  missing.json           KNMI files that don't exist (so they aren't retried)
"""

import json
import math
import time
from datetime import datetime, timedelta, timezone

import numpy as np

HOURS = 6                 # verification window
HALF_LIFE_H = 2.0         # recency weighting within the window
LEADS = 24                # 5 … 120 minutes
WET_MMH = 0.3             # wet/dry threshold
CELL = 4                  # coarse cell, in frame pixels (~4 km)
TILE = 10                 # tile, in cells (~40 km)
MIN_RAIN = 400.0          # below this (weighted wet pixels) there's too little rain to judge
BUDGET = 20               # KNMI downloads per run for back-filling
TIME_LIMIT = 180          # seconds per run for back-filling; the rest waits for the next run

STEP = timedelta(minutes=5)
WET_BYTE = 1 + round((math.log10(WET_MMH) + 1) / math.log10(400) * 254)


def coarse(grid):
    """Byte grid (H × W) → wet-pixel counts per CELL × CELL block."""
    h, w = (grid.shape[0] // CELL) * CELL, (grid.shape[1] // CELL) * CELL
    wet = grid[:h, :w] >= WET_BYTE
    return wet.reshape(h // CELL, CELL, w // CELL, CELL).sum(axis=(1, 3)).astype(np.uint8)


def tile_sums(a):
    """Sum a cell grid into TILE × TILE tiles (edge tiles may be partial)."""
    ny, nx = math.ceil(a.shape[0] / TILE), math.ceil(a.shape[1] / TILE)
    padded = np.zeros((ny * TILE, nx * TILE), a.dtype)
    padded[:a.shape[0], :a.shape[1]] = a
    return padded.reshape(ny, TILE, nx, TILE).sum(axis=(1, 3))


def stamp(t):
    return t.strftime("%Y%m%d%H%M")


class Verifier:
    def __init__(self, out, fetch_obs, fetch_run):
        """fetch_obs(t) → byte grid; fetch_run(ref) → list of 24 byte grids (leads 1..24).
        Either may raise FileNotFoundError when KNMI has no such file."""
        self.dir = out / "state"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.fetch_obs, self.fetch_run = fetch_obs, fetch_run
        self.valid = None    # covered pixels per 4 km cell (set by build.py); None: all
        self.budget = BUDGET
        self.deadline = time.time() + TIME_LIMIT
        mf = self.dir / "missing.json"
        self.missing = set(json.loads(mf.read_text())) if mf.exists() else set()
        sf = self.dir / "scores.npz"
        z = np.load(sf) if sf.exists() else None
        self.keys = ("hit", "fwet", "owet", "cover")
        if z is not None and all(k in z for k in self.keys):
            self.times = list(z["times"])
            self.sums = {k: list(z[k]) for k in self.keys}
        else:  # none yet, or an earlier format: rebuild from scratch
            self.times, self.sums = [], {k: [] for k in self.keys}

    # -- inputs, from state or KNMI (within the download budget) --

    def add_obs(self, t, grid):
        np.savez_compressed(self.dir / f"obs-{stamp(t)}.npz", c=coarse(grid))

    def add_run(self, ref, grids):
        np.savez_compressed(self.dir / f"run-{stamp(ref)}.npz", c=np.stack([coarse(g) for g in grids]))

    def _load(self, kind, t, fetch, add):
        name = f"{kind}-{stamp(t)}"
        path = self.dir / f"{name}.npz"
        if path.exists():
            return np.load(path)["c"]
        if time.time() > self.deadline:
            self.budget = 0
        if name in self.missing or self.budget <= 0:
            return None
        self.budget -= 1
        try:
            add(t, fetch(t))
        except FileNotFoundError:
            self.missing.add(name)
            return None
        return np.load(path)["c"]

    def obs(self, t):
        return self._load("obs", t, self.fetch_obs, self.add_obs)

    def run(self, ref):
        return self._load("run", ref, self.fetch_run, self.add_run)

    # -- scoring --

    def score(self, t):
        """Score the observation at t against each earlier run. False if inputs are missing."""
        o = self.obs(t)
        if o is None:
            return False
        o = o.astype(np.float32)
        shape = (LEADS,) + tile_sums(o).shape
        hit, fwet = np.full(shape, np.nan, np.float32), np.full(shape, np.nan, np.float32)
        owet = np.repeat(tile_sums(o)[None], LEADS, axis=0)
        cover = self.valid.astype(np.float32) if self.valid is not None else np.full(o.shape, CELL * CELL, np.float32)
        cover = np.repeat(tile_sums(cover[:o.shape[0], :o.shape[1]])[None], LEADS, axis=0)
        for k in range(1, LEADS + 1):
            r = self.run(t - k * STEP)
            if r is None:
                if f"run-{stamp(t - k * STEP)}" in self.missing:
                    continue          # KNMI never had this run: leave this lead empty
                return False          # out of budget: try again next time
            f = r[k - 1].astype(np.float32)
            hit[k - 1] = tile_sums(np.minimum(f, o))
            fwet[k - 1] = tile_sums(f)
        owet[np.isnan(hit)] = np.nan
        cover[np.isnan(hit)] = np.nan
        self.times.append(int(t.timestamp()))
        for k, v in zip(self.keys, (hit, fwet, owet, cover)):
            self.sums[k].append(v)
        return True

    def update(self, t0):
        """Score unscored times in the window, newest first, within the budget."""
        start = t0 - timedelta(hours=HOURS)
        done = set(self.times)
        todo = []
        t = t0
        while t > start:
            if int(t.timestamp()) not in done:
                todo.append(t)
            t -= STEP
        scored = 0
        for t in todo:
            if self.score(t):
                scored += 1
            elif self.budget <= 0:
                break
        # Forget scores outside the window and inputs no longer needed.
        keep = [i for i, ts in enumerate(self.times) if ts > start.timestamp()]
        self.times = [self.times[i] for i in keep]
        self.sums = {k: [v[i] for i in keep] for k, v in self.sums.items()}
        done = set(self.times)
        pending = [t for t in todo if int(t.timestamp()) not in done]
        oldest_needed = min(pending, default=t0) - LEADS * STEP
        for p in self.dir.glob("*.npz"):
            if p.name == "scores.npz":
                continue
            try:
                kind, s = p.stem.split("-")
                pt = datetime.strptime(s, "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
            except ValueError:
                continue      # not one of ours (e.g. build.py's coverage mask)
            if pt < oldest_needed or (kind == "obs" and int(pt.timestamp()) in done and pt < t0 - STEP):
                p.unlink()
        return scored, len(pending)

    def save(self):
        if self.times:
            np.savez_compressed(self.dir / "scores.npz", times=np.array(self.times),
                                **{k: np.stack(v) for k, v in self.sums.items()})
        self.missing = {m for m in self.missing if datetime.strptime(m.split("-")[1], "%Y%m%d%H%M").replace(tzinfo=timezone.utc) > datetime.now(timezone.utc) - timedelta(hours=HOURS + 3)}
        (self.dir / "missing.json").write_text(json.dumps(sorted(self.missing)))

    # -- publishing --

    def summary(self, t0):
        """Recency-weighted rain and dry reliability per lead, for each tile (with
        its neighbours) and nationally, in per mille."""
        if not self.times:
            return None
        ages = (t0.timestamp() - np.array(self.times)) / 3600
        w = (0.5 ** (ages / HALF_LIFE_H)).astype(np.float32)[:, None, None, None]
        tot = {k: np.nansum(np.stack(v) * w, axis=0) for k, v in self.sums.items()}   # (LEADS, ny, nx)

        def scores(t):
            h, fw, ow, cv = t["hit"], t["fwet"], t["owet"], t["cover"]
            miss = ow - h
            dry_fc = np.maximum(cv - fw, 1e-9)                 # pixels forecast dry
            rain = np.where(fw >= MIN_RAIN, np.round(1000 * h / np.maximum(fw, 1e-9)), -1)
            dry = np.where(fw + ow >= MIN_RAIN, np.round(1000 * (dry_fc - miss) / dry_fc), -1)
            return rain.astype(int), dry.astype(int)

        # Each tile is judged together with its 8 neighbours.
        def neigh(a):
            p = np.pad(a, ((0, 0), (1, 1), (1, 1)))
            return sum(p[:, dy:dy + a.shape[1], dx:dx + a.shape[2]] for dy in range(3) for dx in range(3))

        rain, dry = scores({k: neigh(v) for k, v in tot.items()})                  # (LEADS, ny, nx)
        nrain, ndry = scores({k: v.sum(axis=(1, 2)) for k, v in tot.items()})
        ny, nx = rain.shape[1:]
        return {
            "t0": t0.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "hours": HOURS,
            "scored": len(self.times),
            "score": "rain+dry",
            "cell": CELL,
            "tile": TILE,
            "nx": int(nx),
            "ny": int(ny),
            # Per mille for leads 5 … 120 min; -1: too little rain to judge.
            "nl": {"rain": nrain.tolist(), "dry": ndry.tolist()},
            # Row-major tiles.
            "rain": rain.transpose(1, 2, 0).reshape(ny * nx, LEADS).tolist(),
            "dry": dry.transpose(1, 2, 0).reshape(ny * nx, LEADS).tolist(),
        }
