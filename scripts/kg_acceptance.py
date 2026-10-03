#!/usr/bin/env python3
"""Seed the mcit/vtv acceptance topology over REST and run the 9 spec §10 checks.

Env: KG_BASE_URL, MEMORY_TWIN_BEARER, plus the seed variables listed in REQUIRED. Exit 0 iff all checks pass.
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
from pathlib import Path
from string import Template
from typing import Mapping
from urllib.parse import quote

import httpx

SEED_DIR = Path(__file__).resolve().parent / "kg_seed"
REQUIRED = ("CF_ACCOUNT_ID", "MCIT_APPI_ID", "CCTECH_KV_ID", "VTV_AKS_ID", "MCIT_GATEWAY_NS",
            "MCIT_GATEWAY_NAME", "MS01_QDRANT_PV", "MCIT_AMW_ID", "MCIT_OTEL_DEPLOYMENT", "GRAFANA_HOST",
            "GRAFANA_AMW_DS_UID", "GRAFANA_DASHBOARD_UID")


def _vars(env: Mapping[str, str]) -> dict[str, str]:
    missing = [k for k in REQUIRED if not env.get(k)]
    if missing:
        raise SystemExit(f"missing env: {', '.join(missing)}")
    v = {k: env[k] for k in REQUIRED}
    v["CF_ACCOUNT_ID_LC"] = env["CF_ACCOUNT_ID"].lower()
    for k in ("MCIT_APPI_ID", "CCTECH_KV_ID", "VTV_AKS_ID", "MCIT_AMW_ID"):
        v[f"{k}_LC"] = env[k].lower().rstrip("/")
    v["GRAFANA_HOST_LC"] = env["GRAFANA_HOST"].lower()
    return v


def render_seed(name: str, env: Mapping[str, str]) -> str:
    return Template((SEED_DIR / f"{name}.jsonl.tmpl").read_text()).substitute(_vars(env))


def _keys(nodes) -> set[str]:
    return {n["key"] for n in nodes}


def run(client: httpx.Client, env: Mapping[str, str], check_latency: bool = True) -> list[tuple[str, bool, str]]:
    v = _vars(env)
    cf = v["CF_ACCOUNT_ID_LC"]
    DNS = f"cloudflare:{cf}/chriscastrotech.com/dns_record/memory-mcp"
    TUN = f"cloudflare:{cf}/-/zero_trust_tunnel_cloudflared/homelab"
    GW = f"kubernetes:mcit-k8s/{v['MCIT_GATEWAY_NS']}/gateway.networking.k8s.io/Gateway/{v['MCIT_GATEWAY_NAME']}"
    DEP = "kubernetes:mcit-k8s/digital-twin/apps/Deployment/memory-mcp"
    OPI = "kubernetes:mcit-k8s/_cluster/core/Node/opi-5"
    MS01SVC = "kubernetes:ms01-k8s/digital-twin/core/Service/memory-mcp"
    PV = f"kubernetes:ms01-k8s/_cluster/core/PersistentVolume/{v['MS01_QDRANT_PV']}"
    AKS = f"azure:{v['VTV_AKS_ID_LC']}"
    AMW = f"azure:{v['MCIT_AMW_ID_LC']}"
    DS = f"grafana:{v['GRAFANA_HOST_LC']}/Datasource/{v['GRAFANA_AMW_DS_UID']}"
    DASH = f"grafana:{v['GRAFANA_HOST_LC']}/Dashboard/{v['GRAFANA_DASHBOARD_UID']}"
    out: list[tuple[str, bool, str]] = []

    def check(name, fn):
        try:
            ok, detail = fn()
        except Exception as e:  # report, don't abort the remaining checks
            ok, detail = False, f"{type(e).__name__}: {e}"
        out.append((name, bool(ok), detail))

    def imp(graph):
        r = client.post(f"/kg/{graph}/import", content=render_seed(graph, env))
        r.raise_for_status()
        return r.json()

    def export_count(graph):
        return sum(1 for ln in client.get(f"/kg/{graph}/export").text.splitlines() if ln.strip())

    seeded = [imp("mcit"), imp("vtv")]
    if any(s["status"] != "ok" for s in seeded) or seeded[1]["xrefs"]["errors"]:
        raise SystemExit(f"seed failed: {json.dumps(seeded)[:2000]}")

    def c1():
        r = client.post("/kg/mcit/impact", json={"key": OPI}).json()
        aff = {n["key"] for layer in r["affected"] for n in layer["nodes"]}
        return DEP in aff and DNS in aff, f"affected={len(aff)}"
    check("1 impact(opi-5) reaches deployment and public hostname", c1)

    def c2():
        r = client.post("/kg/mcit/path", json={"src": DNS, "dst": DEP}).json()
        nodes = [n["key"] for n in r["paths"][0]["nodes"]] if r["found"] else []
        return nodes[:3] == [DNS, TUN, GW] and nodes[-1] == DEP and len(nodes) == 6, " -> ".join(n.split("/")[-1] for n in nodes)
    check("2 path(hostname -> deployment)", c2)

    def c3():
        body = {"start": TUN, "direction": "out", "max_depth": 1}
        past = _keys(client.post("/kg/mcit/traverse", json={**body, "as_of": "2026-09-20"}).json()["nodes"])
        now = _keys(client.post("/kg/mcit/traverse", json=body).json()["nodes"])
        return MS01SVC in past and GW not in past and GW in now and MS01SVC not in now, "as_of 2026-09-20 vs now"
    check("3 as_of shows ms01 routing before MCIT-193 cutover", c3)

    def c4():
        r = client.post("/kg/mcit/impact", json={"key": PV}).json()
        aff = {n["key"] for layer in r["affected"] for n in layer["nodes"]}
        bad = {k for k in aff if "mcit-k8s/" in k} | ({DNS} & aff)
        return bool(aff) and not bad, f"affected={sorted(k.split('/')[-1] for k in aff)}"
    check("4 ms01 PV blast radius is ms01-only (MCIT-251 evidence)", c4)

    def c5():
        r = client.post("/kg/vtv/impact", json={"key": AKS})  # warm-up / sanity: AKS exists
        r.raise_for_status()
        rel = client.get(f"/kg/vtv/entities/{quote('logical:VTV-238', safe='')}").json()
        dsts = {x["dst"] for x in rel["xrefs"]}
        return "mcit::logical:MCIT-184" in dsts, f"xrefs={sorted(dsts)}"
    check("5 VTV-238 references MCIT-184 pattern", c5)

    def c6():
        bad = json.dumps({"record": "entity", "provider": "azure", "type": "Microsoft.Compute/virtualMachines",
                          "native_id": env["MCIT_APPI_ID"].rsplit("/providers/", 1)[0]
                          + "/providers/Microsoft.Network/networkInterfaces/nic-acceptance"})
        r = client.post("/kg/mcit/import", content=bad)
        mismatch = r.status_code == 422 and r.json()["detail"]["errors"][0]["error"] == "type_id_mismatch"
        t = client.get(f"/kg/vtv/entities/{quote(AKS, safe='')}").json()["entity"]["type"]
        return mismatch and t == "Microsoft.ContainerService/managedClusters", f"alias->{t}"
    check("6 type/ID mismatch rejected; TF alias resolved", c6)

    def c7():
        before = (export_count("mcit"), export_count("vtv"))
        again = [imp("mcit"), imp("vtv")]
        statuses = {w["status"] for s in again for w in s["written"]}
        after = (export_count("mcit"), export_count("vtv"))
        return statuses <= {"updated", "retired", "recorded"} and before == after, f"statuses={sorted(statuses)} lines={after}"
    check("7 re-seed is idempotent", c7)

    def c8():
        if not check_latency:
            return True, "skipped (in-process)"
        samples = []
        for _ in range(30):
            t0 = time.perf_counter()
            client.post("/kg/mcit/traverse", json={"start": DEP, "max_depth": 3}).raise_for_status()
            samples.append((time.perf_counter() - t0) * 1000)
        p95 = statistics.quantiles(samples, n=20)[18]
        return p95 < 150, f"p95={p95:.1f}ms (RSS: check `kubectl top pod` separately)"
    check("8 traverse depth-3 p95 < 150 ms", c8)

    def c9():
        r = client.post("/kg/mcit/impact", json={"key": AMW}).json()
        depth = {n["key"]: layer["depth"] for layer in r["affected"] for n in layer["nodes"]}
        return depth.get(DS) == 1 and depth.get(DASH) == 2, f"affected={len(depth)}"
    check("9 impact(amw-mcit) reaches Grafana datasource and dashboard", c9)
    return out


def main() -> int:
    base, token = os.environ.get("KG_BASE_URL"), os.environ.get("MEMORY_TWIN_BEARER")
    if not base or not token:
        print("set KG_BASE_URL and MEMORY_TWIN_BEARER", file=sys.stderr)
        return 2
    with httpx.Client(base_url=base, headers={"Authorization": f"Bearer {token}"}, timeout=30) as c:
        results = run(c, os.environ)
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}  [{detail}]")
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(main())
