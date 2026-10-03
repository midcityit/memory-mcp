import importlib.util
import json
from pathlib import Path

import pytest

from memory_mcp.kg.registry import KINDS, PROVIDERS, Registry

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("build_kg_catalog", ROOT / "scripts" / "build_kg_catalog.py")
bkc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bkc)


@pytest.mark.parametrize("provider,typ,kind", [
    ("azure", "Microsoft.ContainerService/managedClusters", "container"),
    ("azure", "Microsoft.Network/networkSecurityGroups", "security"),
    ("azure", "Microsoft.Network/dnszones", "dns"),
    ("azure", "Microsoft.Network/virtualNetworks/subnets", "network"),
    ("azure", "Microsoft.KeyVault/vaults", "secret"),
    ("azure", "Microsoft.Insights/components", "observability"),
    ("azure", "Microsoft.Resources/resourceGroups", "org"),
    ("aws", "AWS::EC2::SecurityGroup", "security"),
    ("aws", "AWS::EC2::TransitGateway", "network"),
    ("aws", "AWS::EC2::Instance", "compute"),
    ("aws", "AWS::RDS::DBInstance", "database"),
    ("aws", "AWS::Route53::HostedZone", "dns"),
    ("aws", "AWS::APS::Workspace", "observability"),
    ("aws", "AWS::Grafana::Workspace", "observability"),
    ("aws", "AWS::CloudWatch::Alarm", "observability"),
    ("azure", "Microsoft.Monitor/accounts", "observability"),
    ("azure", "Microsoft.Dashboard/grafana", "observability"),
    ("azure", "Microsoft.AlertsManagement/prometheusRuleGroups", "observability"),
    ("gcp", "compute.googleapis.com/Firewall", "security"),
    ("gcp", "compute.googleapis.com/Instance", "compute"),
    ("gcp", "container.googleapis.com/Cluster", "container"),
    ("cloudflare", "dns_record", "dns"),
    ("cloudflare", "zero_trust_tunnel_cloudflared", "network"),
    ("cloudflare", "zero_trust_access_application", "security"),
    ("cloudflare", "zone", "org"),
    ("azure", "Microsoft.Datadog/monitors", "observability"),
    ("azure", "Microsoft.SignalRService/signalR", "messaging"),
    ("azure", "Microsoft.AVS/privateClouds", "host"),
    ("azure", "Microsoft.CostManagement/exports", "org"),
    ("azure", "PaloAltoNetworks.Cloudngfw/firewalls", "security"),
    ("aws", "AWS::InspectorV2::Filter", "security"),
    ("aws", "AWS::CertificateManager::Certificate", "security"),
    ("aws", "AWS::ServiceDiscovery::Service", "dns"),
    ("aws", "AWS::SES::EmailIdentity", "messaging"),
    ("aws", "AWS::CodePipeline::Pipeline", "work"),
    ("aws", "AWS::SSM::Document", "integration"),
    ("aws", "AWS::WorkSpaces::Workspace", "compute"),
    ("gcp", "iap.googleapis.com/IapSettings", "security"),
    ("gcp", "batch.googleapis.com/Job", "compute"),
    ("gcp", "dataplex.googleapis.com/Lake", "analytics"),
    ("azure", "Contoso.Unknown/widgets", "other"),
])
def test_classify_kind(provider, typ, kind):
    assert bkc.classify_kind(provider, typ) == kind


def test_loaders():
    az = [{"namespace": "Microsoft.Compute", "resourceTypes": [{"resourceType": "virtualMachines"}, {"resourceType": "disks"}]}]
    assert bkc.load_azure(az) == ["Microsoft.Compute/disks", "Microsoft.Compute/virtualMachines"]
    assert bkc.load_aws_spec({"ResourceTypes": {"AWS::S3::Bucket": {}, "AWS::EC2::VPC": {}}}) == ["AWS::EC2::VPC", "AWS::S3::Bucket"]
    assert bkc.load_lines("# c\ncompute.googleapis.com/Instance\n\n") == ["compute.googleapis.com/Instance"]
    schema = {"provider_schemas": {"registry.terraform.io/cloudflare/cloudflare": {"resource_schemas": {"cloudflare_zone": {}, "cloudflare_dns_record": {}}}}}
    assert bkc.load_tf_schema(schema, "cloudflare_") == ["dns_record", "zone"]


def test_build_catalog_attaches_aliases_and_extras():
    cat = bkc.build_catalog("azure", ["Microsoft.ContainerService/managedClusters"],
                            {"azurerm_kubernetes_cluster": "Microsoft.ContainerService/managedClusters"})
    t = {x["type"]: x for x in cat["types"]}
    assert t["Microsoft.ContainerService/managedClusters"]["aliases"] == ["azurerm_kubernetes_cluster"]
    # ARM scope types are always present, even though `az provider list` does not report them
    assert "Microsoft.Resources/subscriptions" in t and "Microsoft.Resources/resourceGroups" in t


def test_build_catalog_cloudflare_auto_alias():
    cat = bkc.build_catalog("cloudflare", ["zone"], {})
    assert cat["types"][0]["aliases"] == ["cloudflare_zone"]


def test_committed_catalogs_are_valid():
    cat_dir = ROOT / "src" / "memory_mcp" / "kg" / "catalog"
    for f in cat_dir.glob("*.json"):
        doc = json.loads(f.read_text())
        assert doc["provider"] in PROVIDERS, f
        assert doc["types"], f
        for t in doc["types"]:
            assert t["kind"] in KINDS, (f, t)
    reg = Registry.load()
    assert "core/Deployment" not in reg.types["kubernetes"] and "apps/Deployment" in reg.types["kubernetes"]
    assert reg.types["netbox"]["dcim.device"].kind == "inventory" and reg.types["grafana"]["Dashboard"].kind == "observability"
