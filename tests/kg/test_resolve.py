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


def test_logical_identity_and_infra_types(reg):
    # Entra/Graph-only objects + endpoints + fleet, added to close the KG
    # unsupported-types tracker gaps (service principals had no representation).
    assert reg.resolve_type("logical", "service_principal").kind == "identity"
    assert reg.resolve_type("logical", "servicePrincipal").type == "service_principal"
    assert reg.resolve_type("logical", "sp").type == "service_principal"
    assert reg.resolve_type("logical", "app_registration").kind == "identity"
    assert reg.resolve_type("logical", "directory_role").kind == "identity"
    assert reg.resolve_type("logical", "group").kind == "identity"
    assert reg.resolve_type("logical", "device").kind == "compute"
    assert reg.resolve_type("logical", "fleet").kind == "org"
    # "ticket" alias implied by the tool description ("Jira key") now resolves.
    assert reg.resolve_type("logical", "ticket").type == "jira_issue"


def test_virtualized_on_host_on_host(reg):
    # RCL-55: VM / k8s-Node (kind host) on a hypervisor host was unmodellable
    # because runs_on/hosted_on only accept compute|container sources.
    rel = reg.relation("virtualized_on")
    assert rel.inverse == "virtualizes" and rel.impact == "propagates"
    reg.check_kinds(rel, "host", "host")       # k8s Node / Hyper-V VM on hypervisor
    reg.check_kinds(rel, "compute", "host")    # a VM compute entity on hypervisor
    with pytest.raises(KGError) as e:
        reg.check_kinds(rel, "network", "host")
    assert e.value.code == "kind_constraint"
