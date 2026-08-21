# drone-photogrammetry-pipeline

Drone RGB photogrammetry and master orthophoto production for DJI P1 and the RGB cameras
carried on L2/L3 acquisitions, built on OpenDroneMap / NodeODM, Python and GDAL.

Every approved product meets one raster contract, whatever produced it, and carries a run
manifest recording how it was made.

This repository does not touch the LiDAR path. Trajectory processing, point-cloud
reconstruction, ground classification and LiDAR DTM generation stay in DJI Terra. LiDAR
products are read here only as optional geometry references.

DJI Terra and Metashape do not run here; their outputs are inputs. A defect in a delivered
product must either be fixable losslessly during packaging or be referred back to the
supplier.

---

## Status

Implemented and tested:

- external orthophoto ingest, GDAL packaging to the master contract, raster QA, SHA-256
  checksums and run manifests
- batch processing of a whole project directory, resumable on the source checksum
- NodeODM client and ODM adapter, with the submit / status / fetch commands
- radiometric overlap measurement, per-block gain solving, and application of a solved
  correction during packaging
- `process-project`: one command per delivery, driven by a project file, in the order that
  packages each block once
- derived viewing products: per-block previews, a labelled contact sheet, a destriped
  browsable overview, and a virtual mosaic with an optional external pyramid
- radiometric pass thresholds, derived from the deliveries rather than chosen
- `run-block`: raw imagery through ODM to a packaged master in one command, verified on a real
  79-image DJI P1 flight strip against ODM 3.5.6

Not yet done: a complete P1 block. A P1 folder holds one flight strip, and only one strip of
the one local flight is held here, so the reconstruction proven end to end covers 0.4 ha rather
than a whole block.

`docs/milestone-1-plan.md` has the sequence; `docs/decisions-and-verification.md` records
what has been verified and what is still open.

## Quick start

**1. Install.** Needs [uv](https://docs.astral.sh/uv/) and Python 3.12; no GDAL install, no
Docker, no GPU.

```bash
git clone <this repo> && cd drone-photogrammetry-pipeline
uv sync --all-groups
uv run drone-photo init
```

**2. `init` asks two questions** — where the deliveries are, and where the products should go —
then checks both paths are usable, creates them if you want, writes `.env`, and reports which
projects already have blocks to process. It is the only command that asks anything.

```ini
DPP_INPUTS_ROOT=D:/drone_inputs              # where the deliveries are. Never written to.
DPP_WORKSPACE_ROOT=D:/photogrammetry_outputs # where the products go. Must be outside the repo.
```

Put them on a volume with room: a 79-block survey reads 92 GB and writes about 75 GB. To skip
the questions, `init --inputs <dir> --workspace <dir> --yes`, or copy `.env.example` by hand.

**3. Put the orthophotos where the tool looks for them** — one directory per project, one
directory per block inside it, each holding its `dom.tif` exactly as Terra exported it:

```text
D:/drone_inputs/
  buduunkhad/          <- matches project_id "Buduunkhad", lower case with underscores
    B1/dom.tif
    B2/dom.tif
    ...  B79/dom.tif
  sant/
    N1/dom.tif
    ...  N9/dom.tif
```

Copying is not required if the delivery is already on disk somewhere — pass
`--source-root <dir>` instead, or on Windows point a junction at it
(`New-Item -ItemType Junction -Path D:\drone_inputs\buduunkhad -Target <delivery>`), which
costs no disk space.

**One camera per project directory.** L-camera and P1 orthophotos must not share one, because
a mosaic grid has to be the finest pixel size present, and the Terra-exported P1 orthophotos
here are 1.81 mm against the L cameras' 25.4 mm. The tool checks this from the file headers in
about a second, and also refuses two coordinate systems, before packaging anything.

**4. Run it.**

```bash
uv run drone-photo process-project projects/buduunkhad.yaml --dry-run   # what it would do
uv run drone-photo process-project projects/buduunkhad.yaml             # do it
```

That measures the overlaps, solves one gain per block per band, packages every block to the
master contract with that correction applied, then renders the previews, the contact sheet,
the browsable overview and the virtual mosaic. On the 79-block Buduunkhad delivery it is
about two hours and turns 92 GiB of sources into 74 GiB of masters.

`--dry-run` writes nothing and reports which stages would run, how many blocks already have a
master, an output-size and time estimate, and how much room is free.

**Interrupting it is safe.** Every block is recognised as complete from its own manifest,
matched on the source file's checksum, so a rerun continues where it stopped and reprocesses
only what changed. Ctrl-C, reboot, rerun.

**5. Look at what came out.**

| | |
|---|---|
| `<workspace>/<project>/<block>/runs/<run>/master/` | the corrected orthophoto, its manifest, its QA result |
| `<workspace>/<project>/derived/<project>_overview.jpg` | the whole survey in one image, at a resolution chosen from its extent — open this first |
| `<workspace>/<project>/derived/<project>_contact_sheet.jpg` | every block on one page, labelled |
| `<workspace>/<project>/derived/<project>_mosaic.vrt` | open in QGIS for full-resolution work |
| `<workspace>/<project>/reports/` | what was measured, what was solved, what each run did |

Exit code `0` means every block passed. `1` means at least one failed. `2` means at least one
needs a human to look at it.

To package a single orthophoto instead:

```bash
uv run drone-photo ingest-ortho ./BLK001_DOM.tif \
    --source terra --project-id sant --block-id N003 --verify-pixels
```

This packages the raster to the master contract without resampling, runs QA, checksums the
output and writes a manifest.

Exit codes are `0` PASS, `1` FAIL, `2` REVIEW. REVIEW has its own code so a caller does not
read "a human still has to look at this" as success.

## The project file

One YAML document per delivery, in `projects/`, and it needs no editing: nothing in it is
specific to one machine. Two of its fields cannot be inferred from the imagery and move every
elevation if they are wrong, which is why they live in a reviewable file rather than on a
command line:

```yaml
project_id: Buduunkhad
source_type: DJI_TERRA
asset: dom.tif

declare_crs: EPSG:32647+5705   # adds a vertical reference the delivered files do not carry
height_type: NORMAL            # which surface those heights are above
```

The sources are looked for in `<DPP_INPUTS_ROOT>/<project_id lower-cased>`, then overridden by
`source_root` in the file if it is set, then by `--source-root` on the command line. When none
of them exists the error names all three, so there is nothing to guess about where a delivery
should have been.

**For a new exploration area, generate the file rather than copying one:**

```bash
uv run drone-photo new-project "Artsat" --source-root D:/drone_inputs/artsat
```

It counts the blocks, reads their CRS, refuses a folder holding two cameras, and then asks the
one question that cannot be answered from the imagery: *does a delivery document state a
vertical datum?* If yes, it composes `EPSG:<horizontal>+<vertical>` from the files and your
answer, requires the height type, and records the document's name in `notes`. If no, it
declares nothing and says so. Nothing else about a new area needs a decision — the overview's
resolution is chosen from the survey's extent.

`declare_crs` is documented for Buduunkhad in `METADATA_Buduunkhad_XV-023222.txt`, which also
records that the geoid was already applied in the field: reapplying it costs about 48 m. Sant
came with no such document, so `projects/sant.yaml` declares nothing and its masters are not
suitable for absolute-Z work until the drone team supplies one. A vertical component with no
`height_type` is refused rather than guessed, and an unrecognised key is an error rather than
a line that quietly does nothing.

Stages can be skipped with `--no-correct`, `--no-derived` and `--force`. `--overviews` builds
the mosaic pyramid, which reads every master once and takes hours on a large survey.

Overlaps are measured on the **sources**, before anything is packaged, so each block is
packaged once with its gain already known. Packaging first and correcting afterwards would
repackage the whole delivery: on Buduunkhad that is an extra 96 minutes.

## Commands

| Command | Purpose |
|---|---|
| `init` | Check and write the two paths this checkout needs |
| `new-project` | Write a project file for a new exploration area |
| `process-project` | A whole delivery from a project file: measure, solve, package, derive |
| `validate` | Inventory a block and report what is present or missing |
| `ingest-ortho` | Package one external orthophoto, with a manifest |
| `run-project` | Package every block under a project directory |
| `package` | Package one raster, without a manifest |
| `qa` | Run master raster QA on a packaged orthophoto |
| `p1-geo` | Check a DJI P1 flight folder's geolocation against its mark file |
| `process` | Submit a block to NodeODM; returns the task id |
| `status` | Report a task's progress, optionally following it |
| `fetch` | Download a finished task, package its orthophoto and run QA |
| `run-block` | Reconstruct one block of raw imagery and package it, in one command |
| `radiometry` | Measure how much overlapping blocks disagree |
| `harmonise` | Solve one gain per block per band from those measurements |
| `previews` | Render a small JPEG per master, plus a labelled contact sheet |
| `overview` | Assemble one destriped, browsable image of a whole project |
| `mosaic` | Write a virtual mosaic (`.vrt`) addressing every master |

The last three are also run by `process-project`; they exist separately for rebuilding one
derived product without touching the masters.

## Raw P1 imagery

A P1 delivery is not orthophotos, so `process-project` does not apply. A flight folder holds
the JPGs, a `Timestamp.MRK` covering the whole flight, the PPK observables and an exiftool
export:

```bash
uv run drone-photo p1-geo "<flight folder>" --project-id "Buduunkhad P1"   # check first
uv run drone-photo run-block "<flight folder>" --project-id "Buduunkhad P1" \
    --block-id B084 --profile p1_35_master
```

`run-block` validates, submits to ODM, follows the reconstruction with an ETA, downloads,
packages to the master contract and runs QA — one command. Ctrl-C stops the watching, not the
task: `drone-photo fetch <task id>` collects it afterwards.

**A P1 folder is one flight strip, not a block.** Measured on the local flight: the folder holds
79 exposures spanning 16 × 106 m, while its mark file covers the whole flight, 649 exposures
over 103 × 109 m. Reconstructing one folder gives a correct master of that strip — 0.4 ha — and
not the block. A whole block means submitting every folder of that flight together.

`p1-geo` checks before the hours are spent: that every image matches an exposure in the mark
file *and* agrees with that exposure's position, that the RTK flag is uniform, and that the
gimbal was pointing down. It reports rather than writing a `geo.txt`, because ODM already reads
each image's RTK standard deviations from its XMP — 1.3 to 2.3 cm on the measured block — and a
positions-only sidecar would replace them with ODM's 3 m default.

The antenna-to-camera lever arm in the mark file, 0.71 m horizontally, is **not** applied: the
EXIF and mark positions agree to 3 mm, so whether DJI already applied it cannot be told from
the delivery. `--apply-lever-arm` runs that experiment. `docs/decisions-and-verification.md`
§2.16 has the measurements.

**Verified against ODM 3.5.6** on that strip: the P1 is in ODM's sensor database (no
unknown-camera warning, no calibration override needed), all 79 images entered the
reconstruction, and the output packaged to a master that passed all fourteen contract checks.
One thing the run exposed: ODM's default orthophoto resolution is 5 cm/px, which produced a
5.0 cm master from imagery whose native GSD is 1.50 cm. The profiles now pin
`orthophoto-resolution` low and let the engine clamp to what the imagery supports, because the
master contract requires native resolution — see §2.21.

## The master contract

The producing engine does not set the product standard. P1 + ODM, L2/L3 RGB + ODM and an
approved Terra export all yield the same shape of file.

| | |
|---|---|
| Format | GeoTIFF, tiled, BigTIFF |
| Bands | 4 — red, green, blue, alpha |
| Compression | DEFLATE, lossless only |
| Resolution | native; never resampled, never rounded |
| Alpha | required, and it is the validity mask |
| NoData | unset — no ambiguous `NoData=0` alongside alpha |
| Overviews | none |
| CRS | explicitly defined |

Full specification and the QA checks that enforce it: `docs/raster-standard.md`.

## Outputs

Products go outside the repository, at the path in `DPP_WORKSPACE_ROOT`. A workspace inside
the checkout is refused: the default is a relative `workspace/`, and a clone that skipped the
`.env` step would otherwise put 75 GB of masters in a git working tree. Directory names are
lower case with underscores.

```text
<workspace>/
  <project>/
    B1/ ... B79/
      runs/<block>_<timestamp>_<hash>/
        master/       the packaged orthophoto
        manifest.json how it was made
        qa/           raster QA result
        logs/         JSONL processing log
    reports/
      radiometry/     overlap measurements
      harmonisation/  solved coefficients
      runs/           per-project run summaries
    derived/          built from finished masters, never measured from
      previews/       one JPEG per block
      <project>_contact_sheet.jpg
      <project>_overview.tif / .jpg
      <project>_mosaic.vrt
```

`derived/` is kept apart because everything in it is resampled or lossy by design. A master
and a picture of a master must never be confusable by location.

A run is recognised as complete from its own manifest, matched on the source checksum, so a
project can be interrupted and restarted, and a re-delivered file with an unchanged name is
treated as new work.

The virtual mosaic costs kilobytes: GDAL reads the masters on demand rather than copying them,
and it opens in QGIS as one raster layer. Its grid is the finest native pixel size present, so
a project mixing sensors is refused rather than built — P1 orthophotos here are 1.81 mm
against the L cameras' 25.4 mm, which would be 19,000 gigapixels instead of 97. Give each
sensor its own `project_id`. Without a pyramid (`--overviews`) a zoomed-out read of the mosaic
has to touch every pixel of every master and in practice does not finish, which is what the
single small overview raster is for.

## Radiometry

Blocks processed separately disagree over the ground they share. Measured on the 79-block
Buduunkhad delivery, in linear light, over 231 overlapping pairs:

| | median | p90 | worst |
|---|---:|---:|---:|
| as delivered | 19.9 % | 49.8 % | 125.3 % |
| after per-block gain correction | 7.5 % | 23.6 % | 69.3 % |

`process-project` does this in one pass. The three stages are also separate commands, for
measuring a delivery without committing to packaging it:

```bash
uv run drone-photo radiometry <blocks>/ --project-id buduunkhad --linearise
uv run drone-photo harmonise --project-id buduunkhad
uv run drone-photo run-project <blocks>/ --source terra --project-id buduunkhad \
    --harmonise-with <solution>.json
```

`--linearise` inverts the sRGB transfer function before measuring, so the numbers describe
light rather than display values. The report records which space it used, and a solution
inherits that space, because a gain is only valid applied in the space it was solved in: a
gain of 1.42 in light is 1.16 in display values, and using one where the other belongs is a
mean error of about 22 DN.

Correction and `--verify-pixels` cannot be used together. Verification asserts the pixels are
unchanged; a correction changes them.

### Known limits

- A single gain per block removes a level difference but not a difference in distribution
  shape. Across the quantile range the ratio between two blocks drifts by a median of 25 %.
- Gain-plus-offset fits the measurements better but is not used: the solved offsets drive
  shadow to pure black in 107 of 237 block-bands.
- The delivered mosaics carry flight-strip striping, present in 99 % of blocks. Sampled across
  whole blocks rather than along a single transect it is 4.9–5.7 % of scene brightness on
  average and 17.6 % at worst. It comes from Terra's blending, not from this pipeline. It is
  removed from the previews and the overview (78 % of the ripple, both surveys) and never from
  a master: no directional filter can tell a stripe from real linear geology, and a preview is
  the one place where getting that wrong costs nothing.
- Block identity stays recoverable from corrected imagery at about 14.6 times chance.
  Correction reduces disagreement between blocks; it does not make them indistinguishable.
  A downstream train/test split must therefore be **geographic, cut along a coordinate axis
  with an exclusion buffer, and applied per sampled window** — not block-disjoint. Holding
  out whole blocks does not work here: every block overlaps another, so the survey is one
  connected component of overlapping footprints and a held-out block's ground is imaged by
  its neighbours. Measured on the Buduunkhad delivery, bounding boxes overstate that overlap
  as 3.06x where the real figure from footprint geometry is 1.43x.

`docs/radiometry.md` has the method and the measurements.

## Layout

```text
docs/        architecture, sensor workflows, raster standard, radiometry, decisions
profiles/    versioned per-sensor processing profiles; ODM options live only here
projects/    one file per delivery: where its sources are, and what its CRS means
src/         the package
tests/       unit tests and generated raster fixtures
examples/    example block.yaml documents
```

## Documentation

| Document | What it covers |
|---|---|
| [architecture.md](docs/architecture.md) | Layering, stage contracts, workspace, status model |
| [sensor-workflows.md](docs/sensor-workflows.md) | Path A (P1) and Path B (L2/L3 RGB), navigation, GCP vs check points |
| [raster-standard.md](docs/raster-standard.md) | The master contract, GDAL realisation, alpha and NoData rules, QA checks |
| [radiometry.md](docs/radiometry.md) | Overlap measurement, harmonisation, ODM options that alter radiometry |
| [decisions-and-verification.md](docs/decisions-and-verification.md) | Decisions taken, and what has and has not been verified |
| [milestone-1-plan.md](docs/milestone-1-plan.md) | File tree, implementation sequence, test strategy |

## Development

```bash
uv run pytest          # unit tests; no ODM, no GPU, no containers
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

Integration tests needing a real NodeODM are marked `integration` and excluded by default.
CI does not run photogrammetry.
