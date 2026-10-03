import hashlib
import math

import pytest
from qdrant_client import QdrantClient

DIM = 384


class StubEmbedder:
    """Deterministic, model-free embedder: same text -> same unit vector.
    Texts sharing words get similar vectors (bag of hashed tokens)."""
    dim = DIM

    def embed(self, text: str) -> list[float]:
        v = [0.0] * DIM
        for tok in text.lower().replace("/", " ").replace("-", " ").split():
            h = int(hashlib.sha256(tok.encode()).hexdigest(), 16)
            v[h % DIM] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]


@pytest.fixture
def stub_embedder():
    return StubEmbedder()


@pytest.fixture
def qdrant():
    c = QdrantClient(":memory:")
    yield c
    c.close()


from memory_mcp.kg.registry import Registry, TypeDef


@pytest.fixture
def kg_registry():
    """Packaged registry plus a few azure/cloudflare types, so tests don't depend on generated catalogs."""
    reg = Registry.load()
    for t, kind, aliases in [
        ("Microsoft.ContainerService/managedClusters", "container", ("azurerm_kubernetes_cluster",)),
        ("Microsoft.Compute/virtualMachines", "compute", ("azurerm_linux_virtual_machine",)),
        ("Microsoft.Network/networkInterfaces", "network", ()),
    ]:
        reg.types.setdefault("azure", {})[t] = TypeDef("azure", t, kind, aliases)
    for t, kind in [("dns_record", "dns"), ("zero_trust_tunnel_cloudflared", "network")]:
        reg.types.setdefault("cloudflare", {})[t] = TypeDef("cloudflare", t, kind, (f"cloudflare_{t}",))
    reg._build_type_index()
    return reg


@pytest.fixture
def gstore(qdrant, stub_embedder, kg_registry):
    from memory_mcp.kg.store import QdrantGraphStore
    s = QdrantGraphStore(qdrant, stub_embedder)
    for g in kg_registry.graphs:
        s.ensure_graph(g)
    s.ensure_xrefs()
    return s


@pytest.fixture
def svc(kg_registry, gstore):
    from memory_mcp.kg.service import KGService
    return KGService(kg_registry, gstore)
