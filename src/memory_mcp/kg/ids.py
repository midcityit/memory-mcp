"""Per-provider native-ID validation, normalization and type/ID consistency."""
from __future__ import annotations

import re

from memory_mcp.kg.models import KGError
from memory_mcp.kg.registry import TypeDef

_AZ = re.compile(r"^/subscriptions/[^/]+(?:/resourcegroups/[^/]+)?(?:/providers/(?P<rest>.+))?$")
_ARN = re.compile(r"^arn:(aws|aws-cn|aws-us-gov):(?P<svc>[a-z0-9-]+):[a-z0-9-]*:(\d{12})?:.+$")
_GCP = re.compile(r"^//(?P<svc>[a-z0-9.-]+\.googleapis\.com)/.+$")
_HOSTPATH = re.compile(r"^(?P<host>[A-Za-z0-9.-]+)/(?P<rest>.+)$")
_CF = re.compile(r"^[0-9a-f]{32}/[^/]+/(?P<otype>[a-z0-9_]+)/.+$")
_LOGICAL = {
    "jira_issue": re.compile(r"^[A-Z][A-Z0-9]+-\d+$"),
    "repo": re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$"),
    "confluence_page": re.compile(r"^[A-Za-z0-9~]+/\d+$"),
}
_LOGICAL_DEFAULT = re.compile(r"^\S.*$")
# CloudFormation service segment -> ARN service, where they differ.
_AWS_SVC = {
    "elasticloadbalancingv2": "elasticloadbalancing", "networkfirewall": "network-firewall",
    "stepfunctions": "states", "apigatewayv2": "apigateway", "cognito": "cognito-idp",
    "msk": "kafka", "opensearchservice": "es", "elasticsearch": "es",
}


def _invalid(provider: str, nid: str, hint: str) -> KGError:
    return KGError("invalid_native_id", f"Invalid {provider} native_id '{nid}': expected {hint}", field="native_id")


def _mismatch(declared: str, found: str) -> KGError:
    return KGError("type_id_mismatch", f"native_id is a '{found}' but type is '{declared}'", field="type",
                   suggestions=[found])


def _azure(td: TypeDef, nid: str) -> str:
    n = nid.lower().rstrip("/")
    m = _AZ.match(n)
    if not m:
        raise _invalid("azure", nid, "a full ARM resource ID (/subscriptions/...)")
    rest = m.group("rest")
    if rest is None:
        found = "microsoft.resources/resourcegroups" if "/resourcegroups/" in n else "microsoft.resources/subscriptions"
    else:
        parts = rest.split("/")
        if len(parts) < 3 or len(parts) % 2 == 0:
            raise _invalid("azure", nid, "providers/{namespace}/{type}/{name}[/{childType}/{childName}...]")
        found = parts[0] + "/" + "/".join(parts[1::2])
    if found != td.type.lower():
        raise _mismatch(td.type, found)
    return n


def _aws(td: TypeDef, nid: str) -> str:
    m = _ARN.match(nid)
    if not m:
        raise _invalid("aws", nid, "an ARN (arn:aws:service:region:account:resource)")
    cfn_svc = td.type.split("::")[1].lower()
    expected = _AWS_SVC.get(cfn_svc, cfn_svc)
    if m.group("svc") != expected:
        raise _mismatch(td.type, f"arn service '{m.group('svc')}'")
    return nid


def _gcp(td: TypeDef, nid: str) -> str:
    m = _GCP.match(nid)
    if not m:
        raise _invalid("gcp", nid, "a full resource name (//service.googleapis.com/...)")
    if m.group("svc") != td.type.split("/")[0]:
        raise _mismatch(td.type, m.group("svc"))
    return nid


def _hostpath(provider: str, td: TypeDef, nid: str, check_segment: bool) -> str:
    m = _HOSTPATH.match(nid)
    if not m:
        raise _invalid(provider, nid, "{host_fqdn}/{path}")
    out = f"{m.group('host').lower()}/{m.group('rest')}"
    if check_segment:
        seg = m.group("rest").split("/", 1)
        if len(seg) < 2:
            raise _invalid(provider, nid, "{host_fqdn}/{object_type}/{name}")
        if seg[0].lower() != td.type.lower():
            raise _mismatch(td.type, seg[0])
    return out


def _cloudflare(td: TypeDef, nid: str) -> str:
    n = nid.lower()
    m = _CF.match(n)
    if not m:
        raise _invalid("cloudflare", nid, "{account_id}/{zone or -}/{object_type}/{id_or_name}")
    if m.group("otype") != td.type:
        raise _mismatch(td.type, m.group("otype"))
    return n


def _kubernetes(td: TypeDef, nid: str) -> str:
    parts = nid.split("/")
    if len(parts) != 5 or not all(parts):
        raise _invalid("kubernetes", nid, "{cluster}/{namespace or _cluster}/{group}/{Kind}/{name}")
    found = f"{parts[2]}/{parts[3]}"
    if found != td.type:
        raise _mismatch(td.type, found)
    return nid


def _netbox(td: TypeDef, nid: str) -> str:
    out = _hostpath("netbox", td, nid, check_segment=True)
    if not out.split("/", 2)[2].isdigit():
        raise _invalid("netbox", nid, "{netbox_host}/{app.model}/{numeric id}")
    return out


def _logical(td: TypeDef, nid: str) -> str:
    if not _LOGICAL.get(td.type, _LOGICAL_DEFAULT).match(nid):
        raise _invalid("logical", nid, f"the {td.type} format")
    return nid


def normalize_native_id(provider: str, td: TypeDef, native_id: str) -> str:
    nid = (native_id or "").strip()
    if not nid:
        raise _invalid(provider, native_id, "a non-empty identifier")
    if provider == "azure":
        return _azure(td, nid)
    if provider == "aws":
        return _aws(td, nid)
    if provider == "gcp":
        return _gcp(td, nid)
    if provider == "vmware":
        return _hostpath("vmware", td, nid, check_segment=False)
    if provider == "hyperv":
        return _hostpath("hyperv", td, nid, check_segment=True)
    if provider == "cloudflare":
        return _cloudflare(td, nid)
    if provider == "kubernetes":
        return _kubernetes(td, nid)
    if provider in ("grafana", "prometheus"):
        return _hostpath(provider, td, nid, check_segment=True)
    if provider == "netbox":
        return _netbox(td, nid)
    return _logical(td, nid)
