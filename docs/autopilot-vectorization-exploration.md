# Vectorizing `bluesky/traffic/autopilot.py` — Exploration Notes

This is an exploratory analysis, not an implementation plan. No code was changed.

## Current state: it's already half vectorized, by design

The file has a deliberate two-tier architecture, stated in the `wppassingcheck`
docstring (`bluesky/traffic/autopilot.py:113`):

- **Continuous guidance** (`update()`, lines 329-483) is **fully vectorized**.
  Every line operates on whole traffic arrays with `np.where`, boolean masks,
  and vectorized helpers (`geo.qdrdist`, `vcasormach2tas`, `distaccel`). It
  runs every sim timestep from `traffic.py:407`.
- **Waypoint switching** (`wppassingcheck`, `ComputeVNAV`, `setspeedforRTA`,
  `calcvrta`, and `actwp.calcturn`) is **scalar, per aircraft**, on the
  argument that it's event-driven and rare: only aircraft in `idxreached`
  (those passing a waypoint this timestep) enter the loop.

So "vectorising autopilot.py" really means: vectorising the waypoint-passing
path. That assumption of rareness holds for small scenarios, but breaks at
scale — with thousands of aircraft on dense routes (TMA scenarios, drone
swarms), many aircraft pass a waypoint every update, and the Python loop body
is heavy: two Python-level `route` method calls, a scalar `geo.qdrdist`, up
to two scalar `cas2tas` calls, `calcturn`, and `ComputeVNAV` per aircraft.

## The fundamental obstacle: ragged route data

The per-aircraft `Route` objects (`bluesky/traffic/route.py`) store waypoints
as plain Python lists of varying length (`wplat`, `wplon`, `wpalt`, `wpspd`,
`wpstack`, ...). The loop's data source, `self.route[i].getnextwp()`, is a
scalar gather from those lists, plus side effects (advancing `iactwp`,
runway-landing logic that issues stack commands). You can't vectorize across
aircraft while each aircraft's route lives in its own Python object with its
own list lengths. Everything else in the loop is arithmetic that vectorizes
fine — this data structure is the crux.

There are also two genuinely event-like operations in the loop that resist
vectorization but don't block it:

- `runactwpstack()` (`route.py:1340`) just pushes command strings onto the
  stack (deferred execution), so it's cheap and can stay a loop over
  `idxreached`.
- The runway-landed branch inside `getnextwp()` does string parsing and
  navdb lookups — inherently scalar, but rare.

## Tiers of vectorization effort, by ambition

### Tier 0 — measure first

Only `idxreached` aircraft pay the scalar cost. Before restructuring, profile
a large scenario to confirm the loop is actually hot relative to the already
-vectorized `update()` (which itself does two full-fleet great-circle
`qdrdist` calls per timestep — lines 332 and 420 — a known cost that could be
cut with `kwikqdrdist` for the turn-distance check, where precision matters
less).

### Tier 1 — vectorize over `idxreached` without touching `Route` (low risk, good payoff)

Keep a small loop that only does the ragged gathers (`getnextwp`,
`getnextturnwp`, `runactwpstack`) into temporary arrays of length
`len(idxreached)`, then replace all subsequent scalar math with array
operations using fancy indexing (`arr[idxreached] = ...`):

- The pre-gather shift `actwp.spd[i] = actwp.nextspd[i]` (lines 149-150) → one
  fancy-indexed assignment before the gather loop.
- The `qdrdist` per aircraft (line 218) → one vectorized call on the reached
  subset.
- The `nextwpttas` four-way branch (lines 254-264) → `np.select` / nested
  `np.where` (`cas2tas` in `aero.py` is already numpy-vectorized).
- The turndist adjustments, flag copies, and the reduced-turnspd correction
  (lines 274-297) → masked array ops.
- The trailing RTA loop (lines 313-327) → vectorizable the same way, though
  its population is usually tiny.

### Tier 2 — vectorize `ComputeVNAV` and `calcturn` (medium risk)

Both are pure(ish) arithmetic with branching, which translates mechanically
to masks:

- `ComputeVNAV` (lines 485-653) has a three-way top split (descend / climb /
  level) plus nested conditions (urgent descent, swtod/swtoc alternatives).
  Each branch is a boolean mask over the reached subset; outputs (`dist2vs`,
  `actwp.vs`, `nextaltco`, `selalt`) become `np.where` chains. The structure
  is very similar to what `update()` already does, so this fits the
  codebase's idiom. Main care point: it also calls `setspeedforRTA`, whose
  `calcvrta` quadratic solve is vectorizable (both roots computed, validity
  masks, fallback `dx/dt`) but fiddly.
- `calcturn` (`activewpdata.py:119`) branches on how many of {bank, radius,
  speed, heading-rate} are user-specified. Vectorized form: compute
  `num_defined_values` as an integer array, build a mask per case, compute
  all candidate formulas, and select. Verbose but mechanical.

### Tier 3 — columnar route storage (high effort, full vectorization)

Replace per-aircraft Python lists with flat numpy arrays plus per-aircraft
offsets (CSR-style): `wplat_all[wpstart[ac] + iactwp[ac]]` becomes a
vectorized gather for *all* reached aircraft at once, and `getnextwp` mostly
disappears into indexing. `calcfp`'s lookahead products (`wptoalt`,
`wpxtoalt`, `wptorta`, `wpxtorta`) are already precomputed per route and would
slot into the same layout. This is the only way to eliminate the Python loop
entirely, but it's an architectural change touching `route.py` (1645 lines),
every stack command that edits routes, and any plugin that pokes `Route`
internals — BlueSky's `Entity`/`replaceable` plugin system means external
code depends on these shapes. Only worth pursuing if Tier 1+2 profiling still
shows the gather loop dominating.

**Alternative to Tier 3:** keep the event-driven scalar design and JIT the hot
loop with numba (or move `getnextwp`'s arithmetic into the existing C++ geo
extension pattern — there's already a `src_cpp` under `tools/geo`). Often a
better effort/reward ratio than restructuring ragged data.

## Hazards to respect in any of these

- **Side-effect ordering.** The loop mutates `qdr` and `self.dist2wp` in place
  (lines 218-222), which `update()` consumes right after; `spd`/`nextspd`
  shifting must happen before the gather; `oldturnspd` must be captured
  before `turnspd` is overwritten. Vectorized versions must preserve these
  read-before-write orders — they're easy to break when reordering into
  batch operations.
- **The `swlastwp` early-`continue`** (lines 170-176) removes aircraft from
  the rest of the loop body; in vector form that's a shrinking mask, and
  every subsequent assignment must use the filtered index set.
- **`getnextwp` isn't a pure gather** — it advances `iactwp` and can trigger
  the landed-runway path. Tier 1/2 keep it in a loop precisely for this
  reason.
- **Scalar/array duck-typing:** `cas2tas` etc. work on both scalars and
  arrays, so the math ports cleanly, but branches like
  `if nextwptspd > 0` become masks, and `-999` sentinel conventions (used
  everywhere here) must survive `np.where` chains — sentinel leakage into
  physics formulas is the classic bug in this style of code.
- **Testing:** behaviour is easiest to lock down by running identical
  scenarios (e.g. from `scenario/`) before/after and comparing trajectory
  logs, since much of this logic only fires at waypoint-passing moments that
  unit tests rarely cover.

## Bottom line

The file's continuous guidance is already vectorized; the payoff target is
the waypoint-passing path. The pragmatic route is Tier 1 (batch the
post-gather arithmetic over `idxreached`) plus Tier 2 (mask-based
`ComputeVNAV`/`calcturn`), which gets you ~90% of the win without touching
the `Route` data structure. Full vectorization requires flattening the ragged
route storage — a much larger, plugin-breaking change only worth undertaking
with profiling evidence that the gather loop still dominates.
