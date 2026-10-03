import pytest
from memory_mcp.kg.models import KGError
from memory_mcp.kg.registry import Registry


@pytest.fixture(scope="module")
def reg():
    return Registry.load()


def test_exact_alias_and_case_insensitive(reg):
    assert reg.resolve_type("kubernetes", "apps/Deployment").kind == "container"
    assert reg.resolve_type("kubernetes", "kubernetes_deployment_v1").type == "apps/Deployment"
    assert reg.resolve_type("kubernetes", "APPS/deployment").type == "apps/Deployment"
    assert reg.resolve_type("azure", "azurerm_kubernetes_cluster").type == "Microsoft.ContainerService/managedClusters"
    assert reg.resolve_type("logical", "jira_issue").kind == "work"


def test_k8s_crd_pattern_accepted(reg):
    td = reg.resolve_type("kubernetes", "trident.qnap.io/TridentBackend")
    assert (td.type, td.kind) == ("trident.qnap.io/TridentBackend", "container")


def test_unknown_type_suggests(reg):
    with pytest.raises(KGError) as e:
        reg.resolve_type("kubernetes", "apps/Deploymnet")
    assert e.value.code == "unknown_type" and "apps/Deployment" in e.value.suggestions
    with pytest.raises(KGError):
        reg.resolve_type("kubernetes", "gateway.networking.k8s.io/HTTPRoutee")  # known group, unknown kind


def test_unknown_provider_before_type(reg):
    with pytest.raises(KGError) as e:
        reg.resolve_type("azzure", "x")
    assert e.value.code == "unknown_provider"


def test_check_kinds(reg):
    reg.check_kinds(reg.relation("hosted_on"), "compute", "host")
    reg.check_kinds(reg.relation("member_of"), "anything", "else")  # unconstrained
    with pytest.raises(KGError) as e:
        reg.check_kinds(reg.relation("tracked_in"), "container", "network")
    assert e.value.code == "kind_constraint"
