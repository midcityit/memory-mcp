# kg-spike-pg — Postgres 16 + Apache AGE 1.5.0 + pgvector 0.8.7, baked in and
# auto-created on init. MULTI-ARCH (linux/amd64 + linux/arm64).
#
# RCL-64 P0: the original spike image was built ad-hoc and shipped amd64-only,
# which cannot schedule on the prod arm64 pool (opi-5). This is the
# source-controlled, genuinely multi-arch rebuild.
#
# IMPORTANT (the TARGETARCH trap): do NOT give `ARG TARGETARCH` a default. Under
# buildx, TARGETARCH is injected per-platform automatically; a default value
# SHADOWS buildx's per-platform value and silently bakes one arch for all
# platforms (that is exactly how the previous image became amd64-only while the
# build claimed multi-arch). We declare it with no default and never hardcode an
# arch below — both extensions compile from source for the build's native arch.
FROM postgres:18-bookworm@sha256:ece55f7868da684e400f82f1030a050718adbdd65c23aa500c486f6e2950dc0f

# buildx injects these per target platform. NO DEFAULTS.
ARG TARGETARCH
ARG TARGETOS

ARG AGE_VERSION=PG16/v1.5.0-rc0
ARG PGVECTOR_VERSION=v0.8.7

# Build deps for compiling the two extensions from source (arch-independent).
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        build-essential git ca-certificates \
        postgresql-server-dev-16 \
        bison flex \
        libreadline-dev zlib1g-dev; \
    rm -rf /var/lib/apt/lists/*

# Apache AGE 1.5.0 (graph) — compiled for the native arch of this build stage.
RUN set -eux; \
    # AGE tags carry the PG major as a prefix (e.g. PG16/v1.5.0-rc0): clone the
    # FULL tag. No fallback to master: master targets a newer PG and does not
    # compile against PG16, so a bad tag must fail the build loudly.
    git clone --depth 1 --branch "${AGE_VERSION}" https://github.com/apache/age.git /tmp/age; \
    cd /tmp/age; \
    make PG_CONFIG=/usr/lib/postgresql/16/bin/pg_config; \
    make install PG_CONFIG=/usr/lib/postgresql/16/bin/pg_config; \
    rm -rf /tmp/age

# pgvector 0.8.7 (vectors) — compiled for the native arch of this build stage.
RUN set -eux; \
    git clone --depth 1 --branch "${PGVECTOR_VERSION}" https://github.com/pgvector/pgvector.git /tmp/pgvector; \
    cd /tmp/pgvector; \
    make PG_CONFIG=/usr/lib/postgresql/16/bin/pg_config; \
    make install PG_CONFIG=/usr/lib/postgresql/16/bin/pg_config; \
    rm -rf /tmp/pgvector

# Trim build toolchain to keep the runtime image lean (extensions already built).
RUN set -eux; \
    apt-get purge -y --auto-remove build-essential git bison flex \
        postgresql-server-dev-16 libreadline-dev zlib1g-dev; \
    rm -rf /var/lib/apt/lists/*

# Schema bootstrap: age + vector are created and the KG schema initialised on a
# fresh data volume. Mirrors infra-tf crp-kg2-postgres schema ConfigMap and
# spikes/rcl44-kg2.0/01_schema.sql.
COPY 01-init-extensions.sql /docker-entrypoint-initdb.d/01-init-extensions.sql

# Sanity: fail the build if either extension did not install for THIS arch.
RUN set -eux; \
    ls /usr/lib/postgresql/16/lib/age.so; \
    ls /usr/lib/postgresql/16/lib/vector.so
