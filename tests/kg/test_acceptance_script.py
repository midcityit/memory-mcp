import importlib.util
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from memory_mcp.kg.query import KGQuery
from memory_mcp.kg.registry import TypeDef
from memory_mcp.kg.rest import build_router

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("kg_acceptance", ROOT / "scripts" / "kg_acceptance.py")
acc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(acc)

ENV = {
    "CF_ACCOUNT_ID": "0123456789ABCDEF0123456789abcdef",
    "MCIT_APPI_ID": "/subscriptions/00000000-0000-0000-0000-00000000000a/resourceGroups/rg-mon/providers/Microsoft.Insights/components/appi-memory-twin-mcit",
    "CCTECH_KV_ID": "/subscriptions/00000000-0000-0000-0000-00000000000a/resourceGroups/rg-kv/providers/Microsoft.KeyVault/vaults/cctech-keyvault",
    "VTV_AKS_ID": "/subscriptions/00000000-0000-0000-0000-00000000000b/resourceGroups/RG-AKS/providers/Microsoft.ContainerService/managedClusters/aks-vtv-prod",
    "MCIT_GATEWAY_NS": "envoy-gateway-system",
    "MCIT_GATEWAY_NAME": "eg",
    "MS01_QDRANT_PV": "qdrant-data",
    "MCIT_AMW_ID": "/subscriptions/00000000-0000-0000-0000-00000000000a/resourceGroups/rg-mon/providers/Microsoft.Monitor/accounts/amw-mcit",
    "MCIT_OTEL_DEPLOYMENT": "otel-collector",
    "GRAFANA_HOST": "Grafana.ChrisCastroTech.com",
    "GRAFANA_AMW_DS_UID": "amw-mcit-prom",
    "GRAFANA_DASHBOARD_UID": "memory-mcp-overview",
}


def test_render_seed_lowercases_derived_vars():
    text = acc.render_seed("vtv", ENV)
    assert "azure:/subscriptions/00000000-0000-0000-0000-00000000000b/resourcegroups/rg-aks/" in text
    assert "${" not in text


def test_full_acceptance_in_process(svc, kg_registry, gstore):
    for t, kind in [("Microsoft.Insights/components", "observability"), ("Microsoft.KeyVault/vaults", "secret"),
                    ("Microsoft.Monitor/accounts", "observability")]:
        kg_registry.types["azure"][t] = TypeDef("azure", t, kind)
    kg_registry.types["kubernetes"]["core/PersistentVolume"] = TypeDef("kubernetes", "core/PersistentVolume", "storage")
    kg_registry._build_type_index()
    app = FastAPI()
    app.include_router(build_router(svc, KGQuery(kg_registry, gstore), lambda: None))
    results = acc.run(TestClient(app), ENV, check_latency=False)
    failed = [r for r in results if not r[1]]
    assert not failed, failed
    assert len(results) == 9
