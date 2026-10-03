#!/usr/bin/env python3
"""Generate kg type catalogs for providers with machine-readable type lists.

Usage:
  python scripts/build_kg_catalog.py azure      scripts/kg_sources/azure_providers.json
  python scripts/build_kg_catalog.py aws        scripts/kg_sources/aws_cfn_spec.json
  python scripts/build_kg_catalog.py gcp        scripts/kg_sources/gcp_asset_types.txt
  python scripts/build_kg_catalog.py cloudflare scripts/kg_sources/cloudflare_schema.json
Writes src/memory_mcp/kg/catalog/<provider>.json and prints unmapped (kind=other) types to stderr.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "src" / "memory_mcp" / "kg" / "catalog"

# Ordered (first match wins). Patterns are matched case-insensitively against the full native type.
RULES: dict[str, list[tuple[str, str]]] = {
    "azure": [
        (r"^Microsoft\.Network/(dnszones|privateDnsZones|dnsResolvers)", "dns"),
        (r"^Microsoft\.Network/(frontDoors|FrontDoorWebApplicationFirewallPolicies)|^Microsoft\.Cdn/", "edge"),
        (r"^Microsoft\.Network/(networkSecurityGroups|azureFirewalls|firewallPolicies|ApplicationGatewayWebApplicationFirewallPolicies|ddosProtectionPlans)", "security"),
        (r"^Microsoft\.Network/", "network"),
        (r"^Microsoft\.(ContainerService|ContainerInstance|ContainerRegistry|App|Kubernetes|KubernetesConfiguration|RedHatOpenShift)/", "container"),
        (r"^Microsoft\.HybridCompute/", "host"),
        (r"^Microsoft\.(Compute|Web|Batch|DesktopVirtualization)/", "compute"),
        (r"^Microsoft\.(Sql|DBforPostgreSQL|DBforMySQL|DBforMariaDB|DocumentDB|Cache)/", "database"),
        (r"^Microsoft\.(Storage|StorageSync|NetApp|RecoveryServices|DataProtection)/", "storage"),
        (r"^Microsoft\.KeyVault/", "secret"),
        (r"^Microsoft\.(ManagedIdentity|Authorization|AAD|AzureActiveDirectory)/", "identity"),
        (r"^Microsoft\.(Security|SecurityInsights)/", "security"),
        (r"^Microsoft\.(Insights|OperationalInsights|Monitor|AlertsManagement|Dashboard)/", "observability"),
        (r"^Microsoft\.(ServiceBus|EventHub|EventGrid|Relay|NotificationHubs)/", "messaging"),
        (r"^Microsoft\.(Synapse|Kusto|DataFactory|Databricks|Fabric|PowerBIDedicated|StreamAnalytics|Purview)/", "analytics"),
        (r"^Microsoft\.(CognitiveServices|MachineLearningServices|Search)/", "ai"),
        (r"^Microsoft\.(Logic|ApiManagement|Web/connections)/", "integration"),
        (r"^Microsoft\.(Resources|Management|Subscription|Billing|Portal)/", "org"),
        (r"^Microsoft\.(Datadog|Elastic)/|^(NewRelic|Dynatrace)\.", "observability"),
        (r"^Microsoft\.(ServiceNetworking|Peering|NetworkFunction|ManagedNetworkFabric|HybridNetwork)/", "network"),
        (r"^Microsoft\.(SignalRService|Devices|Confluent|Communication)/", "messaging"),
        (r"^Microsoft\.(ServiceFabric|ServiceFabricMesh|Automation|DevTestLab|LabServices)/", "compute"),
        (r"^Microsoft\.(AppPlatform)/", "container"),
        (r"^Microsoft\.(AVS|AzureStackHCI|ConnectedVMwarevSphere|ScVmm)/", "host"),
        (r"^Microsoft\.(HDInsight|DataLakeAnalytics|DataShare|DataMigration)/", "analytics"),
        (r"^Microsoft\.(SqlVirtualMachine|AzureArcData)/|^Oracle\.Database/", "database"),
        (r"^Microsoft\.(FileShares|StorageCache|StorageMover|DataBox)/|^PureStorage\.", "storage"),
        (r"^PaloAltoNetworks\.", "security"),
        (r"^Microsoft\.(CostManagement|Consumption|Capacity|BillingBenefits|Marketplace)/", "org"),
    ],
    "aws": [
        (r"^AWS::EC2::(SecurityGroup|NetworkAcl)", "security"),
        (r"^AWS::EC2::(VPC|Subnet|RouteTable|Route|TransitGateway|NatGateway|InternetGateway|EgressOnlyInternetGateway|VPCEndpoint|EIP|NetworkInterface|VPNGateway|VPNConnection|CustomerGateway|PrefixList|IPAM)", "network"),
        (r"^AWS::(EC2|Lambda|AutoScaling|ElasticBeanstalk|Batch|Lightsail)::", "compute"),
        (r"^AWS::(ECS|EKS|ECR|AppRunner)::", "container"),
        (r"^AWS::(NetworkFirewall|WAFv2|WAF|WAFRegional|Shield|GuardDuty|SecurityHub|Inspector|Macie)::", "security"),
        (r"^AWS::(ElasticLoadBalancing|ElasticLoadBalancingV2|NetworkManager|DirectConnect|VpcLattice)::", "network"),
        (r"^AWS::Route53(Resolver)?::", "dns"),
        (r"^AWS::(CloudFront|GlobalAccelerator)::", "edge"),
        (r"^AWS::(RDS|DynamoDB|ElastiCache|DocDB|Neptune|MemoryDB)::", "database"),
        (r"^AWS::(S3|EFS|FSx|Backup|StorageGateway)::", "storage"),
        (r"^AWS::(SecretsManager|KMS|SSM::Parameter|ACMPCA)", "secret"),
        (r"^AWS::(IAM|SSO|Cognito|IdentityStore)::", "identity"),
        (r"^AWS::(CloudWatch|Logs|CloudTrail|XRay|Oam|APS|Grafana|ApplicationInsights|InternetMonitor|Synthetics)::", "observability"),
        (r"^AWS::(SQS|SNS|Events|Kinesis|MSK|AmazonMQ|Pipes)::", "messaging"),
        (r"^AWS::(Glue|Athena|Redshift|EMR|LakeFormation|QuickSight)::", "analytics"),
        (r"^AWS::(SageMaker|Bedrock|Comprehend|Rekognition)::", "ai"),
        (r"^AWS::(StepFunctions|ApiGateway|ApiGatewayV2|AppSync|AppFlow)::", "integration"),
        (r"^AWS::(Organizations|ControlTower|Budgets|CE)::", "org"),
        (r"^AWS::(ServiceDiscovery|Route53GlobalResolver|Route53Recovery\w*)::", "dns"),
        (r"^AWS::(AppMesh)::", "network"),
        (r"^AWS::(InspectorV2|FMS|Config|CertificateManager|SecurityLake|DevOpsAgent|SecurityAgent)::", "security"),
        (r"^AWS::(RolesAnywhere|VerifiedPermissions)::", "identity"),
        (r"^AWS::(ObservabilityAdmin|Notifications)::", "observability"),
        (r"^AWS::(SES|SMSVOICE|Pinpoint|PinpointEmail|KafkaConnect|EventsV2)::", "messaging"),
        (r"^AWS::(Timestream|Cassandra|OpenSearchServerless|OpenSearchService|DMS)::", "database"),
        (r"^AWS::(S3Tables|S3Outposts|S3Files|Transfer|DataSync)::", "storage"),
        (r"^AWS::(RedshiftServerless|KinesisAnalyticsV2|EMRContainers|DataBrew)::", "analytics"),
        (r"^AWS::(WorkSpaces|WorkSpacesWeb|AppStream|Amplify|ImageBuilder)::", "compute"),
        (r"^AWS::(CodeBuild|CodePipeline|CodeDeploy|CodeArtifact|CodeCommit)::", "work"),
        (r"^AWS::SSM::", "integration"),
    ],
    "gcp": [
        (r"^dns\.googleapis\.com/", "dns"),
        (r"^compute\.googleapis\.com/(Firewall|FirewallPolicy|SecurityPolicy)", "security"),
        (r"^compute\.googleapis\.com/(Network|Subnetwork|Router|Address|GlobalAddress|ForwardingRule|GlobalForwardingRule|BackendService|TargetHttpProxy|TargetHttpsProxy|UrlMap|VpnGateway|VpnTunnel|Route|InterconnectAttachment|NetworkEndpointGroup)", "network"),
        (r"^compute\.googleapis\.com/", "compute"),
        (r"^(container|artifactregistry|run|gkehub)\.googleapis\.com/", "container"),
        (r"^(cloudfunctions|appengine)\.googleapis\.com/", "compute"),
        (r"^(sqladmin|spanner|bigtableadmin|firestore|redis|alloydb)\.googleapis\.com/", "database"),
        (r"^(storage|file)\.googleapis\.com/", "storage"),
        (r"^(secretmanager|cloudkms)\.googleapis\.com/", "secret"),
        (r"^iam\.googleapis\.com/", "identity"),
        (r"^(logging|monitoring)\.googleapis\.com/", "observability"),
        (r"^pubsub\.googleapis\.com/", "messaging"),
        (r"^(bigquery|dataflow|dataproc|composer)\.googleapis\.com/", "analytics"),
        (r"^(aiplatform|ml)\.googleapis\.com/", "ai"),
        (r"^(apigateway|apigee|workflows)\.googleapis\.com/", "integration"),
        (r"^cloudresourcemanager\.googleapis\.com/", "org"),
        (r"^(iap|dlp|ids)\.googleapis\.com/", "security"),
        (r"^(dataplex|looker|dataform)\.googleapis\.com/", "analytics"),
        (r"^(batch|tpu)\.googleapis\.com/", "compute"),
        (r"^(netapp|lustre|backupdr)\.googleapis\.com/", "storage"),
        (r"^apikeys\.googleapis\.com/", "identity"),
    ],
    "cloudflare": [
        (r"^(dns_record|record|dns_)", "dns"),
        (r"^(zero_trust_access|access_|ruleset|waf|firewall|zero_trust_gateway|zero_trust_device|zero_trust_list)", "security"),
        (r"^(zero_trust_tunnel|tunnel|magic_|load_balancer)", "network"),
        (r"^(workers?_|pages_)", "compute"),
        (r"^r2_", "storage"),
        (r"^(zone|account)", "org"),
        (r".*", "edge"),
    ],
}

# Types that `az provider list` does not report but ARM IDs use (scopes, and the subnet child type).
# The GCP docs page rendered to curl is a partial list (it omits e.g. compute Instance), so alias targets
# that are real Cloud Asset Inventory types but missing from the scraped page are added explicitly.
EXTRAS = {
    "azure": ["Microsoft.Resources/subscriptions", "Microsoft.Resources/resourceGroups",
              "Microsoft.Network/virtualNetworks/subnets"],
    "gcp": ["compute.googleapis.com/Firewall", "compute.googleapis.com/Instance", "compute.googleapis.com/Subnetwork",
            "container.googleapis.com/Cluster", "iam.googleapis.com/ServiceAccount", "sqladmin.googleapis.com/Instance"],
}


def classify_kind(provider: str, typ: str) -> str:
    for pattern, kind in RULES.get(provider, []):
        if re.search(pattern, typ, re.IGNORECASE):
            return kind
    return "other"


def load_azure(doc: list[dict]) -> list[str]:
    return sorted({f"{p['namespace']}/{rt['resourceType']}" for p in doc for rt in p.get("resourceTypes", [])})


def load_aws_spec(doc: dict) -> list[str]:
    return sorted(doc["ResourceTypes"])


def load_lines(text: str) -> list[str]:
    return sorted({ln.strip() for ln in text.splitlines() if ln.strip() and not ln.startswith("#")})


def load_tf_schema(doc: dict, prefix: str) -> list[str]:
    out = set()
    for prov in doc["provider_schemas"].values():
        out |= {k[len(prefix):] for k in prov.get("resource_schemas", {}) if k.startswith(prefix)}
    return sorted(out)


def build_catalog(provider: str, types: list[str], aliases: dict[str, str]) -> dict:
    by_type: dict[str, list[str]] = {}
    for tf_name, native in aliases.items():
        by_type.setdefault(native, []).append(tf_name)
    all_types = sorted(set(types) | set(EXTRAS.get(provider, [])))
    entries = []
    for t in all_types:
        al = sorted(by_type.get(t, []))
        if provider == "cloudflare":
            al = sorted(set(al) | {f"cloudflare_{t}"})
        entries.append({"type": t, "kind": classify_kind(provider, t), "aliases": al})
    return {"provider": provider, "types": entries}


def main(argv: list[str]) -> int:
    provider, src = argv[1], Path(argv[2])
    raw = src.read_text()
    types = {
        "azure": lambda: load_azure(json.loads(raw)),
        "aws": lambda: load_aws_spec(json.loads(raw)),
        "gcp": lambda: load_lines(raw),
        "cloudflare": lambda: load_tf_schema(json.loads(raw), "cloudflare_"),
    }[provider]()
    aliases = (yaml.safe_load((ROOT / "scripts" / "kg_tf_aliases.yaml").read_text()) or {}).get(provider) or {}
    missing = sorted(set(aliases.values()) - set(types) - set(EXTRAS.get(provider, [])))
    if missing:
        print(f"WARNING alias targets not in source: {missing}", file=sys.stderr)
    cat = build_catalog(provider, types, aliases)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"{provider}.json").write_text(json.dumps(cat, indent=0) + "\n")
    other = [t["type"] for t in cat["types"] if t["kind"] == "other"]
    print(f"{provider}: {len(cat['types'])} types, {len(other)} unmapped", file=sys.stderr)
    for t in other:
        print(f"  other: {t}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
