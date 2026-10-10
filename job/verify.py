"""How well did recent forecasts hold up, per lead time and region?

Each observed frame at time t is compared with what the forecast runs issued
5, 10, … 120 minutes earlier predicted for t. Comparison uses the Fractions
Skill Score (FSS) on wet/dry fields within ~13 km neighbourhoods, so a shower
forecast a few km off still counts as mostly right, and dry areas where both
agree contribute nothing (a dry day doesn't look "perfect").

Scores are kept per ~40 km tile for the last HOURS and published as
skill.json: for every tile (combined with its neighbours) and lead time, the
recency-weighted FSS over the window, or -1 where too little rain fell to say.

State lives in OUT/state (stored on the data branch, not deployed):
  run-YYYYmmddHHMM.npz   coarse wet counts for a forecast run's 24 lead frames
  obs-YYYYmmddHHMM.npz   coarse wet counts for an observed frame
  scores.npz             per scored time: FSS sums per lead and tile
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
NEIGH = 3                 # FSS neighbourhood, in cells (~13 km)
TILE = 10                 # tile, in cells (~40 km)
MIN_REF = 50.0            # below this (weighted rain amount) there's too little rain to judge
BUDGET = 20               # KNMI downloads per run for back-filling
TIME_LIMIT = 180          # seconds per run for back-filling; the rest waits for the next run

STEP = timedelta(minutes=5)
WET_BYTE = 1 + round((math.log10(WET_MMH) + 1) / math.log10(400) * 254)


def coarse(grid):
    """Byte grid (H × W) → wet-pixel counts per CELL × CELL block."""
    h, w = (grid.shape[0] // CELL) * CELL, (grid.shape[1] // CELL) * CELL
    wet = grid[:h, :w] >= WET_BYTE
    return wet.reshape(h // CELL, CELL, w // CELL, CELL).sum(axis=(1, 3)).astype(np.uint8)


def fractions(counts):
    """Fraction of wet pixels in the NEIGH × NEIGH cell neighbourhood of each cell."""
    c = counts.astype(np.float32)
    p = NEIGH // 2
    padded = np.pad(c, p)
    s = sum(padded[dy:dy + c.shape[0], dx:dx + c.shape[1]] for dy in range(NEIGH) for dx in range(NEIGH))
    return s / (NEIGH * NEIGH * CELL * CELL)


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
        self.budget = BUDGET
        self.deadline = time.time() + TIME_LIMIT
        mf = self.dir / "missing.json"
        self.missing = set(json.loads(mf.read_text())) if mf.exists() else set()
        sf = self.dir / "scores.npz"
        if sf.exists():
            z = np.load(sf)
            self.times, self.mse, self.ref = list(z["times"]), list(z["mse"]), list(z["ref"])
        else:
            self.times, self.mse, self.ref = [], [], []

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
        fo = fractions(o)
        mse = np.full((LEADS,) + tile_sums(fo).shape, np.nan, np.float32)
        ref = np.full_like(mse, np.nan)
        for k in range(1, LEADS + 1):
            r = self.run(t - k * STEP)
            if r is None:
                if f"run-{stamp(t - k * STEP)}" in self.missing:
                    continue          # KNMI never had this run: leave this lead empty
                return False          # out of budget: try again next time
            ff = fractions(r[k - 1])
            mse[k - 1] = tile_sums((ff - fo) ** 2)
            ref[k - 1] = tile_sums(ff ** 2 + fo ** 2)
        self.times.append(int(t.timestamp()))
        self.mse.append(mse)
        self.ref.append(ref)
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
        self.mse = [self.mse[i] for i in keep]
        self.ref = [self.ref[i] for i in keep]
        done = set(self.times)
        pending = [t for t in todo if int(t.timestamp()) not in done]
        oldest_needed = min(pending, default=t0) - LEADS * STEP
        for p in self.dir.glob("*.npz"):
            if p.name == "scores.npz":
                continue
            kind, s = p.stem.split("-")
            pt = datetime.strptime(s, "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
            if pt < oldest_needed or (kind == "obs" and int(pt.timestamp()) in done and pt < t0 - STEP):
                p.unlink()
        return scored, len(pending)

    def save(self):
        if self.times:
            np.savez_compressed(self.dir / "scores.npz", times=np.array(self.times), mse=np.stack(self.mse), ref=np.stack(self.ref))
        self.missing = {m for m in self.missing if datetime.strptime(m.split("-")[1], "%Y%m%d%H%M").replace(tzinfo=timezone.utc) > datetime.now(timezone.utc) - timedelta(hours=HOURS + 3)}
        (self.dir / "missing.json").write_text(json.dumps(sorted(self.missing)))

    # -- publishing --

    def summary(self, t0):
        """Recency-weighted FSS per lead for each tile (with its neighbours) and nationally."""
        if not self.times:
            return None
        ages = (t0.timestamp() - np.array(self.times)) / 3600
        w = (0.5 ** (ages / HALF_LIFE_H)).astype(np.float32)[:, None, None, None]
        mse = np.nansum(np.stack(self.mse) * w, axis=0)       # (LEADS, ny, nx)
        ref = np.nansum(np.stack(self.ref) * w, axis=0)

        def fss(m, r):
            return np.where(r >= MIN_REF, np.round(100 * (1 - m / np.maximum(r, 1e-9))), -1).astype(int)

        # Each tile is judged together with its 8 neighbours.
        def neigh(a):
            p = np.pad(a, ((0, 0), (1, 1), (1, 1)))
            return sum(p[:, dy:dy + a.shape[1], dx:dx + a.shape[2]] for dy in range(3) for dx in range(3))

        local = fss(neigh(mse), neigh(ref))                      # (LEADS, ny, nx)
        national = fss(mse.sum(axis=(1, 2)), ref.sum(axis=(1, 2)))
        ny, nx = local.shape[1:]
        return {
            "t0": t0.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "hours": HOURS,
            "scored": len(self.times),
            "cell": CELL,
            "tile": TILE,
            "nx": int(nx),
            "ny": int(ny),
            "nl": national.tolist(),
            # Row-major tiles, each a list of FSS×100 for leads 5…120 min (-1: too little rain).
            "tiles": local.transpose(1, 2, 0).reshape(ny * nx, LEADS).tolist(),
        }
