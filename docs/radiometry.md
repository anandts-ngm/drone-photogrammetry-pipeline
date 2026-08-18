# Radiometric policy

Status: draft for review. Policy is defined here; thresholds are deliberately not frozen.

---

## 1. The measured problem

Overlapping ground, independently processed as separate blocks, does not agree
radiometrically:

| Project | Overlapping pairs | Median difference | Worst | Blocks |
|---|---|---|---|---|
| Sant | 20 | 15.1 % | 25.3 % | N3 / N7 |
| Buduunkhad | 422 | 11.0 % | 16.6 % | B44 / B51 |
| *(known compatible)* | — | 2.2 % | — | B64 / B66 |

These comparisons are over the **same physical ground**. Geology is identical across the
overlap by construction, so the disagreement cannot be attributed to the surface. It is
introduced by processing.

The B64/B66 figure of 2.2 % matters as much as the failures: it shows the scale of
disagreement that is achievable, so 11–15 % is not an inherent limit of the method.

## 2. Why this is a correctness problem, not a cosmetic one

The master orthophoto is intended to feed derived products — iron-oxide indices, gossan
indicators, RGB ratios, alteration proxies. A block-boundary radiometric step of 11–15 %
propagates into those products as a spatial pattern that follows the flight blocks. It
will be interpreted as a geological boundary, because it looks exactly like one.

Block-specific processing can therefore manufacture false geological signals. That is the
risk this policy exists to prevent.

---

## 3. Mandatory rules for MASTER products

- No per-block automatic white balance.
- No independent automatic colour correction.
- No per-block histogram normalisation.
- No independent cosmetic enhancement.
- No modifying one block merely to visually match another.
- Preserve source radiometry as far as practical.
- One documented radiometric-processing policy across a project / acquisition series.

Any automatic engine-side colour or seam normalisation capable of altering block radiometry
is explicitly evaluated before it is enabled. A setting is never accepted because the
resulting mosaic looks better.

The policy is declared in the profile (`radiometry.policy`, e.g. `analytical_master`), and
the profile hash is recorded in every run manifest — so "which radiometric policy produced
this file" is always answerable from the product itself.

---

## 4. ODM options that alter radiometry

Verified against ODM `opendm/config.py` and `opendm/orthophoto.py`, 2026-08-18.

| Option | ODM default | Effect | Policy position |
|---|---|---|---|
| `--texturing-skip-global-seam-leveling` | `False` | When `False`, ODM **normalises colours across all images**. ODM's own help says: *"Skip normalization of colors across all images. Useful when processing radiometric data."* | Candidate: set `true` for `analytical_master`. Must be benchmarked both ways before being frozen. |
| `--orthophoto-cutline` | `False` | Computes a cutline and applies EDT-based feathering/blending across seams, mixing pixel values from different images near boundaries | Keep `false` for the master. Blending is an appearance operation. |
| `--build-overviews` | `False` | Builds overviews with `COMPRESS_OVERVIEW JPEG` — lossy data inside the file | Keep `false`. Also enforced by the raster contract. |
| `--orthophoto-compression` | `DEFLATE` | Lossless at default; `JPEG` and `LZMA` options exist | Pin `DEFLATE`. Enforced by the raster contract. |
| `--radiometric-calibration` | `none` | Camera / camera+sun calibration for multispectral and thermal | Leave `none` for RGB. It is not a general RGB normalisation tool and must not be repurposed as one. |
| `--crop` | `3` (metres) | Shrinks the output boundary; changes which pixels exist, including the marginal ones most likely to be radiometrically extreme | Record explicitly; keep consistent within a project so coverage differences are not mistaken for data differences. |

The important verified fact is the first row: **a default ODM run is not radiometrically
neutral.** Global colour normalisation is on unless it is switched off. Any comparison
between blocks processed with it on is comparing normalised, not source, radiometry.

Whether normalisation on or off yields better inter-block agreement is an empirical
question. Intuition says off; that intuition is exactly what the benchmark exists to test,
since leveling could equally reduce within-block variation enough to improve agreement.
Both configurations are to be run on the benchmark pairs before either is frozen.

---

## 5. The Terra route

Route B2 ingests an orthophoto that was radiometrically processed by DJI Terra under
settings this repository did not control and may not be able to read back.

Consequences, all recorded in the manifest rather than assumed away:

- `source_type` and `processing_engine` distinguish Terra products from ODM products.
- Terra-derived and ODM-derived masters of the same ground are **not** assumed to be
  radiometrically comparable. Cross-engine overlap statistics are reported with the engine
  pair attached.
- Any Terra-side colour setting that can be recovered from the export or its metadata is
  recorded. Where it cannot be recovered, the manifest says so — an unknown radiometric
  history is a fact about the product, not a gap to be left blank.

---

## 6. Overlap QA — implemented

`drone-photo radiometry <project-dir> --project-id <id>` measures how much every pair of
overlapping blocks disagrees about the ground they share. It reports numbers and judges
nothing.

### Why it is built this way

**Ground patches, not pixel pairs.** Two independently processed orthophotos are not
co-registered to the pixel. A per-pixel difference would therefore mix a radiometric signal
with a geometric one, and the geometric part would dominate on any edge or texture. Instead
a square of ground is read from both blocks and averaged down to a common small grid. That
compares the same ground without assuming the pixels line up.

**Different native pixel sizes cost nothing.** Each patch is averaged to a fixed output
size, so a 1.6 cm block and a 5 cm block contribute comparably. The resampling happens
inside QA on temporary data; no master is touched, which is what `raster-standard.md` §6
requires.

**Only ground valid in both blocks counts.** Alpha is read with the colour bands and a
sample is used only where both blocks are fully valid. Partially transparent edge pixels
cannot contaminate the result. In the Buduunkhad set this matters: a number of pairs
overlap by bounding box while sharing no valid ground at all, and those are reported as
such rather than silently returning a meaningless number.

**Spread beats volume.** Neighbouring pixels are highly correlated, so many small patches
spread over the whole overlap carry more information than a few large ones in one corner.
The defaults are 48 patches of 4 m.

**Medians throughout.** With hundreds of pairs, some samples will land on shadow, water or
a vehicle. A mean would follow them; a median will not.

### What it reports

Per pair, and per band:

```text
block_a, block_b
overlap_area_ha
sample_count, sample_pixels
median_a, median_b
median_difference                    (DN)
relative_difference_pct              symmetric, so swapping the blocks only flips the sign
robust_normalized_difference_pct     median of the per-sample symmetric difference
status                               NOT_EVALUATED, always, for now
```

### Reading the result

If the difference is **similar across red, green and blue**, the blocks disagree about
*exposure*, and a per-block gain can model it. If it is **concentrated in one band**, there
is a colour cast, and a single gain cannot fix it.

That distinction decides whether harmonisation is even possible with a gain-and-offset
model, which is why measurement comes before correction.

## 6a. Measured results — 2026-08-18

First full measurement of both deliveries. The two projects turn out to be in **opposite
regimes**, which matters more than either individual number.

| | Buduunkhad | Sant |
|---|---|---|
| Blocks / measured pairs | 79 / 234 (of 435 by bounding box) | 9 / 20 (of 20) |
| Median worst-band disagreement | 9.3 % | **18.5 %** |
| Band spread (max − min across R,G,B) | 2.3 pp | **15.0 pp** |
| Spread ÷ disagreement | **0.21** | **0.85** |
| Pairs whose bands disagree in sign | 9 / 234 (4 %) | **7 / 20 (35 %)** |
| Overlap graph | one component, all 79 | one component, all 9 |

**Buduunkhad is an exposure problem.** All three bands move together, so a single per-block
brightness factor explains most of it. **Sant is a colour-cast problem.** Blue moves
independently of red and green, and in a third of pairs it moves in the *opposite direction*
— which no single gain can ever fix.

Effect of solving one gain per block per band over the overlap network:

| | red | green | blue |
|---|---|---|---|
| Buduunkhad | 7.7 → **2.9 %** | 7.5 → **2.8 %** | 8.0 → **3.0 %** |
| Sant | 9.2 → 3.0 % | 6.1 → 3.1 % | **16.1 → 5.5 %** (90th pct 43.5 → 17.7 %) |

Buduunkhad lands at 2.9 %, essentially the 2.2 % that B64/B66 already achieved — so gain-only
harmonisation is sufficient there. Sant's red and green behave the same way, but **blue does
not correct**: its 90th percentile is still 17.7 % afterwards. Sant's blue is also the band
measured earlier as very dark and partly clipped at zero (median 34–66 DN, ~0.8 % of pixels
at exactly 0), so it carries little signal to rescale in the first place.

Two consequences:

- Sant's blue band should not be trusted for any colour-ratio product without further work.
  A gain model is provably insufficient for it.
- Illumination is **not** the cause at Buduunkhad. Per-block brightness correlates with sun
  elevation at only r = +0.25 (r² = 0.06), despite sun elevation spanning 38–67° across the
  survey. The differences come from per-block processing, not from the sky.

Coefficients are written to `harmonisation_gains.csv` in each project's workspace directory.
Blocks with few overlaps have correspondingly weaker constraints — B1 has 3, B70 and B18
have 4 — and their coefficients should be treated as less certain than a block tied in by
eight.

## 6b. From measurement to correction

The overlap graph for Buduunkhad is **a single connected component**: 435 pairs sharing at
least a hectare, spanning all 79 blocks. Sant is likewise connected across 20 pairs and 9
blocks.

Connectivity is what makes harmonisation solvable. With 435 constraints and 79 unknowns per
band, per-block coefficients are a least-squares solution over the whole network — the same
idea as a bundle adjustment, applied to radiometry instead of geometry. Nobody chooses the
settings; the overlaps determine them.

Three properties this must have, all of which follow from the mandate in §3:

1. **One solve for the whole project**, never per-block auto-correction. Correcting each
   block in isolation optimises each one separately and therefore *guarantees* a mismatch at
   every boundary. It is the cause of the problem in §1, not a fix for it.
2. **An anchor.** Relative constraints alone let all blocks drift together. The project mean
   is fixed, or a reference block, or ground targets where they exist.
3. **Coefficients recorded in the manifest**, so the correction is reversible and auditable
   and the MASTER remains the untouched per-block product.

Harmonisation is a *derived* product. It never overwrites a master, and it is not seam
blending: feathering hides a discontinuity by averaging pixels across it, which corrupts
exactly the block margins that band-ratio products depend on. Removing the cause and hiding
the symptom are not the same operation.

Success is measured by re-running this same tool afterwards and watching the distribution
move toward the ~2 % that B64/B66 already achieves.

## 6b. Original interface notes

```text
block A ∩ block B  →  equivalent-ground sample  →  statistics
```

The eventual `RadiometricQAResult` records:

```text
block_a
block_b
overlap_area
mean_difference
median_difference
per-band difference
robust normalized difference
qa_status
```

Design constraints already fixed:

- Compare **equivalent ground only** — the intersection of the two alpha-valid regions,
  not the bounding-box intersection.
- Blocks may have different native pixel sizes (§ `raster-standard.md` §6). Comparison
  therefore samples to a common analysis grid. That resampling happens **inside QA on
  temporary data**; it never touches either master.
- Report robust statistics (median, and a robust normalised difference) alongside means.
  With 422 pairs in one project, means are dominated by outliers.
- Report per-band as well as combined. A uniform brightness offset and a colour cast are
  different defects with different causes.

## 7. Thresholds are not frozen

No permanent PASS threshold is defined yet. Setting one now would be guessing.

The initial validation datasets are the known cases:

```text
B64 / B66     known compatible, 2.2 %
B44 / B51     11.0 % median, 422 pairs
N3  / N7      15.1 % median, 20 pairs
```

Thresholds are derived from these benchmarks once the pipeline can measure them
consistently, and the derivation is documented alongside the numbers. Until then, overlap
QA reports values and marks status `NOT_EVALUATED` rather than inventing a verdict.
