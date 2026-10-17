import pytest
from memory_mcp.kg.models import (
    Entity, Edge, KGError, make_key, fq_key, split_fq, is_fq_key, entity_point_id, edge_point_id,
    iso_to_ts, is_current,
)


def test_keys_and_fq_round_trip():
    k = make_key("kubernetes", "mcit-k8s/digital-twin/apps/Deployment/memory-mcp")
    assert k == "kubernetes:mcit-k8s/digital-twin/apps/Deployment/memory-mcp"
    assert split_fq(fq_key("mcit", k)) == ("mcit", k)


def test_split_fq_rejects_unqualified():
    with pytest.raises(KGError) as e:
        split_fq("kubernetes:x")
    assert e.value.code == "invalid_native_id"


def test_is_fq_key_only_fires_on_graph_prefix():
    # Real fully-qualified keys: '{graph}::{key}'.
    assert is_fq_key("mcit::kubernetes:x")
    assert is_fq_key("vtv::aws:arn:aws:states:us-east-1:064025363976:stateMachine:x")
    # Bare key, no graph prefix.
    assert not is_fq_key("kubernetes:mcit-k8s/_cluster/core/Node/opi-5")
    # RCL-61: global-service AWS ARN keys contain '::' (empty region/account) but are NOT
    # cross-graph references — the segment before the first '::' is not a valid graph id.
    assert not is_fq_key("aws:arn:aws:iam::064025363976:role/StepFunctionRole")
    assert not is_fq_key("aws:arn:aws:s3:::my-bucket")
    assert not is_fq_key("aws:arn:aws:cloudfront::064025363976:distribution/E123")
    # split_fq must agree with is_fq_key: ARN keys are not splittable as fq.
    with pytest.raises(KGError):
        split_fq("aws:arn:aws:iam::064025363976:role/StepFunctionRole")


def test_point_ids_deterministic():
    assert entity_point_id("a:b") == entity_point_id("a:b")
    assert edge_point_id("a", "runs_on", "b", "t1") != edge_point_id("a", "runs_on", "b", "t2")


def test_iso_to_ts_handles_naive_and_none():
    assert iso_to_ts(None) is None
    assert iso_to_ts("2026-09-27T00:00:00") == iso_to_ts("2026-09-27T00:00:00+00:00")


def test_is_current_window():
    t = iso_to_ts("2026-09-20T00:00:00+00:00")
    assert is_current("2026-09-01T00:00:00+00:00", "2026-09-27T00:00:00+00:00", t)
    assert not is_current("2026-09-01T00:00:00+00:00", "2026-09-10T00:00:00+00:00", t)
    assert not is_current("2026-09-25T00:00:00+00:00", None, t)


def test_entity_payload_round_trip_and_compact():
    e = Entity(key="logical:MCIT-193", provider="logical", type="jira_issue", kind="work",
               native_id="MCIT-193", display_name="MCIT-193", valid_from="2026-09-08T00:00:00+00:00")
    p = e.to_payload()
    assert p["valid_from_ts"] == iso_to_ts(e.valid_from) and p["valid_to_ts"] is None
    assert Entity.from_payload(p) == e
    assert e.compact("mcit") == {"graph": "mcit", "key": "logical:MCIT-193",
                                 "display_name": "MCIT-193", "kind": "work", "type_short": "jira_issue"}


def test_kgerror_to_dict():
    d = KGError("unknown_type", "nope", field="type", suggestions=["x"]).to_dict()
    assert d == {"error": "unknown_type", "message": "nope", "field": "type", "suggestions": ["x"]}
