import pytest
from memory_mcp.kg.ids import normalize_native_id
from memory_mcp.kg.models import KGError
from memory_mcp.kg.registry import TypeDef

AKS = TypeDef("azure", "Microsoft.ContainerService/managedClusters", "container")
VM = TypeDef("azure", "Microsoft.Compute/virtualMachines", "compute")
SUBNET = TypeDef("azure", "Microsoft.Network/virtualNetworks/subnets", "network")
RG = TypeDef("azure", "Microsoft.Resources/resourceGroups", "org")
EC2 = TypeDef("aws", "AWS::EC2::Instance", "compute")
ALB = TypeDef("aws", "AWS::ElasticLoadBalancingV2::LoadBalancer", "network")
GCE = TypeDef("gcp", "compute.googleapis.com/Instance", "compute")
DEPLOY = TypeDef("kubernetes", "apps/Deployment", "container")
CF_DNS = TypeDef("cloudflare", "dns_record", "dns")
HV_VM = TypeDef("hyperv", "VM", "compute")
VS_VM = TypeDef("vmware", "VirtualMachine", "compute")
GF_DASH = TypeDef("grafana", "Dashboard", "observability")
PROM_JOB = TypeDef("prometheus", "ScrapeJob", "observability")
NB_DEV = TypeDef("netbox", "dcim.device", "inventory")
JIRA = TypeDef("logical", "jira_issue", "work")
REPO = TypeDef("logical", "repo", "work")
SUB = "/subscriptions/00000000-0000-0000-0000-000000000001"


def test_azure_case_and_trailing_slash_normalize():
    a = normalize_native_id("azure", AKS, f"{SUB}/resourceGroups/RG-1/providers/Microsoft.ContainerService/managedClusters/AKS-VTV-PROD/")
    b = normalize_native_id("azure", AKS, f"{SUB}/resourcegroups/rg-1/providers/microsoft.containerservice/managedclusters/aks-vtv-prod")
    assert a == b == b.lower()


def test_azure_child_and_scope_types():
    normalize_native_id("azure", SUBNET, f"{SUB}/resourceGroups/rg/providers/Microsoft.Network/virtualNetworks/v1/subnets/s1")
    normalize_native_id("azure", RG, f"{SUB}/resourceGroups/rg")


def test_azure_type_id_mismatch():
    nic = f"{SUB}/resourceGroups/rg/providers/Microsoft.Network/networkInterfaces/nic1"
    with pytest.raises(KGError) as e:
        normalize_native_id("azure", VM, nic)
    assert e.value.code == "type_id_mismatch" and "microsoft.network/networkinterfaces" in e.value.message


@pytest.mark.parametrize("bad", ["aks-vtv-prod", "/subscriptions/x/providers/Microsoft.Compute/virtualMachines",
                                 f"{SUB}/resourceGroups/rg/providers/Microsoft.Compute"])
def test_azure_invalid(bad):
    with pytest.raises(KGError) as e:
        normalize_native_id("azure", VM, bad)
    assert e.value.code in ("invalid_native_id", "type_id_mismatch")


def test_aws_arn_and_service_mapping():
    assert normalize_native_id("aws", EC2, "arn:aws:ec2:us-west-2:891377069618:instance/i-0abc") == \
        "arn:aws:ec2:us-west-2:891377069618:instance/i-0abc"
    normalize_native_id("aws", ALB, "arn:aws:elasticloadbalancing:us-west-2:891377069618:loadbalancer/app/x/123")
    with pytest.raises(KGError) as e:
        normalize_native_id("aws", EC2, "arn:aws:s3:::bucket")
    assert e.value.code == "type_id_mismatch"
    with pytest.raises(KGError):
        normalize_native_id("aws", EC2, "i-0abc")


def test_gcp():
    nid = "//compute.googleapis.com/projects/p/zones/us-west1-a/instances/vm1"
    assert normalize_native_id("gcp", GCE, nid) == nid
    with pytest.raises(KGError) as e:
        normalize_native_id("gcp", GCE, "//storage.googleapis.com/projects/_/buckets/b")
    assert e.value.code == "type_id_mismatch"


def test_kubernetes():
    nid = "mcit-k8s/digital-twin/apps/Deployment/memory-mcp"
    assert normalize_native_id("kubernetes", DEPLOY, nid) == nid
    with pytest.raises(KGError) as e:
        normalize_native_id("kubernetes", DEPLOY, "mcit-k8s/digital-twin/apps/StatefulSet/qdrant")
    assert e.value.code == "type_id_mismatch"
    with pytest.raises(KGError):
        normalize_native_id("kubernetes", DEPLOY, "mcit-k8s/memory-mcp")


def test_cloudflare_hyperv_vmware():
    acct = "0123456789abcdef0123456789ABCDEF"
    assert normalize_native_id("cloudflare", CF_DNS, f"{acct}/chriscastrotech.com/dns_record/memory-mcp") == \
        f"{acct.lower()}/chriscastrotech.com/dns_record/memory-mcp"
    assert normalize_native_id("hyperv", HV_VM, "MS01.int.example.net/VM/opi-5") == "ms01.int.example.net/VM/opi-5"
    with pytest.raises(KGError):
        normalize_native_id("hyperv", HV_VM, "ms01.int.example.net/VMSwitch/ext")
    assert normalize_native_id("vmware", VS_VM, "VC01.corp.local/DC1/vm/web01") == "vc01.corp.local/DC1/vm/web01"


def test_grafana_prometheus_netbox():
    assert normalize_native_id("grafana", GF_DASH, "Grafana.ChrisCastroTech.com/Dashboard/memory-mcp-overview") == \
        "grafana.chriscastrotech.com/Dashboard/memory-mcp-overview"
    with pytest.raises(KGError) as e:
        normalize_native_id("grafana", GF_DASH, "grafana.chriscastrotech.com/Datasource/amw")
    assert e.value.code == "type_id_mismatch"
    assert normalize_native_id("prometheus", PROM_JOB, "prometheus.monitoring.svc/ScrapeJob/memory-mcp") == \
        "prometheus.monitoring.svc/ScrapeJob/memory-mcp"
    assert normalize_native_id("netbox", NB_DEV, "NetBox.example.net/dcim.device/42") == "netbox.example.net/dcim.device/42"
    with pytest.raises(KGError) as e:
        normalize_native_id("netbox", NB_DEV, "netbox.example.net/dcim.device/web01")
    assert e.value.code == "invalid_native_id"
    with pytest.raises(KGError) as e:
        normalize_native_id("netbox", NB_DEV, "netbox.example.net/ipam.prefix/7")
    assert e.value.code == "type_id_mismatch"


def test_logical():
    assert normalize_native_id("logical", JIRA, "MCIT-193") == "MCIT-193"
    assert normalize_native_id("logical", REPO, "midcityit/memory-mcp") == "midcityit/memory-mcp"
    for bad, td in [("mcit 193", JIRA), ("memory-mcp", REPO), ("  ", JIRA)]:
        with pytest.raises(KGError):
            normalize_native_id("logical", td, bad)
