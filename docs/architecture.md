# Architecture

Status: draft for review. No implementation has been written yet.

This document defines the layering, the contracts between layers, and the invariants
that the rest of the repository must not violate. It is the reference for every later
design decision.

---

## 1. Scope

In scope: RGB photogrammetry and orthophoto production for DJI P1 and the RGB cameras
carried on L2/L3 acquisitions, plus ingestion of externally produced RGB orthophotos
(DJI Terra DOM), plus packaging and QA to a single NETGROUP master standard.

Out of scope: the entire LiDAR path (trajectory/POS, point-cloud reconstruction, ground
classification, LiDAR DTM, strip QA). LiDAR products are treated as read-only external
reference data used for optional geometric QA.

---

## 2. Design principles

**P1. The product standard belongs to this repository, not to the engine.**
ODM, NodeODM and DJI Terra are interchangeable sources of a raster. The master contract
(see `raster-standard.md`) is enforced downstream of all of them, identically.

**P2. Sources are immutable.**
Nothing the pipeline does writes into, renames, or re-encodes anything under a block's
source directory. All generated data lives in a separate workspace.

**P3. Contracts are typed.**
Stages exchange Pydantic models, not dictionaries. A stage boundary that cannot be
expressed as a model is a design smell.

**P4. Every product is reproducible from its manifest.**
Profile hash, input manifest hash, engine versions, exact packaging operations and output
checksums are recorded. If a number cannot be traced to a recorded input, it is not a
product.

**P5. Success of a processing engine is not approval of a product.**
Engine status and product gate status are separate state machines (§8).

---

## 3. Layer model

```text
        ┌────────────────────────────────────────────────────────────┐
        │                        CLI  (Typer)                        │
        │        human-readable output only; no business logic       │
        └───────────────────────────┬────────────────────────────────┘
                                    │
        ┌───────────────────────────▼────────────────────────────────┐
        │                      ORCHESTRATION                         │
        │   run/step sequencing, workspace, run identity, manifest   │
        └───┬───────────────┬───────────────┬──────────────┬─────────┘
            │               │               │              │
   ┌────────▼──────┐ ┌──────▼───────┐ ┌─────▼───────┐ ┌────▼─────────┐
   │  INGESTION    │ │  PROCESSING  │ │  EXTERNAL   │ │  NAVIGATION  │
   │  block scan   │ │  ODM adapter │ │  PRODUCT    │ │  NavSource   │
   │  validation   │ │      │       │ │  INGEST     │ │  CRS/height  │
   │               │ │  NodeODM     │ │  Terra DOM  │ │              │
   │               │ │  client      │ │             │ │              │
   └───────┬───────┘ └──────┬───────┘ └─────┬───────┘ └────┬─────────┘
           │                │               │              │
           └────────────────┴───────┬───────┴──────────────┘
                                    │  SourceOrtho  (common contract)
                    ┌───────────────▼────────────────┐
                    │        MASTER PACKAGER         │
                    │      GDAL, no resampling       │
                    └───────────────┬────────────────┘
                                    │  MasterRaster
                    ┌───────────────▼────────────────┐
                    │           QA ENGINE            │
                    │  raster │ geometry │ radiometry│
                    └───────────────┬────────────────┘
                                    │  QAReport
                    ┌───────────────▼────────────────┐
                    │      REPORTING / MANIFEST      │
                    │   RunManifest, checksums, logs │
                    └───────────────┬────────────────┘
                                    │
                          PASS / REVIEW / FAIL
                                    │
                          explicit promotion only
                                    ▼
                              MASTER PRODUCT
```

The two producing paths converge at `SourceOrtho`. Everything below that line is shared
and engine-agnostic. This is the single most important structural property of the system:
it is what makes a Terra-derived master and an ODM-derived master comparable.

---

## 4. Stage contracts

| Boundary | Input model | Output model | Notes |
|---|---|---|---|
| Block scan | path | `Block` | filesystem layout, discovered assets |
| Validation | `Block` | `ValidatedBlock` | severity-classified findings; fatal blocks the run |
| Profile load | sensor/lens id | `ProcessingProfile` | YAML, versioned, hashed |
| Navigation resolve | `ValidatedBlock` | `NavigationSolution` | CRS, height type, accuracies, source |
| ODM processing | `ValidatedBlock` + `ProcessingProfile` | `EngineResult` | NodeODM task id, versions, asset paths |
| External ingest | path + source declaration | `EngineResult` | Terra DOM; no reconstruction performed |
| Source selection | `EngineResult` | `SourceOrtho` | locates the orthophoto to package |
| Packaging | `SourceOrtho` | `MasterRaster` | GDAL; records every operation applied |
| Raster QA | `MasterRaster` | `RasterQAResult` | contract conformance only |
| Geometry QA | `MasterRaster` + CPs / LiDAR | `GeometryQAResult` | interface in M1, implementation later |
| Radiometric QA | two `MasterRaster` | `RadiometricQAResult` | interface in M1, implementation later |
| Manifest write | all of the above | `RunManifest` | JSON, schema-validated |

`ValidatedBlock` is deliberately a distinct type from `Block`. A function that requires
validated input should be unable to accept unvalidated input by construction, rather than
by convention.

---

## 5. Workspace and source protection

Source blocks are read-only inputs. The pipeline never writes inside them. Generated data
goes to a workspace root configured once (`config.py` / `.env`):

```text
<workspace_root>/
└── <project_id>/
    └── <block_id>/
        ├── runs/
        │   └── <run_id>/
        │       ├── manifest.json
        │       ├── inputs/
        │       │   └── input_manifest.json    # paths + sizes + SHA-256 of sources
        │       ├── engine/
        │       │   ├── all.zip                # NodeODM archive, retained or hashed
        │       │   └── extracted/             # only the assets the profile requested
        │       ├── master/
        │       │   └── <BLOCK>_ORTHO_MASTER.tif
        │       ├── qa/
        │       │   └── raster_qa.json
        │       └── logs/
        │           ├── pipeline.jsonl         # structured, machine-readable
        │           └── engine_console.log     # raw NodeODM/ODM console output
        └── master/                            # promoted product, written only on promotion
```

Source imagery is referenced by absolute path and hash in `inputs/input_manifest.json`.
It is not copied into the workspace and not symlinked — symlink creation on Windows
requires elevation or developer mode, so relying on it would make the layout
environment-dependent.

Uploading images to NodeODM is a read of the source, never a move or a rewrite. EXIF is
read but never written back.

---

## 6. Run identity and traceability

`run_id = <block_id>_<UTC timestamp, compact>_<8 hex chars>`

Three hashes give reproducibility:

- `input_manifest_hash` — SHA-256 over the sorted list of `(relative name, size, sha256)`
  of every source file that entered the run. Changes if a single image changes.
- `profile_hash` — SHA-256 over the canonicalized profile document, so a silent profile
  edit cannot be mistaken for the same processing.
- `output_hashes` — SHA-256 per delivered output.

Engine versions (`nodeodm_version`, `odm_version`) are read from the running service, not
assumed from configuration, because the deployed image is the thing that actually produced
the result.

---

## 7. Where radiometric policy lives

Radiometric policy is a property of the **profile**, not of the code, and it is hashed
into the manifest. Any ODM option capable of altering block radiometry is declared in one
place in the profile, never spread through Python source.

Verified behaviour that makes this urgent (see `radiometry.md` for detail): ODM performs
global colour normalisation across all images **by default**
(`--texturing-skip-global-seam-leveling` defaults to `False`), and ODM's own help text
recommends skipping it when processing radiometric data. A default ODM run is therefore
*not* radiometrically neutral. This is exactly the class of setting that the mandate
requires to be explicitly evaluated rather than accepted because the mosaic looks better.

---

## 8. Status model

Two independent state machines, deliberately not merged:

```text
engine / workflow status                product gate status
────────────────────────                ───────────────────
VALIDATION_PENDING                      NOT_EVALUATED
VALIDATED                                    │
PROCESSING_PENDING                           │
PROCESSING                                   │
PROCESSING_COMPLETE                          ▼
PACKAGING                               PASS | REVIEW | FAIL
PACKAGED                                     │
QA_PENDING                                   │ explicit promotion
QA_RUNNING                                   ▼
QA_COMPLETE                                MASTER
```

`gate_status` starts at `NOT_EVALUATED` and is only ever set by the QA engine.
`MASTER` is never set by the pipeline as a side effect of files existing; it requires an
explicit promotion step. A NodeODM task reporting status 40 (COMPLETED) sets
`PROCESSING_COMPLETE` and nothing more.

---

## 9. Verified external behaviour that shaped this design

Checked against current upstream sources on 2026-08-18; re-verify when versions move.

| Finding | Consequence for architecture |
|---|---|
| NodeODM's `/task/{uuid}/download/{asset}` accepts **only `all.zip`** (`Task.js: getAssetsArchivePath`) | No selective asset download. The client must request a restricted asset set at task creation via the `outputs` parameter and extract members locally. Retrieval is a distinct stage with its own storage cost. |
| ODM writes the orthophoto with `TILED=YES`, `COMPRESS=<--orthophoto-compression>` (default `DEFLATE`), `PREDICTOR=2`, `BIGTIFF=IF_SAFER`, `BLOCKXSIZE/YSIZE=512` | ODM output is close to, but not equal to, the master contract. `IF_SAFER` does not guarantee BigTIFF, so repackaging is mandatory rather than optional. |
| `--build-overviews` defaults to `False`, and when enabled builds **JPEG-compressed** overviews (`gdaladdo -r average --config COMPRESS_OVERVIEW JPEG`) | The "zero overviews in the master" rule is not cosmetic: enabling overviews would embed lossy data in the delivered file. Profiles must keep it off and QA must assert overview count 0. |
| `--texturing-skip-global-seam-leveling` defaults to `False` (colour normalisation across all images is **on** by default) | Radiometric policy must be explicit in every profile. See §7. |
| `--orthophoto-resolution` is "capped by a ground sampling distance estimate" | The profile value is a *request*, not a guarantee. Actual pixel size must be read back from the produced raster and recorded; never resampled to match the request. |
| `--crop` defaults to `3` (metres) | Coverage is engine-influenced. Coverage differences between blocks may be a processing artefact, not a data artefact; record the value. |
| ODM ortho paths: `odm_orthophoto/odm_orthophoto.tif` (cropped), `odm_orthophoto.original.tif` (uncropped), `odm_dem/dsm.tif`, `odm_georeferencing/odm_georeferenced_model.laz` | Asset location is a lookup table in the ODM adapter, not scattered string literals. Which of the two orthos is authoritative is a profile decision. |
| Latest ODM release is `v3.6.2` (2026-08-12), but Docker Hub's newest **concrete** `opendronemap/nodeodm` tag is `3.5.6` (2025-07-17); `latest`/`master` moved 2026-03-25 | Pinning to a non-floating tag and pinning to the newest ODM are currently in conflict. Open decision — see `decisions-and-verification.md`. |

---

## 10. Deliberately deferred

FastAPI, Celery, Redis, Kafka, Kubernetes, PostgreSQL/PostGIS, object storage and
ClusterODM are out of scope for milestone 1. The seams that let them arrive later without
a rewrite:

- **Storage.** All workspace access goes through one module that resolves paths. A future
  object-store backend replaces that module, not its callers.
- **Execution.** The ODM adapter depends on a `NodeODMClient` interface, not on a URL.
  ClusterODM is a different base URL behind the same interface.
- **State.** Run state is a `RunManifest` document written to disk. A future database
  stores the same document; nothing else needs to know.
- **Job submission.** Orchestration is a sequence of pure-ish stage functions over models,
  so a future queue invokes the same functions from a worker.

No infrastructure is introduced ahead of a production requirement that justifies it.
