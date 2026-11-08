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

Pushed by `.github/workflows/build-kg-spike-pg.yml` (self-hosted homelab runner,
buildx amd64+arm64) to `acrmcit.azurecr.io/kg-spike-pg:pg16-age-vector`.
Manual: Actions → "Build kg-spike-pg (multi-arch)" → Run workflow.

## Schema

`01-init-extensions.sql` creates `age` + `vector` on init. The full KG schema
(`kg_entity`/`kg_edge`/`kg_xref` + indexes) is applied by the infra-tf schema
ConfigMap mounted into `/docker-entrypoint-initdb.d` alongside this file.
