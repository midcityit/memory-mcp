"""Hard-delete (entity + xref) and catalog-surfacing — RCL-54 and the tracker open question."""
import pytest

from memory_mcp.kg.models import KGError


def jira(svc, key, graph="mcit"):
    return svc.upsert_entity(graph, "logical", "jira_issue", key)


def node(svc, name="opi-5", graph="mcit"):
    return svc.upsert_entity(graph, "kubernetes", "core/Node", f"mcit-k8s/_cluster/core/Node/{name}",
                             display_name=name)


# ── entity hard delete ────────────────────────────────────────────────────────
def test_delete_entity_requires_confirm(svc):
    r = jira(svc, "MCIT-1")
    with pytest.raises(KGError) as e:
        svc.delete_entity("mcit", r["key"], confirm=False)
    assert e.value.code == "confirm_required"


def test_delete_entity_purges_entity_edges_and_xrefs(svc, gstore):
    # two linked entities in mcit, plus an xref from a vtv entity to one of them
    a = node(svc, "opi-5")
    b = jira(svc, "MCIT-184")
    svc.link("mcit", a["key"], "tracked_in", b["key"])
    svc.upsert_entity("vtv", "logical", "jira_issue", "VTV-238")
    svc.xref("vtv::logical:VTV-238", "pattern_from", f"mcit::{b['key']}", "pattern")
    assert len(gstore.xrefs_touching(f"mcit::{b['key']}")) == 1

    r = svc.delete_entity("mcit", b["key"], confirm=True)
    assert r["status"] == "deleted"
    assert r["edges_removed"] == 1
    assert r["xrefs_removed"] == 1
    assert gstore.get_entity("mcit", b["key"]) is None
    # xref is gone (not merely marked dangling) — this is the RCL-54 fix
    assert gstore.xrefs_touching(f"mcit::{b['key']}") == []


def test_delete_entity_missing(svc):
    with pytest.raises(KGError) as e:
        svc.delete_entity("mcit", "logical:nope", confirm=True)
    assert e.value.code == "not_found"


# ── xref hard delete ──────────────────────────────────────────────────────────
def test_delete_xref(svc, gstore):
    jira(svc, "MCIT-184")
    svc.upsert_entity("vtv", "logical", "jira_issue", "VTV-238")
    svc.xref("vtv::logical:VTV-238", "pattern_from", "mcit::logical:MCIT-184", "pattern")
    assert gstore.get_xref("vtv::logical:VTV-238", "pattern_from", "mcit::logical:MCIT-184") is not None

    r = svc.delete_xref("vtv::logical:VTV-238", "pattern_from", "mcit::logical:MCIT-184")
    assert r["status"] == "deleted"
    assert gstore.get_xref("vtv::logical:VTV-238", "pattern_from", "mcit::logical:MCIT-184") is None


def test_delete_xref_missing(svc):
    with pytest.raises(KGError) as e:
        svc.delete_xref("vtv::logical:VTV-1", "pattern_from", "mcit::logical:MCIT-1")
    assert e.value.code == "not_found"


def test_delete_dangling_xref_after_both_endpoints_gone(svc, gstore):
    # The exact RCL-54 scenario: delete both endpoints, then clean up the orphaned xref.
    jira(svc, "MCIT-9")
    svc.upsert_entity("vtv", "logical", "jira_issue", "VTV-9")
    svc.xref("vtv::logical:VTV-9", "lessons_from", "mcit::logical:MCIT-9", "lesson")
    svc.delete_entity("mcit", "logical:MCIT-9", confirm=True)  # purges the xref via touching cleanup
    # recreate a stray xref and remove it explicitly to prove the direct path works too
    jira(svc, "MCIT-9")
    svc.xref("vtv::logical:VTV-9", "lessons_from", "mcit::logical:MCIT-9", "lesson")
    assert svc.delete_xref("vtv::logical:VTV-9", "lessons_from", "mcit::logical:MCIT-9")["status"] == "deleted"


# ── catalog surfacing ─────────────────────────────────────────────────────────
def test_catalog_full(svc):
    cat = svc.reg.describe_catalog()
    assert "logical" in cat["providers"] and "azure" in cat["providers"]
    assert set(cat["kinds"]) >= {"compute", "identity", "host", "work", "org"}
    assert "mcit" in cat["graphs"] and "vtv" in cat["graphs"]
    assert cat["relations"]["virtualized_on"]["inverse"] == "virtualizes"
    assert cat["relations"]["virtualized_on"]["src_kinds"] == ["compute", "host"]
    assert cat["type_counts"]["logical"] >= 15


def test_catalog_filter_by_provider_and_search(svc):
    cat = svc.reg.describe_catalog(provider="logical", search="principal")
    assert list(cat["types"].keys()) == ["logical"]
    assert [t["type"] for t in cat["types"]["logical"]] == ["service_principal"]


def test_catalog_filter_by_kind(svc):
    cat = svc.reg.describe_catalog(provider="logical", kind="identity")
    kinds = {t["kind"] for t in cat["types"]["logical"]}
    assert kinds == {"identity"}
    types = {t["type"] for t in cat["types"]["logical"]}
    assert {"service_principal", "app_registration", "directory_role", "group"} <= types


def test_catalog_unknown_provider(svc):
    with pytest.raises(KGError) as e:
        svc.reg.describe_catalog(provider="azzure")
    assert e.value.code == "unknown_provider"
