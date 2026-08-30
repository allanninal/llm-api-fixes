from anthropic_cmek_external_key_audit import (attachment_type, classify,
                                                coverage, kms_ref, mask_arn,
                                                repair_lines, uncovered,
                                                workspace_geo)

ARN = "arn:aws:kms:eu-west-1:210987654321:key/9f2c"


def key(kid, kind="unattached", geo="eu", name="EU customer key"):
    return {"id": kid, "type": "external_key", "display_name": name, "geo": geo,
            "attachment": {"type": kind},
            "provider_config": {"type": "aws", "kms_arn": ARN}}


def workspace(wid, key_id=None, geo="eu", archived=None):
    return {"id": wid, "type": "workspace", "name": wid,
            "external_key_id": key_id, "archived_at": archived,
            "data_residency": {"workspace_geo": geo,
                               "default_inference_geo": geo,
                               "allowed_inference_geos": "unrestricted"}}


def test_two_configs_with_no_live_workspace_and_only_one_may_be_deleted():
    # The note, and the most dangerous thing in it. Neither config covers a live
    # workspace. One is inert; deleting the other destroys retained data.
    inert = key("ekey_01hq", "unattached")
    holding = key("ekey_01gd", "attached", name="Legacy tenant key")
    cover = coverage([workspace("wrk_04", "ekey_01gd", archived=1_700_000_000)])

    state_a, detail_a = classify(inert, cover.get("ekey_01hq"), [])
    assert state_a == "unattached-and-unused"
    assert "inert" in detail_a

    state_b, detail_b = classify(holding, cover.get("ekey_01gd"),
                                 [("wrk_04", "eu")])
    assert state_b == "archived-workspaces-only"
    assert "still encrypted under this config" in detail_b

    lines_a = repair_lines(state_a, inert)
    lines_b = repair_lines(state_b, holding)
    assert any("can be deleted" in line for line in lines_a)
    assert not any("can be deleted" in line for line in lines_b)
    assert any("unrecoverable" in line for line in lines_b)


def test_when_the_two_listings_disagree_the_safe_reading_wins():
    stale = key("ekey_01zz", "unattached")
    cover = coverage([workspace("wrk_09", "ekey_01zz")])
    state, detail = classify(stale, cover.get("ekey_01zz"), [("wrk_09", "eu")])
    assert state == "unattached-but-referenced"
    assert "The two listings disagree" in detail
    lines = repair_lines(state, stale)
    assert any("do not delete this" in line for line in lines)
    assert not any("can be deleted" in line for line in lines)


def test_an_unrecognised_attachment_is_never_assumed_unattached():
    assert attachment_type(key("e", "attached")) == "attached"
    assert attachment_type(key("e", "UNATTACHED")) == "unattached"
    assert attachment_type({"id": "e", "attachment": {"type": "pending"}}) == "unknown"
    assert attachment_type({"id": "e"}) == "unknown"
    assert attachment_type(None) == "unknown"
    state, detail = classify({"id": "e", "attachment": {"type": "pending"}}, {}, [])
    assert state == "attachment-unreadable"
    assert "will not say whether" in detail


def test_a_geo_mismatch_is_read_across_the_workspaces_it_covers():
    eu_key = key("ekey_01eu", "attached", geo="eu")
    cover = coverage([workspace("wrk_01", "ekey_01eu", geo="eu"),
                      workspace("wrk_02", "ekey_01eu", geo="us")])
    geos = [("wrk_01", "eu"), ("wrk_02", "us")]
    state, detail = classify(eu_key, cover.get("ekey_01eu"), geos)
    assert state == "geo-mismatch"
    assert "wrk_02 at us" in detail
    assert "wrk_01" not in detail
    assert any("write-once" in line for line in repair_lines(state, eu_key))
    # Matching geos are simply covered.
    assert classify(eu_key, cover.get("ekey_01eu"),
                    [("wrk_01", "eu")])[0] == "covered"
    assert repair_lines("covered", eu_key) == []


def test_the_coverage_map_and_the_uncovered_split():
    rows = [workspace("wrk_01"), workspace("wrk_02", None),
            workspace("wrk_03", "ekey_01hq"),
            workspace("wrk_04", "ekey_01hq", archived=1_700_000_000),
            workspace("wrk_05", None, archived=1_700_000_001)]
    cover = coverage(rows)
    assert cover == {"ekey_01hq": {"live": ["wrk_03"], "archived": ["wrk_04"]}}
    # A null external_key_id must never become a key id.
    assert "None" not in cover and None not in cover
    assert uncovered(rows) == (["wrk_01", "wrk_02"], ["wrk_05"])
    assert coverage(None) == {}
    assert uncovered(None) == ([], [])
    assert workspace_geo(rows[0]) == "eu"
    assert workspace_geo({"id": "w"}) is None


def test_the_provider_line_names_the_key_and_masks_the_account():
    assert mask_arn(ARN) == "arn:aws:kms:eu-west-1:****:key/9f2c"
    assert mask_arn("not-an-arn") == "not-an-arn"
    assert mask_arn(None) == "unknown"
    assert kms_ref({"type": "aws", "kms_arn": ARN}).startswith("aws arn:aws:kms:")
    assert "210987654321" not in kms_ref({"type": "aws", "kms_arn": ARN})
    assert kms_ref({"type": "gcp", "key_name": "projects/p/locations/eu/x"}) == \
        "gcp projects/p/locations/eu/x"
    assert "vault.azure.net" in kms_ref(
        {"type": "azure", "key_name": "k", "vault_uri": "https://v.vault.azure.net"})
    assert kms_ref({"type": "quantum"}) == "unrecognised provider quantum"
    assert kms_ref(None) == "unrecognised provider none"
    # Every finding says the validate call was deliberately not made.
    assert any("write verb" in line
               for line in repair_lines("unattached-and-unused", key("ekey_1")))
