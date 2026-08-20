# Milestone 1 — proposed file tree and implementation sequence

Status: phases 0–2 implemented and green (lint, format, mypy strict, 80 unit tests).
Phases 3–7 are still proposals.

---

## 1. Proposed file tree

```text
drone-photogrammetry-pipeline/
│
├── README.md
├── AGENTS.md
├── pyproject.toml
├── uv.lock
├── docker-compose.yml
├── .env.example
├── .gitignore
├── .pre-commit-config.yaml
│
├── .github/
│   └── workflows/
│       └── ci.yml
│
├── docs/
│   ├── architecture.md                  ✓ written
│   ├── sensor-workflows.md              ✓ written
│   ├── raster-standard.md               ✓ written
│   ├── radiometry.md                    ✓ written
│   ├── decisions-and-verification.md    ✓ written
│   ├── milestone-1-plan.md              ✓ this file
│   ├── processing-flow.md               ← with implementation
│   └── compatibility.md                 ← with implementation
│
├── profiles/
│   ├── p1_35.yaml
│   ├── p1_50.yaml
│   ├── l2_rgb.yaml
│   ├── l3_rgb.yaml                      (placeholder; specs not yet supplied)
│   └── external_terra.yaml              ← added
│
├── src/
│   └── drone_photogrammetry_pipeline/
│       │
│       ├── __init__.py
│       ├── cli.py
│       ├── config.py
│       ├── log.py                       ← added
│       ├── workspace.py                 ← added
│       ├── integrity.py                 ← added
│       │
│       ├── models/
│       │   ├── enums.py                 ← added
│       │   ├── block.py
│       │   ├── profile.py
│       │   ├── manifest.py
│       │   └── qa.py
│       │
│       ├── ingest/
│       │   ├── scan.py
│       │   └── validate.py
│       │
│       ├── navigation/
│       │   ├── source.py                (NavigationSource abstraction)
│       │   └── exif.py                  (only implementation in M1)
│       │
│       ├── nodeodm/
│       │   ├── client.py
│       │   └── schemas.py
│       │
│       ├── processing/
│       │   ├── odm.py
│       │   └── external.py
│       │
│       ├── packaging/
│       │   ├── raster.py
│       │   └── gdal_backend.py          ← added
│       │
│       ├── qa/
│       │   ├── raster.py                (implemented in M1)
│       │   ├── checkpoints.py           (interface only)
│       │   ├── lidar.py                 (interface only)
│       │   └── radiometry.py            (interface only)
│       │
│       └── reporting/
│           └── manifest.py
│
├── tests/
│   ├── unit/
│   ├── fixtures/
│   │   └── make_rasters.py              ← added (generates fixtures, none committed)
│   └── integration/
│
└── examples/
    ├── p1_block.yaml
    └── l3_rgb_block.yaml
```

## 2. Deviations from the kickoff tree, and why

Six additions. Nothing was removed or moved.

| Addition | Reason |
|---|---|
| `workspace.py` | Source protection is a cross-cutting invariant. If every module computes its own output paths, "never write into the source directory" becomes a convention that erodes. One module owns path resolution, so the rule has exactly one place it can be broken — and one place to test. This is also the seam that a future object-store backend replaces. |
| `integrity.py` | SHA-256 hashing is needed by ingest (input manifest), packaging (output hashes) and profiles (profile hash). Without a shared home it gets reimplemented three times with three different canonicalisations, which would make hashes incomparable. |
| `models/enums.py` | The status model, sensor identifiers and source types are referenced by `block`, `profile`, `manifest` and `qa` alike. A shared leaf module avoids circular imports between the model files. |
| `log.py` | "Separate human-readable CLI output from machine-readable processing logs" is an explicit requirement. It needs an owner that configures both sinks once; otherwise the separation depends on every call site remembering it. Named `log.py` rather than `logging.py` so it cannot shadow the standard library module for a reader skimming imports. |
| `profiles/external_terra.yaml` | Every run manifest records a profile id, version and hash — including runs that perform no reconstruction. Without this profile, the packaging behaviour applied to an external product (band selection, alpha policy) would be undeclared and unversioned, which is exactly what the manifest exists to prevent. |
| `packaging/gdal_backend.py` | Isolates the GDAL invocation behind a protocol so the backend decision (`decisions-and-verification.md` §2.1) can change later without touching `raster.py`, and so packaging logic is testable against a fake backend. |
| `tests/fixtures/make_rasters.py` | Synthetic QA rasters are generated deterministically rather than committed as binaries. Keeps the repository free of binary blobs, and makes the fixtures themselves reviewable — a fixture that asserts "this file is not tiled" should be readable as code. |
| `.pre-commit-config.yaml`, `.github/workflows/ci.yml` | Required by the stated stack (pre-commit, GitHub Actions) but absent from the kickoff tree. |

---

## 3. Implementation sequence

Seven phases. Each ends at a point where the repository is coherent and tested.

### Phase 0 — Scaffold — **DONE** *(kickoff items 1, 2)*
`pyproject.toml` with pinned Python and dependencies, `uv.lock`, Ruff + type checking +
pre-commit configuration, `.gitignore`, `.env.example`, CI running lint/format/types on an
empty-but-valid package.

*Done when:* `uv sync` and `uv run pytest` succeed on a clean checkout, and CI is green.

### Phase 1 — Foundations — **DONE** *(items 4, 5, 6, 7)*
`config.py` (pydantic-settings), `logging.py`, `workspace.py`, `integrity.py`,
`models/` — `enums`, `Block`, `ProcessingProfile`, `RunManifest`, `qa`. Manifest JSON
schema defined and validated. Profile loading with hashing.

*Done when:* models round-trip through JSON, the manifest schema validates the example from
the kickoff, and profile hashing is stable across formatting changes.

### Phase 2 — The raster contract, end to end — **DONE** *(items 15, 16, 17, 18, 19, and 3 partially)*
`packaging/raster.py` + `gdal_backend.py`, `qa/raster.py`, `processing/external.py`,
output checksums, manifest writing, and the CLI commands `ingest-ortho`, `package`, `qa`.

**This phase is deliberately before NodeODM.** Reasons:

- It delivers the stated milestone-1 raster deliverable in full, with no ODM dependency.
- The master contract is the repository's actual product standard; both producing paths
  converge on it, so it is the highest-value thing to get right first.
- It is completely testable with synthetic rasters — no GPU, no reconstruction, no
  container, fast in CI.
- It is the phase most likely to surface a wrong assumption (alpha handling, BigTIFF,
  colour interpretation), and surfacing that early is cheap.

*Done when:* `drone-photo ingest-ortho --source terra <file>` produces a master that passes
raster QA, with a complete manifest, and QA correctly **rejects** each deliberately
non-conforming fixture.

### Phase 3 — Block ingest and validation *(items 8, 9)*
`ingest/scan.py`, `ingest/validate.py`, `navigation/source.py` + `exif.py`, profile
registry, `drone-photo validate`. Four-way severity classification. GCP and Check Point as
distinct types. Height-type recording with explicit failure when undeterminable.

*Done when:* `drone-photo validate ./example_p1_block` classifies every expectation
correctly, including the "missing but acceptable" and "missing and fatal" cases.

### Phase 4 — NodeODM — **client and compose DONE; adapter and CLI outstanding**
*(items 10, 11, 12, 13, 14, 20)*

Built: `nodeodm/client.py` and `nodeodm/schemas.py` — the only modules that speak HTTP to
NodeODM — plus `docker-compose.yml` pinned by digest. 21 tests, mocked at the httpx transport
layer so both the requests built and the responses parsed are covered, with no container.

Three things the client does deliberately:

- **Chunked upload always.** A P1 block is hundreds of 25 MB images; a single multipart POST
  of several gigabytes restarts from zero on any failure.
- **Retries only where safe.** Transport errors and 5xx are retried with backoff; a 4xx is
  the server saying the request was wrong, and repeating it asks the same wrong question.
- **Options validated against the running engine** via `/options`, never against a table kept
  in this repository, which would eventually disagree with the engine actually running.

Outstanding: `processing/odm.py` (the adapter that turns a validated block into a task and
its result into a `SourceOrtho`), and the `process` / `status` / `fetch` CLI commands.
`nodeodm/client.py` (the only module that speaks HTTP to NodeODM), `docker-compose.yml`,
`processing/odm.py`, CLI `process`, `status`, `fetch`. Chunked upload, task polling,
console-log capture, `all.zip` retrieval and selective extraction, cancel, bounded retry.

Blocked on decisions §2.2 (version pin) and verification V1 (`outputs` semantics).

*Done when:* unit tests pass against a mocked NodeODM, and a real container round-trips a
tiny dataset in the integration suite.

### Phase 5 — Orchestration
`drone-photo run` wiring validate → process → fetch → package → qa → manifest, with the
two-state status model and no automatic promotion to `MASTER`.

### Phase 6 — Interfaces for later milestones
`qa/checkpoints.py`, `qa/lidar.py`, `qa/radiometry.py` — typed result models, documented
contracts, `NotImplementedError` bodies, and TODO documentation. No speculative
implementation.

### Phase 7 — Documentation completion *(items 22, 23)*
`processing-flow.md`, `compatibility.md`, README, and the benchmark plan for the
initial validation strategy.

---

## 4. Test strategy

Unit tests never require an ODM reconstruction.

- **NodeODM** — mocked at the `httpx` transport layer, so the client's request
  construction and response parsing are both under test. Real-container tests live in
  `tests/integration/` behind a pytest marker and are excluded from ordinary CI.
- **Raster QA** — small synthetic rasters generated by `tests/fixtures/make_rasters.py`.
  For every contract clause there is a conforming fixture and a violating fixture; the
  violating fixture must fail with the specific clause named, not merely fail.
- **Packaging** — the grid invariants (§`raster-standard.md` §4) are asserted in tests, not
  only in production code. A test that packages a fixture and compares the geotransform,
  dimensions and pixel checksums is the guard against an accidental resample.
- **Models** — schema round-trips, and hash stability under formatting-only changes.

CI runs lint, format verification, type checks, unit tests and synthetic raster QA tests.
It does not run photogrammetry.

---

## 5. Definition of done for milestone 1

```bash
docker compose up -d

drone-photo validate ./example_p1_block
drone-photo run ./example_p1_block
```

validates inputs, submits imagery to NodeODM, monitors processing, downloads outputs,
locates the orthophoto, packages it to the master standard, runs raster QA, calculates
checksums, writes a run manifest, preserves processing logs, and returns a clear result.

```bash
drone-photo ingest-ortho --source terra ./example_l3_dom.tif
```

ingests the existing orthophoto, packages it to the same master contract, validates it, and
writes the manifest.

Not in milestone 1, interfaces only: automatic PPK, MRK conversion, advanced camera
calibration, full GCP conversion, Check Point QA, LiDAR cloud comparison, radiometric
overlap QA, project scheduler, multi-node scheduling, ClusterODM, PostGIS, object storage,
web dashboard, geological RGB indices, automatic MASTER approval.
