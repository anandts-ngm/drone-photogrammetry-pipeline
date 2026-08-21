# Assumptions, open decisions, and verification log

Status: draft for review. This is the working document for everything that is not yet
settled. Items move out of §2 as they are decided and out of §3 as they are verified.

---

## 1. Assumptions taken

These are proceeding without a blocking question. Each states what happens if it is wrong.

| # | Assumption | If wrong |
|---|---|---|
| A1 | Python 3.12 is pinned. It has full wheel coverage across Rasterio/GDAL/pyproj/Shapely and is not at the edge of the geospatial ecosystem's support. | Change one line in `pyproject.toml` and regenerate `uv.lock`. Cheap. |
| A2 | Development happens on Windows 11 with Docker Desktop; NodeODM runs in a Linux container; CI runs on Linux. Code is written path-portable (`pathlib`, no shell-isms). | Cheap if caught early, expensive if not — so path portability is enforced from the first commit rather than assumed. |
| A3 | The CLI is installed as `drone-photo` while the distribution/repository is `drone-photogrammetry-pipeline`. | Rename in one `[project.scripts]` entry. |
| A4 | One workspace root, configured once, holding all generated data; source block directories are strictly read-only. | This is a mandate, not really an assumption. Recorded here because the workspace *layout* under that root is my proposal (`architecture.md` §5) and can be changed. |
| A5 | `run_id = <block>_<UTC compact>_<8 hex>`; runs are never overwritten, only added. | Trivial to change; nothing depends on its format except human readability. |
| A6 | Milestone 1 resolves navigation from EXIF only, and records the height type it finds rather than converting anything. MRK/PPK are interfaces with no implementation. | This is the stated milestone scope. |
| A7 | Full SHA-256 over all source imagery on every run. For a large P1 block this is I/O-bound and noticeable, but traceability was named as the priority over convenience. | If runtime proves unacceptable, add an opt-out flag that records *that it was used* in the manifest. Never silently downgrade the hash. |
| A8 | Block/project CRS and vertical datum are declared per block in `block.yaml` and validated, not inferred from imagery. | None — this is the safe direction. The actual values still have to come from you (§2.5). |

---

## 2. Decisions

§2.1–2.3 were decided on 2026-08-18 and are marked **DECIDED**. The rest remain open.

### 2.1 GDAL backend for the master packager — **DECIDED: option (a)**

Packaging uses the GDAL bundled in the Rasterio wheel, behind a `RasterBackend` protocol so
a Docker backend can be added later without touching callers. The manifest records the
backend name, the real GDAL version reported at runtime, and the creation options actually
used. It does not record a CLI command that was never executed.

The packager needs GDAL. Three realistic options:

| Option | Pros | Cons |
|---|---|---|
| **(a) GDAL bundled in the Rasterio wheel** *(recommended)* | Pure `uv sync` install, works on Windows and Linux identically, no host GDAL, version pinned by the lockfile, testable in CI with no containers | No `gdal_translate` binary; packaging is done through the GDAL API. The manifest records the API call and creation options, and the equivalent CLI command as documentation only |
| (b) Pinned `osgeo/gdal` Docker image | Byte-identical GDAL everywhere; real CLI command strings in the audit trail | Container round-trip and volume mounting per packaging operation; awkward on Windows paths; CI needs the image |
| (c) System GDAL on the host | Simplest conceptually | Not reproducible; GDAL is currently not installed on this machine; version drift is exactly what the reproducibility requirement forbids |

Recommendation: **(a)**, behind a `RasterBackend` protocol so (b) can be added later without
touching callers. The audit trail records the actual GDAL version and the actual creation
options used — it will not record a CLI command that was never executed.

### 2.2 ODM / NodeODM version pin

The requirement "no `latest` Docker tags" and "pin the newest ODM" currently conflict:

- Latest ODM release: **`v3.6.2`**, published 2026-08-12.
- Newest **concrete** `opendronemap/nodeodm` Docker Hub tag: **`3.5.6`**, pushed 2025-07-17.
- `opendronemap/nodeodm:latest` and `:master` moved 2026-03-25 — floating, so disallowed.

Options:

1. Pin `opendronemap/nodeodm:3.5.6`. Reproducible today, ~13 months behind ODM.
2. Pin by image **digest** (`opendronemap/nodeodm@sha256:...`) taken from `latest`. Fully
   reproducible and current; less readable, and needs a documented refresh procedure.
3. Build our own NodeODM image on ODM `v3.6.2`. Most current and fully controlled; adds an
   image-build pipeline we would then own.

**DECIDED: option (2), digest pin.** `docker-compose.yml` pins
`opendronemap/nodeodm@sha256:<digest>` resolved from the current `latest`, so nothing
floats while staying current. The digest is refreshed deliberately, never implicitly, and
the manifest records `engineVersion` from `/info` as ground truth for what actually ran.
The digest is resolved in Phase 4 when compose is written.

### 2.3 Global seam leveling default for `analytical_master` — **DECIDED: skip leveling**

ODM normalises colours across all images by default. ODM's own documentation recommends
disabling that for radiometric data.

Profiles ship with `texturing-skip-global-seam-leveling: true` and
`radiometry.provisional: true`, matching ODM's own advice for radiometric data. The value
is provisional pending the B64/B66, B44/B51 and N3/N7 benchmarks, which must be run **both
ways** before the setting is frozen. Erring toward preserving source radiometry rather than
toward a better-looking mosaic is the deliberate choice here. See `radiometry.md` §4.

### 2.4 Which ODM orthophoto is authoritative

ODM emits both `odm_orthophoto.tif` (cropped by `--crop`, default 3 m) and
`odm_orthophoto.original.tif` (uncropped). The cropped one is the sensible default for a
delivered product; the uncropped one preserves marginal coverage. This is a profile setting
either way — the decision is what the default should be.

### 2.5 Project CRS and vertical datum values — **RESOLVED for Buduunkhad**

Supplied by `METADATA_Buduunkhad_XV-023222.txt` in the delivery:

| | |
|---|---|
| Horizontal | `EPSG:32647` — WGS 84 / UTM zone 47N |
| Vertical | `EPSG:5705` — Baltic 1977 height, **normal** heights over a quasigeoid |
| Compound | `EPSG:32647+5705` |
| Geoid | Mongolia Geoid 2012, **already applied in the field** before processing |

Three consequences the code now reflects:

1. **`HeightType.NORMAL` was added.** Baltic 1977 is a normal-height system, explicitly not
   orthometric and not ellipsoidal. The enum could not previously express the actual data,
   and forcing it to `ORTHOMETRIC` would have been precisely the silent mixing of vertical
   references the mandate forbids.
2. **Reapplying a geoid would introduce ~48 m of error.** `VerticalReference.geoid_applied`
   records that the transformation has already happened, because that fact exists only in
   the document.
3. **The vertical CRS is absent from every delivered file header.** The metadata sheet is
   the only authoritative basis, so the pipeline can never recover it by inspection. The
   master therefore declares the compound CRS explicitly (`--declare-crs`), recorded as a
   metadata-only operation, which is exactly what the delivery's own instructions ask for
   (`gdal_edit.py -a_srs "EPSG:32647+5705"`, and never `gdalwarp`).

Still needed for other projects: the same two codes for Sant and any other area. Nothing is
inferred.

### 2.7 Repackage versus metadata-only fast path — **DECIDED: full repackage**

Only one contract clause is violated by the delivered DOMs (the NoData value), which is
metadata, so a byte-copy plus an in-place header edit looked attractive against 92 GiB of
input. Measurement settled it: packaging `B78` (447 MB) took **41.5 s including
`--verify-pixels`** and produced a **340 MB** master, because Terra does not use a
compression predictor and this pipeline does.

Extrapolated over 79 zones: roughly 2.5 hours, and the output set is about **70 GiB against
92 GiB of input**. A byte-copy fast path would consume the full 92 GiB and save nothing, so
it is not worth the second code path.

### 2.8 Output location — **DECIDED: `C:\PHOTOGRAMMETRY_OUTPUTS`**

Products were previously written to `C:\MINING_PIPELINE_WORKSPACE`, a name that says nothing
about what produced them and sits one letter from `C:\MINING_PIPELINE_WORK` — a *different*
pipeline (satellite/ASTER, KOMPSAT, regional GIS) covering the same licence area. Two
similarly-named roots, one of which this repository rebuilds wholesale, is a mistake waiting
to happen. The new name states the producing discipline, so a rebuild can never be aimed at
the GIS pipeline's products by accident.

Set per machine via `DPP_WORKSPACE_ROOT` in a gitignored `.env`, because it is a property of
the workstation rather than of the project.

The old root was deleted (108.5 GiB) after confirming both projects were regenerable: the
Buduunkhad sources are present, and the Sant sources were located at
`nergui_undur/sant/l_drone/N1..N9` — the manifests record `nergui-undur` with a hyphen, so
the folder was renamed after processing and a path-only check would have wrongly concluded
the sources were gone. The radiometry and harmonisation reports (1.65 MB) were preserved
first: they are measurements, not products, and re-deriving the pre-replacement baseline
would require restoring superseded source files.

### 2.9 Terrain data for topographic correction — **DECIDED: per-block `dtm.tif`**

The GIS pipeline has already produced 15 terrain derivatives at 0.5 m over the whole licence
area (slope, aspect, TPI, TRI, roughness, plan/profile curvature, LRM at 10/25/50 m, four
hillshades). Reusing them was considered and rejected *for this repository*, for two reasons.

They exist only inside that pipeline's run staging
(`work/runs/<uuid>/staging/05/03_Raster_Products`) with no stable published copy, so
depending on the path couples this pipeline's correctness to another pipeline's garbage
collection. And each delivered block already ships its own `dtm.tif` — 79 files, 485 MB
total, already an input here — on the block's own grid, so slope and aspect derived from it
need no resampling to line up with the orthophoto they correct.

The mosaicked derivatives remain the better choice for the *lithology feature stack*, where
multi-scale descriptors (TRI, TPI, LRM) are genuinely expensive to recompute and grid
alignment across blocks is a feature rather than a hazard. That is the ML dataset's concern,
not this repository's.

### 2.10 Gain versus gain+offset — **DECIDED: gain only, on physical grounds**

Measured in linear light, an affine model fits the matched quantiles far better than a pure
scale: per pair, `scale` leaves 10.03% and `gain+offset` leaves 2.99%. A single gain gets a
pair under 5% only 28.7% of the time, and the ratio `qq_b/qq_a` drifts a median 25% across
the quantile range, so the blocks genuinely do not differ by a scale factor alone.

The network solve cannot realise most of that: one coefficient pair per block instead of per
pair takes it from 2.99% to 7.4%, against 9.6% for gain only on the same metric and weighting.

It is nonetheless refused, because of how it buys the improvement. The solved offsets reach
-55 DN in linear terms, and the correction `gain * value + offset` clips at zero: **107 of
237 block-bands drive some shadow to pure black, and 45 of them crush everything below DN
60**. B55 red loses everything under DN 122 — close to half the tonal range. The quantile
residual improves because the sampled levels start at the 5th percentile and never see what
was destroyed below it.

This is a metric improving while the product gets worse, and for a lithology model the
crushed region is exactly the shaded rock face where the reading is hardest. Gain only stays.

If offsets are revisited, they must be constrained so that `gain * min_linear + offset >= 0`
per block, which is also the physically sensible bound: an offset models additive path
radiance, and no correction should subtract more light than the darkest pixel contains.

An earlier version of this comparison, measured on gamma-encoded values, reported that
gain+offset bought only 0.3-0.7 pp for +-135 DN. That number was misleading — the offset was
absorbing tone-curve curvature rather than a black level — but its conclusion was right for a
reason it had not identified.

### 2.11 Weighting applied consistently across models

`solve_gains` weighted its constraints by the square root of the sample count; the gain and
offset solves inside `solve_gain_offset` did not. Every comparison between the two models was
therefore a comparison of two changes at once, and the weighting mattered more than the model:
unweighted gain-only left 10.1% where weighted gain-only left 6.4%, so an unweighted
gain+offset at 8.0% could look like an improvement while being worse than the solution
actually deployed. Both solves are now weighted on the same basis.

### 2.12 Downstream train/test splits — **CORRECTED: geographic, not block-disjoint**

An earlier revision of `README.md` advised that because block identity stays recoverable from
corrected imagery at about 14.6 times chance, downstream splits "should be block-disjoint".
That advice was wrong, and it was committed and pushed before being caught.

Holding out whole blocks does not isolate anything here. Every block overlaps at least one
other, so each survey is a **single connected component** of overlapping footprints — which
this pipeline's own harmonisation solve reports directly, because the anchor count depends on
it:

| project | blocks | constraints | connected components |
|---|---|---|---|
| buduunkhad | 79 | 231 | 1 |
| sant | 9 | 20 | 1 |

A held-out block's ground is imaged by its neighbours, so its pixels are in the training set
under a different block id. The split has to be **geographic**: cut along a coordinate axis,
exclude a buffer either side of each cut, and assign each sampled *window* by its centre
rather than assigning whole blocks. Blocks straddle the cuts, which is precisely why
assignment is per window.

Two measurements worth keeping with this. Footprints are rotated rectangles inside
axis-aligned bounding boxes, so bounding boxes overstate the overlap as **3.06x** where the
real figure from footprint geometry is **1.43x** — a split reasoning from bounding boxes would
mis-estimate the leak. And uniform sampling inside a bounding box lands outside the imagery
about **65%** of the time, which is why windows must be placed from a validity mask.

Both figures come from the downstream `lithology-ml` repository, which had already reached the
correct conclusion; the error here was advising otherwise without checking.

### 2.13 One command per delivery, in the order that packages each block once — **DECIDED**

Preparing the repository for coworkers who will clone it, drop a delivery in a folder and run
it, the sequence a human would reach for is the wrong one. Building the masters first and
correcting them afterwards means packaging the delivery twice: 96 minutes of it on Buduunkhad.
Overlaps are measurable on the *sources*, so `process-project` measures, solves, and only then
packages, with each block's gain already known.

Three things went into `projects/*.yaml` rather than onto the command line:

- `declare_crs` and `height_type` are a 48 m error one flag apart, and putting them in a file
  makes them reviewable and diffable instead of retyped per stage and per rerun.
- Unknown keys are refused. A mistyped `destripe_preview` that pydantic quietly drops reads as
  configured while doing nothing.
- A `+` compound CRS with `height_type: UNKNOWN` is refused. The declaration exists to remove
  an ambiguity about which surface the heights are above; leaving the surface unstated puts it
  straight back.

`run-project` and `process-project` share one packaging implementation (`_package_blocks`),
and the derived stages are shared with `previews`, `overview` and `mosaic` the same way. Two
implementations of the same stage drift, and the drift shows up as products that differ
depending on which command wrote them.

### 2.14 A workspace inside the checkout is refused — **DECIDED**

`DPP_WORKSPACE_ROOT` defaults to a relative `workspace`, so a clone that skips copying
`.env.example` writes every product inside the git working tree. On this survey that is 75 GiB
of masters in a repository. `Workspace.__init__` now walks the ancestors for a `.git` and
refuses, naming the setting to change. `.env.example` was changed to a concrete off-repo path
for the same reason: a default that only works if you notice it is not a default.

### 2.15 The mosaic refuses a mixed-sensor project — **DECIDED: 4x GSD spread**

A VRT has one geotransform, and the grid has to be the finest native pixel size present or
detail the finest blocks really have is discarded. That makes one much finer block expensive
for everything: P1 orthophotos here are 1.81 mm against the L cameras' 25.4 mm, so a combined
mosaic would be 19,000 gigapixels rather than 97 — hours spent building something no viewer
can open.

`read_sources` therefore refuses a spread above 4x, which tolerates the 2.0x within the
L-camera survey and the 1.1x within Sant. The error says what to do instead: give each sensor
its own `project_id`. This is a refusal rather than an automatic regrid because resampling a
survey to a common coarser grid is a decision about the product, not a detail of mosaicking.

### 2.16 P1 geolocation: no `geo.txt` by default — **DECIDED**

A P1 folder arrives as raw imagery plus a `Timestamp.MRK` event-mark file, and the obvious move
is to turn the mark file into ODM's `geo.txt`. Reading ODM's own source says not to.

**What ODM already does.** `opendm/photo.py` reads `@drone-dji:RtkStdLon`, `RtkStdLat` and
`RtkStdHgt` from each image's XMP and sets that image's `gps_xy_stddev` to the larger of the
two horizontal values and `gps_z_stddev` from the height. On the measured block those are
1.3 cm to 2.3 cm, per image. `--gps-accuracy` (default **3 m**, not 10) is only the fallback
for imagery that carries no such tags.

**What a geo file would cost.** `GeoFile` parses
`filename x y [z] [yaw pitch roll] [horiz_acc vert_acc]`, and `update_with_geo_entry` assigns
`gps_xy_stddev = geo_entry.horizontal_accuracy` unconditionally. A four-column file therefore
*clears* the per-image RTK weighting ODM had just read, dropping the block back to the 3 m
default. Filling the accuracy columns is not available either: they sit behind the attitude
columns, ODM takes yaw from `FlightYawDegree` (absent from the exiftool export the drone team
produces) and on the EXIF path stores `90 + GimbalPitchDegree` for a DJI make while a geo entry
is taken as given — so writing the raw gimbal pitch would hand the bundle adjustment a
90-degree prior.

So `p1-geo` **reports** and does not write unless asked. What it checks is worth having on its
own:

| Check | Measured on `DJI_202608031301_013_B084` |
|---|---|
| Images matched to an exposure | 79 of 79 |
| Exposures in the mark file | 649 — one file per flight, one folder per block |
| RTK flag | 52 for every exposure, uniform |
| Position accuracy, 95th percentile | 2.2 cm horizontal, 2.3 cm vertical |
| Gimbal within 1 degree of nadir | 79 of 79 |

The image-to-exposure match is by the four-digit filename suffix and is **verified** against
that image's EXIF position, refusing anything more than a metre apart. Without that check a
wrong rule would georeference every image to a neighbour's position and nothing downstream
would notice.

**One question left open: the antenna-to-camera lever arm.** The mark file's `N/E/V` fields
are 0.402 m, 0.591 m and 0.219 m on the first exposure. They are a body-frame vector expressed
in local ENU, not a constant: measured across 649 exposures the offset's bearing holds a
constant 33 degrees relative to the flight course while the course itself runs 88, 268 and 311
degrees, and the magnitude stays at 0.71 m. So it reverses between opposite strips — a 1.4 m
relative displacement between neighbouring flight lines, not a uniform shift that a bundle
adjustment absorbs harmlessly.

Whether DJI has already applied it cannot be settled from this data: the EXIF position and the
mark position agree to 3 mm, so either both are the antenna phase centre or both are the camera.
Settling it needs a check point, or the same block processed both ways and compared. Until then
`p1-geo --apply-lever-arm` exists to run that experiment and is off by default, and this is the
one case where writing a `geo.txt` is the point rather than a cost.

### 2.17 Prompts live in setup only — **DECIDED**

Preparing this for other people raised the question of whether the commands should ask for
what they need. Two commands do, and the processing commands never will.

`init` and `new-project` run before anything is being processed, so there is nothing in flight
for a prompt to interrupt, and what they produce is a file rather than an answer that scrolls
away. Both take flags plus `--yes` so a script is never blocked on a human being present.

`process-project` asks nothing, and the reason is not taste:

* A run is two hours unattended. A prompt can surface at minute 50 with nobody watching, and
  it would break any future scheduling or unattended rerun.
* `declare_crs` and `height_type` are the 48 m fields. Typed at a prompt, that decision leaves
  no record of *why*; in `projects/*.yaml` it sits next to the document it came from. The one
  place a prompt earns its keep is `new-project`, which asks whether a document states a
  vertical datum rather than letting the answer default silently to "no" -- and then writes the
  document's name into `notes` so the next reader can check it.
* `--dry-run` already answers "tell me what you are about to do" without asking anything.

### 2.18 The overview's resolution is chosen, not configured — **DECIDED**

`overview_gsd` was the last number a new area needed someone to pick, and no fixed value can
serve every area. Both existing overviews were built at 0.5 m by hand. Measured against the
real extents, that is 19531 x 12869 px and 404 MB for Buduunkhad, while the same 0.5 m over a
350 ha P1 block would be under 4000 px: too much browse image in one case and too little in the
other.

It is now derived from the survey's extent, capped at a 10,000 pixel long edge, snapped to a
1-2-5 ladder, and never finer than the finest master present (past that it is upsampling, which
adds bytes and no detail). Measured:

| survey | extent | chosen | long edge |
|---|---:|---:|---:|
| Buduunkhad | 9,766 m | 1 m | 9,766 px |
| Sant | 2,667 m | 0.5 m | 5,334 px |
| a 350 ha P1 block | 1,870 m | 0.2 m | 9,350 px |

A cap rather than a target, because the failure that matters is a browse image too large to
browse. Sant lands on the 0.5 m it was given; Buduunkhad becomes 1 m, which is the intended
change. Only a derived viewing product is affected, so nothing measured depends on it.

### 2.6 Deviations from the proposed repository tree

Six small additions, each with a reason, listed in `milestone-1-plan.md` §2. Flagged here
because the kickoff asked for changes to the tree to be explained.

---

## 3. Verification log

The kickoff requires that uncertain ODM/NodeODM behaviour be verified rather than guessed.

### 3.1 Verified — 2026-08-18

| Item | Finding | Source |
|---|---|---|
| NodeODM API surface | `/info`, `/options`, `/task/new`, `/task/new/init`, `/task/new/upload/{uuid}`, `/task/new/commit/{uuid}`, `/task/list`, `/task/{uuid}/info`, `/task/{uuid}/output`, `/task/{uuid}/download/{asset}`, `/task/cancel`, `/task/restart`, `/task/remove`, `/auth/*`; optional `token` query parameter | NodeODM `docs/index.adoc` |
| Task status codes | `10` QUEUED, `20` RUNNING, `30` FAILED, `40` COMPLETED, `50` CANCELED; `progress` 0–100 | NodeODM `docs/index.adoc` |
| Downloadable assets | **Only `all.zip`.** `getAssetsArchivePath()` returns `false` for anything else | NodeODM `libs/Task.js` |
| ODM ortho creation options | `TILED=YES`, `COMPRESS=<--orthophoto-compression>`, `PREDICTOR=2` for LZW/DEFLATE else `1`, `BIGTIFF=IF_SAFER`, `BLOCKXSIZE=BLOCKYSIZE=512` | ODM `opendm/orthophoto.py: get_orthophoto_vars()` |
| ODM overview building | `gdaladdo -r average --config BIGTIFF_OVERVIEW IF_SAFER --config COMPRESS_OVERVIEW JPEG {ortho} 2 4 8 16` — lossy | ODM `opendm/orthophoto.py: build_overviews()` |
| Seam leveling default | `--texturing-skip-global-seam-leveling` default `False`; help: *"Skip normalization of colors across all images. Useful when processing radiometric data."* | ODM `opendm/config.py` |
| Orthophoto resolution semantics | `--orthophoto-resolution` default `5`, cm/pixel, *"capped by a ground sampling distance (GSD) estimate"* — a target, not a guarantee | ODM `opendm/config.py` |
| Other option defaults | `--orthophoto-compression DEFLATE`; `--orthophoto-no-tiled False`; `--build-overviews False`; `--crop 3`; `--dsm False`; `--dtm False`; `--dem-resolution 5`; `--pc-quality medium`; `--feature-quality high`; `--radiometric-calibration none`; `--camera-lens auto`; `--primary-band auto`; `--use-exif False` | ODM `opendm/config.py` |
| ODM output paths | `odm_orthophoto/odm_orthophoto.tif`, `odm_orthophoto/odm_orthophoto.original.tif`, `odm_dem/dsm.tif`, `odm_dem/dtm.tif`, `odm_georeferencing/odm_georeferenced_model.laz`, `opensfm/reconstruction.json` | ODM output documentation |
| Ortho alpha band | RGB orthophotos carry an alpha band; ODM treats band 4 as alpha in its own gdalwarp calls | ODM `opendm/orthophoto.py` |
| Versions | ODM `v3.6.2` (2026-08-12); NodeODM `v2.2.3` (2024-05-15); newest concrete nodeodm image tag `3.5.6` (2025-07-17) | GitHub releases API, Docker Hub tags API |

### 3.1.1 Verified locally — 2026-08-18 (GDAL 3.12.4, Rasterio 1.5.1, Python 3.12.13)

Measured on this machine rather than reasoned about, because the answers decide how the
packager writes the validity mask.

| Item | Finding |
|---|---|
| **V7 — `ALPHA=NON-PREMULTIPLIED`** | **Accepted.** With `PHOTOMETRIC=RGB` + `ALPHA=NON-PREMULTIPLIED`, band 4 reads back as `alpha`. This is what the packager uses, and a packaged master passes every contract check. |
| GTiff alpha tagging generally | Whether band 4 counts as alpha is decided by the TIFF `ExtraSamples` tag written **at creation time**: no creation options → alpha (the driver's default); `PHOTOMETRIC=RGB` alone → undefined; `PHOTOMETRIC=RGB` + `ALPHA=UNSPECIFIED` → undefined. |
| Post-hoc colour interpretation | Assigning `colorinterp` to an already-created GTiff **does not survive** when `PHOTOMETRIC=RGB` was given without an `ALPHA` option — the assignment is accepted and then silently lost on reopen. Alpha is therefore only ever set through the creation option, never after the fact. This bit the first version of the test fixtures, which claimed to build a tagged-alpha raster and actually built an untagged one. |
| Lossless round-trip | RGB band checksums are identical before and after packaging across every fixture, including striped/LZW/classic-TIFF sources. |

### 3.1.2 Verified against the real Buduunkhad delivery — 2026-08-18

`C:\MINING_PIPELINE_INPUTS\buduunkhad\buduunkhad` — 79 zones `B1`–`B79`, each holding
`dom.tif`, `dsm.tif`, `dtm.tif`, `cloud_merged.laz`. Headers only were read, with
`GDAL_PAM_ENABLED=NO` so GDAL could not write `.aux.xml` sidecars into the source tree.

**All 79 DOMs share exactly one raster signature**, totalling 92.1 GiB:

| Property | Delivered value | Master contract |
|---|---|---|
| Driver / bands / dtype | GeoTIFF, 4, uint8 | ✅ |
| Colour interpretation | red, green, blue, **alpha** | ✅ |
| Compression | DEFLATE | ✅ |
| Tiled | yes, 256×256 | ✅ |
| BigTIFF | yes | ✅ |
| Overviews | none | ✅ |
| CRS | `EPSG:32647` (horizontal only) | ✅ |
| **NoData** | **`0` on all four bands** | ❌ **the only violation** |

So a Terra DOM is one metadata flag away from conforming. It also means the delivery hits a
case the original alpha decision table did not name: **a tagged alpha band and an ambiguous
`NoData=0` present at the same time.** Alpha wins, the NoData is dropped, and the drop is
recorded as a packaging operation. Confirmed on a real file: `alpha: passthrough`, band-4
checksum identical, master passes QA.

Other measurements:

- **47 distinct native pixel sizes across 79 zones, 2.54 cm to 5.11 cm**, in clusters near
  2.5, 3.1, 3.7 and 5.0 cm. Every one is square and axis-aligned. This is the strongest
  possible argument for the no-rounding rule: normalising these to a common grid would
  resample 79 blocks to hide a real property of the survey.
- Rasters are large — around 40 000 × 40 000 px, 1–2 GB each.
- `dom.tif` is present for all 79 zones; none missing.

**Gaps in the delivery**, worth raising with the supplier:

- `block_register.csv` and `UTM47N_Baltic1977.prj` are named as accompanying files in
  §9 of the metadata sheet but are **not present**. The register is presumably what maps
  each zone to its sensor (the sheet mentions B1 as L3 and B69 as L2), which this pipeline
  needs in order to assign profiles.
- The metadata sheet's file table lists `dem.tif`; the zones contain `dsm.tif` and
  `dtm.tif` only.

### 3.1.3 Buduunkhad radiometric provenance — 2026-08-18

A `BH_L2_20260704_B64` DJI Terra quality report was supplied, the first available for
Buduunkhad. It settles two things and retires a claim this repository had briefly recorded.

| Finding | Source |
|---|---|
| DJI Terra **V5.2.5**, mission `BH_L2_20260704_B64` | report header |
| Payload **DJI Zenmuse L2**, SN 6U3DN6H0050PGJ — B64's sensor is now recorded, not inferred | Mission Parameters |
| **TDOM GSD 3.82 cm/px**, coverage 0.735 km² — the delivered `dom.tif` measures 3.81 cm, so it *is* the Terra output at its native grid | Output Preview |
| **No colour, white-balance, exposure or radiometric parameter appears anywhere** in the Reconstruction Parameters. Terra's LiDAR 2D-map workflow exposes point-cloud density, accuracy optimisation, smoothing, ground classification, DEM and output CRS only | Reconstruction Parameters |
| Two flights, 2 flight strips, average overlap **26.97 %**, flight heights 81.0 m and 82.7 m | Mission / Flight Strip Accuracy |
| POS Fix 100 %, **PPK Fix 100 %**; 4 check points, point-cloud RMSE 0.067 m | Accuracy Parameters |
| Output CRS `WGS 84 / UTM zone 47N`, geoid **Default** — matches the delivery metadata's account | Point Cloud Output Parameters |

**Consequence.** Because Terra exposes no radiometric setting, a re-export with the same
inputs is deterministic and cannot differ. The four-block re-export pilot was therefore
cancelled: it could not have produced new information.

**A claim was retired.** On the strength of an earlier reading that Buduunkhad was "Terra
output, corrected afterwards", a profile `external_terra_corrected` was created asserting
`external_corrected_uncontrolled`, and a project run began stamping it into every manifest.
That run was stopped at 20 of 79 and those runs discarded, the profile deleted, and the
project re-run under `external_terra`. No downstream correction is established, so none is
claimed.

What remains genuinely unknown is whether the delivered pixels are *byte-identical* to
Terra's output. A colour-only correction would preserve GSD and extent, so the matching GSD
does not exclude one. Settling it would need one re-exported block compared pixel-for-pixel;
that was judged not worth doing, so the manifests claim only what is supported: a Terra
export with no known post-processing.

**A hypothesis that failed, recorded so it is not retried.** The two-flight composition of
B64 suggested that within-block illumination change might explain the residual a per-block
gain cannot remove. It does not. Across 79 blocks the residual correlates with acquisition
time span at r = −0.02, coverage depth r = +0.04, sun elevation r = −0.23 and overlap count
r = +0.11. B32 spans 88 minutes with a 0.7 % residual; B17 spans 29 minutes with 8.1 %. The
3.0 % median residual sits near the ~2.2 % floor that B64/B66 reach naturally, so it may
simply be the measurement floor rather than a correctable effect.

### 3.2 Open — must be verified before the code depends on it

| # | Question | How it will be verified |
|---|---|---|
| ~~V1~~ | Semantics of the `outputs` parameter | **RESOLVED.** NodeODM's API definition: *"An optional serialized JSON string of paths relative to the project directory that should be included in the all.zip result file, overriding the default behavior."* Exactly the lever needed. The client sends it as a JSON array of paths. Still worth an empirical check against a running node with a tiny dataset. |
| V2 | Which ODM version is actually inside the pinned digest | `GET /info` on the running container (`engineVersion`). The compose file now pins `opendronemap/nodeodm@sha256:8845ee48…` (`latest` as resolved 2026-08-18), and every run records `engineVersion` in its manifest, so what actually ran is always known regardless of what the compose file says. |
| V3 | Is the DJI Zenmuse P1 (35 mm and 50 mm) present in ODM's sensor database, and are XMP yaw/pitch/roll read correctly? | Process a real P1 block; inspect ODM logs for unknown-camera warnings and `cameras.json` |
| V4 | Is the L2 RGB camera (4/3 CMOS) in the sensor database? | Same method, real L2 block |
| V5 | Actual band layout of a real ODM P1 orthophoto — band count, alpha tagging, NoData presence | `gdalinfo` / Rasterio on a real output; drives which row of the alpha decision table applies |
| ~~V6~~ | Characteristics of a real DJI Terra DOM export | **RESOLVED — see §3.1.2** |
| V8 | Does `PREDICTOR=2` with `BIGTIFF=YES` behave identically across the GDAL versions in ODM's container and in our packager | Round-trip a fixture and compare pixel checksums. **Half-answered:** our side round-trips RGB band checksums unchanged (`--verify-pixels`, covered by tests). The ODM-container side still needs a real ODM output. |
| V9 | Whether L-camera RGB from a LiDAR-parameterised flight has usable overlap for ODM reconstruction | Benchmark processing on a real L2/L3 block |
| V10 | L3 sensor specifications | Not publicly established — needs to come from you or from the acquisition metadata |

Nothing in §3.2 is coded against until it is answered. Where a milestone-1 module needs one
of these, it is written to detect and report the condition rather than to assume it.
