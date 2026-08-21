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

Not implemented: a single command chaining submit through fetch. No block has yet been
reconstructed end to end through ODM.

`docs/milestone-1-plan.md` has the sequence; `docs/decisions-and-verification.md` records
what has been verified and what is still open.

## Quick start

```bash
uv sync --all-groups
cp .env.example .env          # then set DPP_WORKSPACE_ROOT
```

Package one externally produced orthophoto:

```bash
uv run drone-photo ingest-ortho ./BLK001_DOM.tif \
    --source terra --project-id sant --block-id N003 --verify-pixels
```

This packages the raster to the master contract without resampling, runs QA, checksums the
output and writes a manifest.

Exit codes are `0` PASS, `1` FAIL, `2` REVIEW. REVIEW has its own code so a caller does not
read "a human still has to look at this" as success.

## Commands

| Command | Purpose |
|---|---|
| `validate` | Inventory a block and report what is present or missing |
| `ingest-ortho` | Package one external orthophoto, with a manifest |
| `run-project` | Package every block under a project directory |
| `package` | Package one raster, without a manifest |
| `qa` | Run master raster QA on a packaged orthophoto |
| `process` | Submit a block to NodeODM; returns the task id |
| `status` | Report a task's progress, optionally following it |
| `fetch` | Download a finished task, package its orthophoto and run QA |
| `radiometry` | Measure how much overlapping blocks disagree |
| `harmonise` | Solve one gain per block per band from those measurements |

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

Products go outside the repository, at the path in `DPP_WORKSPACE_ROOT`. Directory names are
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
```

A run is recognised as complete from its own manifest, matched on the source checksum, so a
project can be interrupted and restarted, and a re-delivered file with an unchanged name is
treated as new work.

## Radiometry

Blocks processed separately disagree over the ground they share. Measured on the 79-block
Buduunkhad delivery, in linear light, over 231 overlapping pairs:

| | median | p90 | worst |
|---|---:|---:|---:|
| as delivered | 19.9 % | 49.8 % | 125.3 % |
| after per-block gain correction | 7.5 % | 23.6 % | 69.3 % |

Measure, then solve, then apply:

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
- The delivered mosaics carry flight-strip striping of about 2.7 % of scene brightness,
  present in 99 % of blocks. It comes from Terra's blending, not from this pipeline.
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
