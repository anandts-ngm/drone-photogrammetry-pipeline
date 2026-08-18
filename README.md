# drone-photogrammetry-pipeline

Reproducible drone RGB photogrammetry and master orthophoto production for **DJI P1** and
the **RGB cameras carried on L2/L3 acquisitions**, built on OpenDroneMap / NodeODM, Python
and GDAL.

The goal is not simply to automate photogrammetry. It is to make the workflow repeatable,
auditable, sensor-aware, radiometrically consistent, and suitable for downstream geological
analysis.

**This repository does not touch the LiDAR path.** Trajectory/POS processing, point-cloud
reconstruction, ground classification, LiDAR DTM generation and strip QA stay in DJI Terra.
LiDAR products are consumed here only as optional, read-only geometry references.

**DJI Terra and Metashape do not run here.** Their outputs are inputs. Nothing can be
re-exported from this repository, so a defect found in a delivered product must either be
fixable losslessly during packaging or be escalated to the supplier.

---

## Status

Milestone 1, phases 0–2 are implemented and tested: the scaffold, the typed contracts, and
the complete master-raster path — external ortho ingest, GDAL packaging, raster QA,
SHA-256 checksums and the run manifest.

The NodeODM client and the P1 reconstruction path are phase 4 and are not implemented yet.
See `docs/milestone-1-plan.md` for the sequence and `docs/decisions-and-verification.md`
for what is still open.

## Quick start

```bash
uv sync --all-groups
cp .env.example .env          # then set DPP_WORKSPACE_ROOT

uv run drone-photo ingest-ortho ./BLK001_DOM.tif \
    --source terra --project-id Sant --block-id N003 --verify-pixels
```

This ingests an externally produced orthophoto, packages it to the master contract without
resampling, runs raster QA, checksums the output and writes a run manifest.

```bash
uv run drone-photo qa ./N003_ORTHO_MASTER.tif    # QA an existing master
uv run drone-photo package IN.tif --out OUT.tif  # package without a manifest
```

Exit codes: `0` PASS, `1` FAIL, `2` REVIEW. REVIEW has its own code so that a caller cannot
treat "a human still has to look at this" as success.

## The master contract

Every approved master satisfies the same contract regardless of whether it came from P1 +
ODM, L2/L3 RGB + ODM, or an approved DJI Terra export. The producing engine does not set
the product standard.

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

## The Buduunkhad delivery

79 zones (`B1`–`B79`), 92.1 GiB of Terra DOMs, all sharing one raster signature. They are
already GeoTIFF/RGBA/DEFLATE/tiled/BigTIFF with a CRS and no overviews — the single contract
violation is a `NoData=0` carried alongside a valid alpha band. Packaging keeps the alpha,
drops the NoData, and records both facts.

Native pixel sizes run from **2.54 cm to 5.11 cm across 47 distinct values**. They are
preserved exactly; normalising them to a common grid would resample 79 blocks to conceal a
real property of the survey.

The reference system is `EPSG:32647` horizontally and Baltic 1977 (`EPSG:5705`, **normal**
heights) vertically — but **no delivered file carries the vertical CRS in its header**. It
exists only in `METADATA_Buduunkhad_XV-023222.txt`, along with the warning that the geoid
was already applied in the field and reapplying it costs ~48 m. So the vertical reference is
declared, never inferred:

```bash
uv run drone-photo ingest-ortho .../B78/dom.tif \
    --source terra --project-id Buduunkhad --block-id B78 \
    --declare-crs "EPSG:32647+5705" --height-type NORMAL --verify-pixels
```

`--declare-crs` adds a vertical component as metadata only; a mismatched horizontal
component is refused, because that would relocate every pixel without changing the
geotransform.

## Radiometry

Overlapping ground processed as separate blocks currently disagrees by 11–15 % median
(worst 25 %), while a known-compatible pair agrees to 2.2 %. That disagreement is
introduced by processing, and in a band-ratio product it reads as a geological boundary.

ODM applies global colour normalisation across all images **by default**. Profiles here
disable it and mark the choice provisional pending benchmarks. See `docs/radiometry.md`.

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
| [architecture.md](docs/architecture.md) | Layering, stage contracts, workspace, status model, verified upstream behaviour |
| [sensor-workflows.md](docs/sensor-workflows.md) | Path A (P1) and Path B (L2/L3 RGB), navigation, GCP vs check points |
| [raster-standard.md](docs/raster-standard.md) | The master contract, GDAL realisation, alpha/NoData rules, QA checks |
| [radiometry.md](docs/radiometry.md) | The measured problem, the policy, ODM options that alter radiometry |
| [decisions-and-verification.md](docs/decisions-and-verification.md) | Assumptions, decisions, and what has and has not been verified upstream |
| [milestone-1-plan.md](docs/milestone-1-plan.md) | File tree, implementation sequence, test strategy |

## Development

```bash
uv run pytest          # unit tests; no ODM, no GPU, no containers
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

Integration tests that need a real NodeODM are marked `integration` and excluded by
default. CI never runs photogrammetry.
