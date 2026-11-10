# kg-spike-pg

Postgres 16 + Apache AGE 1.5.0 + pgvector 0.8.7, used as the KG 2.0 Postgres
backend (RCL-44 dev, RCL-64 prod). Extensions are compiled from source per
target arch and auto-created on a fresh data volume.

## Why this exists (RCL-64 P0)

The original spike image was built by hand and pushed amd64-only. Prod
(`digital-twin` namespace) runs on the arm64 pool (opi-5), where an amd64 image
cannot schedule. This directory is the source-controlled, genuinely multi-arch
replacement.

## The TARGETARCH trap

`ARG TARGETARCH` must have **no default**. buildx injects it per target
platform; a default shadows that value and bakes a single arch for every
platform while the build still reports multi-arch. The Dockerfile declares it
with no default and never hardcodes an arch — both extensions compile for the
build stage's native arch. The build workflow then verifies the *published*
manifest carries both `linux/amd64` and `linux/arm64` (`docker buildx imagetools
inspect`), failing otherwise.

## Build

Pushed by `.github/workflows/build-kg-spike-pg.yml` to
`acrmcit.azurecr.io/kg-spike-pg:pg16-age-vector`. Manual: Actions → "Build
kg-spike-pg (multi-arch)" → Run workflow.

SPLIT BUILD: amd64 and arm64 build in parallel, each on its own native homelab
runner (matrix on the `X64` / `ARM64` runner labels) — no QEMU emulation, so
each arch builds at native speed. Each native job pushes a by-digest image (no
tag); a final `merge` job stitches the two digests into one multi-arch tag with
`docker buildx imagetools create` and verifies both arches are present. Per-arch
registry build caches (`:buildcache-amd64`, `:buildcache-arm64`) keep rebuilds
fast.

## Schema

`01-init-extensions.sql` creates `age` + `vector` on init. The full KG schema
(`kg_entity`/`kg_edge`/`kg_xref` + indexes) is applied by the infra-tf schema
ConfigMap mounted into `/docker-entrypoint-initdb.d` alongside this file.
