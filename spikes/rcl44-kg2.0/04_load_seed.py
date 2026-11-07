#!/usr/bin/env python3
"""RCL-46 seed loader: load the mcit acceptance topology into BOTH backends.

The traverse benchmark measures graph-walk latency over the topology (nodes +
edges), which is independent of the literal native_id values. So we substitute
the template's ${VAR} placeholders with DETERMINISTIC SYNTHETIC ids (stable, no
prod secrets) — the graph shape, edge count and depth profile are identical to
the real acceptance seed.

Loads into:
  * relational kg_entity / kg_edge  (for the recursive-CTE prototype)
  * AGE graph 'mcit'  :Entity vertices + typed edges  (for the AGE prototype)

Embeddings: real all-MiniLM-L6-v2 if sentence-transformers is importable, else a
deterministic hash vector (fine for traverse; resolve-parity needs the real one).
"""
from __future__ import annotations
import hashlib, json, os, re, struct, sys
from pathlib import Path

import psycopg

GRAPH = "mcit"
SEED = Path(__file__).resolve().parents[2] / "scripts" / "kg_seed" / "mcit.jsonl.tmpl"
PG_DSN = os.environ["PG_DSN"]

# Deterministic synthetic values for every ${VAR} in the template. Stable across
# runs; shape-preserving; no real resource IDs.
VARS = {
    "CF_ACCOUNT_ID": "CF00000000000000000000000000000001",
    "MCIT_APPI_ID": "/subscriptions/s1/rg/mcit/providers/microsoft.insights/components/appi-mcit",
    "CCTECH_KV_ID": "/subscriptions/s1/rg/mcit/providers/microsoft.keyvault/vaults/cctech-keyvault",
    "VTV_AKS_ID": "/subscriptions/s1/rg/vtv/providers/microsoft.containerservice/managedclusters/vtv-aks",
    "MCIT_GATEWAY_NS": "envoy-gateway-system",
    "MCIT_GATEWAY_NAME": "mcit-gateway",
    "MCIT_OTEL_DEPLOYMENT": "otel-collector",
    "MCIT_AMW_ID": "/subscriptions/s1/rg/mcit/providers/microsoft.monitor/accounts/amw-mcit",
    "MS01_QDRANT_PV": "pv-qdrant-ms01",
    "GRAFANA_HOST": "grafana.example.test",
    "GRAFANA_AMW_DS_UID": "amwdsuid0001",
    "GRAFANA_DASHBOARD_UID": "dashuid0001",
}
# _LC variants = lowercase of the base
for k in list(VARS):
    VARS[f"{k}_LC"] = VARS[k].lower()


def subst(line: str) -> str:
    return re.sub(r"\$\{([A-Z_]+)\}", lambda m: VARS.get(m.group(1), m.group(1)), line)


# ── embedder ──────────────────────────────────────────────────────────────────
def get_embed():
    try:
        from sentence_transformers import SentenceTransformer
        m = SentenceTransformer("all-MiniLM-L6-v2")
        return lambda t: m.encode(t, normalize_embeddings=True).tolist(), "minilm"
    except Exception:
        def h(t: str):
            # deterministic 384-dim unit-ish vector from a hash stream
            b = hashlib.sha256(t.encode()).digest()
            raw = (b * (384 * 4 // len(b) + 1))[: 384 * 4]
            v = list(struct.unpack("384f", raw))
            n = sum(x * x for x in v) ** 0.5 or 1.0
            return [x / n for x in v]
        return h, "hash-fallback"


def main():
    embed, embedder_kind = get_embed()
    entities, edges = [], []
    for line in SEED.read_text().splitlines():
        line = subst(line.strip())
        if not line:
            continue
        rec = json.loads(line)
        if rec["record"] == "entity":
            key = f'{rec["provider"]}:{rec["native_id"]}'
            txt = f'{rec["display_name"]} {" ".join(rec.get("aliases", []))} {rec["type"]}'
            entities.append((GRAPH, key, rec["provider"], rec["type"],
                             rec.get("kind", rec["provider"]), rec["native_id"], rec["display_name"],
                             rec.get("aliases", []), json.dumps(rec.get("properties", {})),
                             rec.get("memory_ids", []), embed(txt),
                             rec.get("valid_from") or "now", rec.get("valid_to")))
        elif rec["record"] == "edge":
            edges.append((GRAPH, rec["src"], rec["relation"], rec["dst"],
                          json.dumps(rec.get("properties", {})), rec.get("evidence_memory_ids", []),
                          rec.get("valid_from") or "now", rec.get("valid_to"), rec.get("retire_reason")))

    with psycopg.connect(PG_DSN, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("TRUNCATE kg_entity, kg_edge, kg_xref;")
        for e in entities:
            vf = "now()" if e[11] == "now" else "%s"
            cur.execute(
                f"""INSERT INTO kg_entity
                    (graph,key,provider,type,kind,native_id,display_name,aliases,properties,
                     memory_ids,embedding,valid_from,valid_to)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,{vf},%s)""",
                e[:11] + ((e[12],) if e[11] == "now" else (e[11], e[12])),
            )
        for g in edges:
            vf = "now()" if g[6] == "now" else "%s"
            cur.execute(
                f"""INSERT INTO kg_edge
                    (graph,src,relation,dst,properties,evidence_memory_ids,valid_from,valid_to,retire_reason)
                    VALUES (%s,%s,%s,%s,%s::jsonb,%s,{vf},%s,%s)""",
                g[:6] + ((g[7], g[8]) if g[6] == "now" else (g[6], g[7], g[8])),
            )
        ec = cur.execute("SELECT count(*) FROM kg_entity").fetchone()[0]
        gc = cur.execute("SELECT count(*) FROM kg_edge").fetchone()[0]

        # AGE graph: vertices + edges (current rows; vt carried as prop for temporal filter)
        cur.execute("LOAD 'age'; SET search_path = ag_catalog, public;")
        try:
            cur.execute("SELECT create_graph('mcit');")
        except Exception:
            pass
        for e in entities:
            key = e[1].replace("'", "''")
            cur.execute(
                "SELECT * FROM cypher('mcit', $$ MERGE (:Entity {key:'%s', kind:'%s', vt:%s}) $$) AS (v agtype);"
                % (key, e[4], "null")
            )
        for g in edges:
            s = g[1].replace("'", "''"); d = g[3].replace("'", "''"); rel = g[2]
            if g[7]:  # retired edge: skip from current AGE graph
                continue
            cur.execute(
                "SELECT * FROM cypher('mcit', $$ MATCH (a:Entity {key:'%s'}),(b:Entity {key:'%s'}) "
                "CREATE (a)-[:REL {relation:'%s'}]->(b) $$) AS (e agtype);" % (s, d, rel)
            )

    print(json.dumps({"embedder": embedder_kind, "entities": ec, "edges": gc,
                      "age_graph": "mcit"}, indent=2))


if __name__ == "__main__":
    main()
