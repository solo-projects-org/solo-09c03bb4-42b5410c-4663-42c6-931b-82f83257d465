from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    settings = Settings(db_path=str(tmp_path / "test.db"), recalc_delay_seconds=0.0)
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client
    # Ensure a clean shutdown of the lifespan context.


def register(client: TestClient, batch_id: str = "B-1", cartons=None):
    if cartons is None:
        cartons = [
            {"carton_no": "CTN-1", "serials": ["SN-1", "SN-2"]},
            {"carton_no": "CTN-2", "serials": ["SN-3", "SN-4"]},
        ]
    response = client.post("/batches", json={"batch_id": batch_id, "cartons": cartons})
    assert response.status_code == 201, response.text
    return response.json()


def scan(client: TestClient, batch_id: str, scan_id: str, point: str, **kwargs):
    payload = {"scan_id": scan_id, "receiving_point": point, **kwargs}
    return client.post(f"/batches/{batch_id}/scans", json=payload)


def result(client: TestClient, batch_id: str = "B-1"):
    response = client.get(f"/batches/{batch_id}/result")
    assert response.status_code == 200, response.text
    return response.json()


def test_register_batch_creates_initial_unevidenced_result(client):
    data = register(client)
    assert data["batch"]["active_packing_list_version"] == 1
    assert data["batch"]["latest_packing_list_version"] == 1
    assert data["result"]["expected_count"] == 4
    assert data["result"]["received"] == []
    assert data["result"]["conflicts"] == []
    assert [item["serial_no"] for item in data["result"]["unevidenced"]] == [
        "SN-1",
        "SN-2",
        "SN-3",
        "SN-4",
    ]
    assert data["result"]["details"] == []


def test_duplicate_batch_rejected(client):
    register(client)
    response = client.post(
        "/batches",
        json={"batch_id": "B-1", "cartons": [{"carton_no": "C", "serials": ["S"]}]},
    )
    assert response.status_code == 409


def test_packing_list_rejects_duplicate_serial_across_cartons(client):
    response = client.post(
        "/batches",
        json={
            "batch_id": "B-DUP",
            "cartons": [
                {"carton_no": "CTN-1", "serials": ["SN-1"]},
                {"carton_no": "CTN-2", "serials": ["SN-1"]},
            ],
        },
    )
    assert response.status_code == 422
    assert "SN-1" in response.text


def test_carton_scan_requires_intact_seal(client):
    register(client)
    assert scan(client, "B-1", "SC-1", "A", carton_no="CTN-1").status_code == 422
    assert (
        scan(client, "B-1", "SC-2", "A", carton_no="CTN-1", seal_intact=False).status_code
        == 422
    )
    assert (
        scan(client, "B-1", "SC-3", "A", carton_no="CTN-1", seal_intact=True).status_code
        == 201
    )


def test_scan_requires_exactly_one_target(client):
    register(client)
    assert scan(client, "B-1", "SC-1", "A").status_code == 422
    assert (
        scan(
            client,
            "B-1",
            "SC-2",
            "A",
            carton_no="CTN-1",
            serial_no="SN-1",
            seal_intact=True,
        ).status_code
        == 422
    )
    assert (
        scan(client, "B-1", "SC-3", "A", serial_no="SN-1", seal_intact=True).status_code
        == 422
    )


def test_carton_scan_expands_to_declared_items(client):
    register(client)
    response = scan(client, "B-1", "SC-1", "A", carton_no="CTN-1", seal_intact=True)
    assert response.status_code == 201
    assert response.json()["result_refreshed"] is True

    data = result(client)
    assert data["received_count"] == 2
    assert {item["serial_no"] for item in data["received"]} == {"SN-1", "SN-2"}
    assert all(item["receiving_point"] == "A" for item in data["received"])
    assert data["unevidenced_count"] == 2
    assert data["details"][0]["expanded_serials"] == ["SN-1", "SN-2"]


def test_unknown_carton_is_unmatched_not_receipt(client):
    register(client)
    response = scan(client, "B-1", "SC-1", "A", carton_no="CTN-X", seal_intact=True)
    assert response.status_code == 201
    data = result(client)
    assert data["received_count"] == 0
    assert data["unevidenced_count"] == 4
    assert data["unmatched_scan_count"] == 1
    assert data["details"][0]["unmatched"] is True
    assert data["details"][0]["expanded_serials"] == []


def test_unknown_serial_is_unmatched_not_receipt(client):
    register(client)
    scan(client, "B-1", "SC-1", "A", serial_no="SN-999")
    data = result(client)
    assert data["received_count"] == 0
    assert data["unmatched_scan_count"] == 1


def test_same_item_same_point_counts_once(client):
    register(client)
    scan(client, "B-1", "SC-1", "A", serial_no="SN-1")
    scan(client, "B-1", "SC-2", "A", serial_no="SN-1")
    scan(client, "B-1", "SC-3", "A", carton_no="CTN-1", seal_intact=True)
    data = result(client)
    assert data["received_count"] == 2
    sn1 = next(item for item in data["received"] if item["serial_no"] == "SN-1")
    assert sn1["receiving_point"] == "A"
    assert sn1["scan_ids"] == ["SC-1", "SC-2", "SC-3"]
    assert data["conflicts"] == []


def test_same_item_from_multiple_points_is_conflict_with_scan_ids(client):
    register(client)
    scan(client, "B-1", "SC-A1", "A", serial_no="SN-1")
    scan(client, "B-1", "SC-A2", "A", serial_no="SN-1")
    scan(client, "B-1", "SC-B1", "B", carton_no="CTN-1", seal_intact=True)

    data = result(client)
    assert data["conflicts"] == [
        {
            "serial_no": "SN-1",
            "carton_no": "CTN-1",
            "reports": [
                {"receiving_point": "A", "scan_ids": ["SC-A1", "SC-A2"]},
                {"receiving_point": "B", "scan_ids": ["SC-B1"]},
            ],
        }
    ]
    # SN-1 is conflicted, SN-2 is received, SN-3/SN-4 unevidenced.
    assert [item["serial_no"] for item in data["received"]] == ["SN-2"]
    assert [item["serial_no"] for item in data["unevidenced"]] == ["SN-3", "SN-4"]
    assert data["conflict_count"] == 1


def test_duplicate_scan_id_rejected(client):
    register(client)
    assert scan(client, "B-1", "SC-1", "A", serial_no="SN-1").status_code == 201
    response = scan(client, "B-1", "SC-1", "B", serial_no="SN-2")
    assert response.status_code == 409


def test_scan_for_unknown_batch_is_404(client):
    response = scan(client, "NOPE", "SC-1", "A", serial_no="SN-1")
    assert response.status_code == 404


def test_correction_moves_item_and_recalculates(client):
    register(client)
    scan(client, "B-1", "SC-1", "A", serial_no="SN-1")

    response = client.post(
        "/batches/B-1/packing-lists/corrections",
        json={"reason": "复核", "moves": [{"serial_no": "SN-1", "carton_no": "CTN-2"}]},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["previous_packing_list_version"] == 1
    assert body["packing_list_version"] == 2
    assert body["result"]["packing_list_version"] == 2
    # Moved serial now belongs to CTN-2; receipt evidence still applies.
    assert body["result"]["received"][0]["serial_no"] == "SN-1"
    assert body["result"]["received"][0]["carton_no"] == "CTN-2"

    packing = client.get("/batches/B-1/packing-lists").json()
    assert packing["version"] == 2
    by_carton = {c["carton_no"]: c["serials"] for c in packing["cartons"]}
    assert by_carton["CTN-1"] == ["SN-2"]
    assert by_carton["CTN-2"] == ["SN-3", "SN-4", "SN-1"]

    # Old result remains queryable as history.
    old = client.get(f"/batches/B-1/results/{body['result']['result_id'] - 1}").json()
    assert old["packing_list_version"] == 1
    assert next(
        item for item in old["received"] if item["serial_no"] == "SN-1"
    )["carton_no"] == "CTN-1"


def test_correction_can_move_item_to_new_carton(client):
    register(client, cartons=[{"carton_no": "CTN-1", "serials": ["SN-1"]}])
    response = client.post(
        "/batches/B-1/packing-lists/corrections",
        json={"moves": [{"serial_no": "SN-1", "carton_no": "CTN-NEW"}]},
    )
    assert response.status_code == 200
    packing = response.json()["result"]
    assert packing["received"] == []
    cartons = client.get("/batches/B-1/packing-lists").json()["cartons"]
    assert {c["carton_no"]: c["serials"] for c in cartons} == {
        "CTN-1": [],
        "CTN-NEW": ["SN-1"],
    }


def test_correction_rejects_unknown_serial_and_keeps_old_version(client):
    register(client)
    response = client.post(
        "/batches/B-1/packing-lists/corrections",
        json={"moves": [{"serial_no": "SN-404", "carton_no": "CTN-1"}]},
    )
    assert response.status_code == 422
    batch = client.get("/batches/B-1").json()
    assert batch["active_packing_list_version"] == 1
    assert batch["latest_packing_list_version"] == 1
    assert batch["recalculation_in_progress"] is False
    versions = client.get("/batches/B-1/packing-lists/versions").json()
    assert [(v["version"], v["status"]) for v in versions] == [(1, "active")]


def test_correction_rejects_noop(client):
    register(client)
    response = client.post(
        "/batches/B-1/packing-lists/corrections",
        json={"moves": [{"serial_no": "SN-1", "carton_no": "CTN-1"}]},
    )
    assert response.status_code == 422


def test_concurrent_scan_during_recalculation_is_included_in_new_version(tmp_path):
    """Scans accepted while recalculation is in flight must appear in the new result.

    The delay is real wall-clock time in the correction request; a second
    client thread submits scans during that window.
    """
    import threading
    import time

    settings = Settings(db_path=str(tmp_path / "race.db"), recalc_delay_seconds=0.8)
    app = create_app(settings)
    with TestClient(app) as client:
        register(client)
        scan(client, "B-1", "SC-1", "A", serial_no="SN-1")

        outcome: dict[str, object] = {}

        def correct():
            outcome["response"] = client.post(
                "/batches/B-1/packing-lists/corrections",
                json={"moves": [{"serial_no": "SN-3", "carton_no": "CTN-1"}]},
            )

        thread = threading.Thread(target=correct)
        thread.start()
        # Wait until the pending version is visible, then read and scan.
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            batch = client.get("/batches/B-1").json()
            if batch["recalculation_in_progress"]:
                break
            time.sleep(0.02)
        else:
            raise AssertionError("pending version never became visible")

        during = result(client)
        assert during["packing_list_version"] == 1
        assert {item["serial_no"] for item in during["received"]} == {"SN-1"}

        scan_response = scan(client, "B-1", "SC-2", "A", serial_no="SN-2")
        assert scan_response.status_code == 201
        assert scan_response.json()["result_refreshed"] is False

        # Queries during recalculation still serve the original version.
        during_again = result(client)
        assert during_again["packing_list_version"] == 1
        assert during_again["result_id"] == during["result_id"]

        thread.join(timeout=10)
        assert not thread.is_alive()
        final = outcome["response"]
        assert final.status_code == 200, final.text
        body = final.json()
        assert body["packing_list_version"] == 2
        assert {item["serial_no"] for item in body["result"]["received"]} == {
            "SN-1",
            "SN-2",
        }
        assert body["result"]["packing_list_version"] == 2
        # SN-3 was moved into CTN-1 and has no evidence yet -> unevidenced.
        assert "SN-3" in {item["serial_no"] for item in body["result"]["unevidenced"]}

        # Every received item's evidence is visible in the same snapshot.
        detail_ids = {detail["scan_id"] for detail in body["result"]["details"]}
        assert {"SC-1", "SC-2"} <= detail_ids


def test_failed_correction_keeps_previous_result_serving(client, monkeypatch):
    register(client)
    scan(client, "B-1", "SC-1", "A", serial_no="SN-1")
    before = result(client)

    from app import service

    def explode(*args, **kwargs):
        raise RuntimeError("simulated persistence failure")

    # Fail only the recalculation attempt, then let the recovery refresh run.
    original_persist = service._persist_result
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated persistence failure")
        return original_persist(*args, **kwargs)

    monkeypatch.setattr(service, "_persist_result", flaky)
    response = client.post(
        "/batches/B-1/packing-lists/corrections",
        json={"moves": [{"serial_no": "SN-2", "carton_no": "CTN-2"}]},
    )
    assert response.status_code == 500
    assert "继续使用原装箱单" in response.text

    after = result(client)
    assert after["packing_list_version"] == 1
    assert after["received"] == before["received"]
    batch = client.get("/batches/B-1").json()
    assert batch["active_packing_list_version"] == 1
    assert batch["recalculation_in_progress"] is False
    versions = client.get("/batches/B-1/packing-lists/versions").json()
    assert versions[0]["status"] == "failed"

    monkeypatch.undo()

    # Scans are still accepted after a failed correction.
    assert scan(client, "B-1", "SC-2", "A", serial_no="SN-2").status_code == 201
    assert result(client)["received_count"] == 2


def test_failed_correction_result_reflects_scans_accepted_meanwhile(tmp_path):
    """A failed change still converges the old result with in-flight scans."""
    import threading
    import time

    settings = Settings(db_path=str(tmp_path / "failwindow.db"), recalc_delay_seconds=0.5)
    app = create_app(settings)
    with TestClient(app) as client:
        from app import service

        register(client)
        scan(client, "B-1", "SC-1", "A", serial_no="SN-1")

        original = service._persist_result

        def flaky(conn, batch_id, packing_list_version, *args, **kwargs):
            # Fail only the activation of version 2; refresh of the still-active
            # version 1 (and any scan intake) must keep working.
            if packing_list_version == 2:
                raise RuntimeError("simulated failure")
            return original(conn, batch_id, packing_list_version, *args, **kwargs)

        service._persist_result = flaky
        outcome = {}

        def correct():
            outcome["response"] = client.post(
                "/batches/B-1/packing-lists/corrections",
                json={"moves": [{"serial_no": "SN-2", "carton_no": "CTN-2"}]},
            )

        try:
            thread = threading.Thread(target=correct)
            thread.start()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if client.get("/batches/B-1").json()["recalculation_in_progress"]:
                    break
                time.sleep(0.02)
            else:
                raise AssertionError("pending version never became visible")
            scan(client, "B-1", "SC-2", "A", serial_no="SN-2")
            thread.join(timeout=10)
        finally:
            service._persist_result = original

        assert outcome["response"].status_code == 500
        final = result(client)
        assert final["packing_list_version"] == 1
        assert {i["serial_no"] for i in final["received"]} == {"SN-1", "SN-2"}
        assert final["result_id"] != 0


def test_pending_correction_blocks_second_correction_and_refresh(client, tmp_path):
    settings = Settings(db_path=str(tmp_path / "second.db"), recalc_delay_seconds=0.5)
    app = create_app(settings)
    import threading
    import time

    with TestClient(app) as client:
        register(client)
        outcome = {}

        def correct():
            outcome["response"] = client.post(
                "/batches/B-1/packing-lists/corrections",
                json={"moves": [{"serial_no": "SN-3", "carton_no": "CTN-1"}]},
            )

        thread = threading.Thread(target=correct)
        thread.start()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if client.get("/batches/B-1").json()["recalculation_in_progress"]:
                break
            time.sleep(0.02)
        else:
            raise AssertionError("pending version never became visible")

        second = client.post(
            "/batches/B-1/packing-lists/corrections",
            json={"moves": [{"serial_no": "SN-4", "carton_no": "CTN-1"}]},
        )
        assert second.status_code == 409
        assert client.post("/batches/B-1/result/refresh").status_code == 409

        thread.join(timeout=10)
        assert outcome["response"].status_code == 200


def test_versions_and_results_history(client):
    register(client)
    client.post(
        "/batches/B-1/packing-lists/corrections",
        json={"moves": [{"serial_no": "SN-2", "carton_no": "CTN-2"}]},
    )
    versions = client.get("/batches/B-1/packing-lists/versions").json()
    assert [(v["version"], v["status"]) for v in versions] == [
        (2, "active"),
        (1, "superseded"),
    ]
    old_packing = client.get("/batches/B-1/packing-lists?version=1").json()
    assert {c["carton_no"]: c["serials"] for c in old_packing["cartons"]}["CTN-1"] == [
        "SN-1",
        "SN-2",
    ]

    results = client.get("/batches/B-1/results").json()
    assert len(results) == 2
    assert results[0]["active"] is True
    assert results[0]["packing_list_version"] == 2
    assert results[1]["active"] is False
    assert results[1]["packing_list_version"] == 1


def test_scan_ids_recomputed_after_correction(client):
    """Carton evidence is re-expanded against the new version; direct scans persist."""
    register(client)
    scan(client, "B-1", "SC-1", "A", carton_no="CTN-1", seal_intact=True)
    scan(client, "B-1", "SC-2", "A", serial_no="SN-1")
    response = client.post(
        "/batches/B-1/packing-lists/corrections",
        json={"moves": [{"serial_no": "SN-1", "carton_no": "CTN-2"}]},
    )
    body = response.json()["result"]
    assert body["received_count"] == 2
    # SN-1 moved out of CTN-1, so the earlier CTN-1 carton scan no longer
    # evidences it; its direct scan remains.
    sn1 = next(i for i in body["received"] if i["serial_no"] == "SN-1")
    assert sn1["carton_no"] == "CTN-2"
    assert sn1["scan_ids"] == ["SC-2"]
    # SN-2 stays in CTN-1 and is still evidenced by the carton scan.
    sn2 = next(i for i in body["received"] if i["serial_no"] == "SN-2")
    assert sn2["carton_no"] == "CTN-1"
    assert sn2["scan_ids"] == ["SC-1"]
    # Detail rows reflect the new packing list.
    details = {d["scan_id"]: d for d in body["details"]}
    assert details["SC-1"]["expanded_serials"] == ["SN-2"]


def test_refresh_picks_up_scans(client):
    register(client)
    scan(client, "B-1", "SC-1", "A", serial_no="SN-1")
    refresh = client.post("/batches/B-1/result/refresh")
    assert refresh.status_code == 200
    assert refresh.json()["result"]["received_count"] == 1


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def test_restart_recovers_pending_version_as_failed(tmp_path):
    """A crashed recalculation must not block intake or reads after restart."""
    settings = Settings(db_path=str(tmp_path / "restart.db"), recalc_delay_seconds=0.0)
    app = create_app(settings)
    with TestClient(app) as client:
        register(client)
        scan(client, "B-1", "SC-1", "A", serial_no="SN-1")

    # Simulate a crash window: insert a pending version, then reopen.
    from app.db import Database

    db = Database(settings.db_path)
    with db.immediate() as conn:
        batch = conn.execute("SELECT * FROM batches WHERE batch_id='B-1'").fetchone()
        conn.execute(
            """
            INSERT INTO packing_list_versions (batch_id, version, status, created_at)
            VALUES ('B-1', ?, 'pending', 'x')
            """,
            (batch["latest_packing_list_version"] + 1,),
        )
        # Scan accepted during the crashed window is not yet in the snapshot.
        conn.execute(
            """
            INSERT INTO scans (
                batch_id, scan_id, receiving_point, serial_no, accepted_at
            ) VALUES ('B-1', 'SC-CRASH', 'A', 'SN-2', 'x')
            """
        )

    app2 = create_app(settings)
    with TestClient(app2) as client2:
        batch = client2.get("/batches/B-1").json()
        assert batch["recalculation_in_progress"] is False
        assert batch["active_packing_list_version"] == 1
        recovered = result(client2)
        assert recovered["packing_list_version"] == 1
        assert {i["serial_no"] for i in recovered["received"]} == {"SN-1", "SN-2"}
        versions = client2.get("/batches/B-1/packing-lists/versions").json()
        assert versions[0]["status"] == "failed"
        assert scan(client2, "B-1", "SC-3", "A", serial_no="SN-3").status_code == 201


# ---------------------------------------------------------------------------
# Paged conflict queries by receiving point
# ---------------------------------------------------------------------------


def conflict_batch(client: TestClient, batch_id: str = "B-1"):
    """SN-1/SN-2/SN-3 conflicted across points A and B; SN-4 unevidenced.

    SC-3 is a whole-carton scan of CTN-1 (SN-1, SN-2), so it is cited by two
    conflict items — the fixture for evidence deduplication.
    """
    register(client, batch_id)
    assert scan(client, batch_id, "SC-1", "A", serial_no="SN-1").status_code == 201
    assert scan(client, batch_id, "SC-2", "B", serial_no="SN-1").status_code == 201
    assert (
        scan(client, batch_id, "SC-3", "A", carton_no="CTN-1", seal_intact=True).status_code
        == 201
    )
    assert scan(client, batch_id, "SC-4", "B", serial_no="SN-2").status_code == 201
    assert scan(client, batch_id, "SC-5", "A", serial_no="SN-3").status_code == 201
    assert scan(client, batch_id, "SC-6", "B", serial_no="SN-3").status_code == 201


def conflict_page(client: TestClient, path: str, **params):
    response = client.get(path, params=params)
    assert response.status_code == 200, response.text
    return response.json()


def test_conflict_page_first_and_continuation(client):
    conflict_batch(client)
    page1 = conflict_page(
        client, "/batches/B-1/result", receiving_point="A", page_size=2
    )
    assert page1["receiving_point"] == "A"
    assert page1["page_size"] == 2
    assert page1["point_conflict_count"] == 3
    assert page1["packing_list_version"] == 1
    # Original conflict order (packing-list order), two items per page.
    assert [c["serial_no"] for c in page1["conflicts"]] == ["SN-1", "SN-2"]
    # Every item still carries all reporting points and their scan ids.
    assert page1["conflicts"][0]["reports"] == [
        {"receiving_point": "A", "scan_ids": ["SC-1", "SC-3"]},
        {"receiving_point": "B", "scan_ids": ["SC-2"]},
    ]
    assert page1["conflicts"][1]["reports"] == [
        {"receiving_point": "A", "scan_ids": ["SC-3"]},
        {"receiving_point": "B", "scan_ids": ["SC-4"]},
    ]
    # Only the scans cited by this page, each exactly once, in scan order.
    assert [e["scan_id"] for e in page1["evidence"]] == ["SC-1", "SC-2", "SC-3", "SC-4"]
    assert page1["next_cursor"]

    page2 = conflict_page(
        client,
        f"/batches/B-1/results/{page1['result_id']}",
        receiving_point="A",
        cursor=page1["next_cursor"],
    )
    assert page2["result_id"] == page1["result_id"]
    assert [c["serial_no"] for c in page2["conflicts"]] == ["SN-3"]
    assert [e["scan_id"] for e in page2["evidence"]] == ["SC-5", "SC-6"]
    assert page2["next_cursor"] is None

    # Pages combined equal the full result's conflict list: no gaps, no dups.
    full = client.get(f"/batches/B-1/results/{page1['result_id']}").json()
    paged = page1["conflicts"] + page2["conflicts"]
    assert [c["serial_no"] for c in paged] == [
        c["serial_no"] for c in full["conflicts"]
    ]
    assert paged == full["conflicts"]


def test_conflict_page_evidence_dedup_and_snapshot_expansion(client):
    conflict_batch(client)
    page1 = conflict_page(
        client, "/batches/B-1/result", receiving_point="A", page_size=2
    )
    cited = [e["scan_id"] for e in page1["evidence"]]
    # SC-3 (carton CTN-1) is cited by both SN-1 and SN-2 on this page: once.
    assert cited.count("SC-3") == 1
    sc3 = next(e for e in page1["evidence"] if e["scan_id"] == "SC-3")
    assert sc3["expanded_serials"] == ["SN-1", "SN-2"]

    # Correct the packing list: SN-2 leaves CTN-1.  The pinned page keeps the
    # queried result's expansion; a fresh page on the new result reflects the
    # new version.
    response = client.post(
        "/batches/B-1/packing-lists/corrections",
        json={"moves": [{"serial_no": "SN-2", "carton_no": "CTN-2"}]},
    )
    assert response.status_code == 200, response.text

    old_page = conflict_page(
        client,
        f"/batches/B-1/results/{page1['result_id']}",
        receiving_point="A",
        page_size=2,
    )
    sc3_old = next(e for e in old_page["evidence"] if e["scan_id"] == "SC-3")
    assert sc3_old["expanded_serials"] == ["SN-1", "SN-2"]

    new_page = conflict_page(
        client, "/batches/B-1/result", receiving_point="A", page_size=10
    )
    assert new_page["result_id"] != page1["result_id"]
    sc3_new = next(e for e in new_page["evidence"] if e["scan_id"] == "SC-3")
    assert sc3_new["expanded_serials"] == ["SN-1"]


def test_conflict_page_pinned_result_immune_to_correction_and_scans(client):
    conflict_batch(client)
    page1 = conflict_page(
        client, "/batches/B-1/result", receiving_point="A", page_size=2
    )
    pinned = page1["result_id"]

    # Meanwhile: a packing-list correction activates a new version and a new
    # scan is accepted.  Continuation must still follow the pinned result.
    response = client.post(
        "/batches/B-1/packing-lists/corrections",
        json={"moves": [{"serial_no": "SN-4", "carton_no": "CTN-1"}]},
    )
    assert response.status_code == 200, response.text
    assert scan(client, "B-1", "SC-7", "A", serial_no="SN-4").status_code == 201

    page2 = conflict_page(
        client,
        f"/batches/B-1/results/{pinned}",
        receiving_point="A",
        cursor=page1["next_cursor"],
    )
    assert page2["result_id"] == pinned
    assert page2["packing_list_version"] == 1
    assert [c["serial_no"] for c in page2["conflicts"]] == ["SN-3"]
    assert page2["next_cursor"] is None

    serials = [c["serial_no"] for c in page1["conflicts"] + page2["conflicts"]]
    assert serials == ["SN-1", "SN-2", "SN-3"]  # 不重不漏


def test_conflict_page_for_point_without_conflicts(client):
    conflict_batch(client)
    page = conflict_page(client, "/batches/B-1/result", receiving_point="C")
    assert page["conflicts"] == []
    assert page["evidence"] == []
    assert page["point_conflict_count"] == 0
    assert page["next_cursor"] is None


def test_result_without_receiving_point_keeps_original_shape(client):
    conflict_batch(client)
    data = result(client)
    for key in ("received", "conflicts", "unevidenced", "details", "scan_high_watermark"):
        assert key in data
    assert "next_cursor" not in data
    assert "evidence" not in data
    by_id = client.get(f"/batches/B-1/results/{data['result_id']}").json()
    assert "details" in by_id
    assert "next_cursor" not in by_id


def test_conflict_page_cursor_validation(client):
    conflict_batch(client)
    page1 = conflict_page(
        client, "/batches/B-1/result", receiving_point="A", page_size=2
    )
    cursor = page1["next_cursor"]
    rid = page1["result_id"]

    # Cursor bound to a different receiving point.
    response = client.get(
        f"/batches/B-1/results/{rid}", params={"receiving_point": "B", "cursor": cursor}
    )
    assert response.status_code == 422

    # Cursor bound to a different result id.
    client.post("/batches/B-1/result/refresh")
    other = result(client)["result_id"]
    assert other != rid
    response = client.get(
        f"/batches/B-1/results/{other}",
        params={"receiving_point": "A", "cursor": cursor},
    )
    assert response.status_code == 422

    # Garbage cursor.
    response = client.get(
        f"/batches/B-1/results/{rid}",
        params={"receiving_point": "A", "cursor": "garbage"},
    )
    assert response.status_code == 422

    # The active-result endpoint does not accept continuation cursors.
    response = client.get(
        "/batches/B-1/result", params={"receiving_point": "A", "cursor": cursor}
    )
    assert response.status_code == 422

    # page_size bounds.
    assert (
        client.get(
            "/batches/B-1/result", params={"receiving_point": "A", "page_size": 0}
        ).status_code
        == 422
    )


def drop_conflict_index(db_path: str, result_id: int) -> None:
    """Simulate a result persisted before the paged-query feature existed."""
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("DELETE FROM result_conflict_points WHERE result_id = ?", (result_id,))
        conn.execute(
            "DELETE FROM result_conflict_evidence WHERE result_id = ?", (result_id,)
        )
        conn.execute(
            "DELETE FROM result_conflict_index_status WHERE result_id = ?", (result_id,)
        )
        conn.commit()
    finally:
        conn.close()


def test_conflict_page_backfills_index_for_old_result(tmp_path):
    settings = Settings(db_path=str(tmp_path / "old.db"), recalc_delay_seconds=0.0)
    app = create_app(settings)
    with TestClient(app) as client:
        conflict_batch(client)
        rid = result(client)["result_id"]
        drop_conflict_index(settings.db_path, rid)

        # Scan intake keeps working before the backfill is triggered.
        assert scan(client, "B-1", "SC-7", "A", serial_no="SN-4").status_code == 201

        page = conflict_page(
            client, f"/batches/B-1/results/{rid}", receiving_point="A", page_size=10
        )
        assert [c["serial_no"] for c in page["conflicts"]] == ["SN-1", "SN-2", "SN-3"]
        assert [e["scan_id"] for e in page["evidence"]] == [
            "SC-1",
            "SC-2",
            "SC-3",
            "SC-4",
            "SC-5",
            "SC-6",
        ]

        # The backfilled index matches what a fresh result would have.
        import sqlite3

        conn = sqlite3.connect(settings.db_path)
        try:
            points = conn.execute(
                "SELECT COUNT(*) FROM result_conflict_points WHERE result_id = ?",
                (rid,),
            ).fetchone()[0]
            assert points == 6  # 3 conflicts x 2 reporting points
            status = conn.execute(
                "SELECT built_at FROM result_conflict_index_status WHERE result_id = ?",
                (rid,),
            ).fetchone()
            assert status is not None
        finally:
            conn.close()


def test_conflict_page_backfill_failure_is_retryable(tmp_path, monkeypatch):
    settings = Settings(db_path=str(tmp_path / "backfill.db"), recalc_delay_seconds=0.0)
    app = create_app(settings)
    with TestClient(app) as client:
        conflict_batch(client)
        rid = result(client)["result_id"]
        drop_conflict_index(settings.db_path, rid)

        from app import service

        def explode(conn, result_id):
            raise RuntimeError("simulated backfill failure")

        monkeypatch.setattr(service, "_build_conflict_index_locked", explode)
        response = client.get(
            f"/batches/B-1/results/{rid}", params={"receiving_point": "A"}
        )
        assert response.status_code == 500
        assert "重试" in response.text

        # The caller retries with the same result id and succeeds.
        monkeypatch.undo()
        retry = conflict_page(client, f"/batches/B-1/results/{rid}", receiving_point="A")
        assert [c["serial_no"] for c in retry["conflicts"]] == ["SN-1", "SN-2", "SN-3"]


def test_conflict_page_missing_evidence_fails(tmp_path):
    settings = Settings(db_path=str(tmp_path / "evidence.db"), recalc_delay_seconds=0.0)
    app = create_app(settings)
    with TestClient(app) as client:
        conflict_batch(client)
        rid = result(client)["result_id"]
        drop_conflict_index(settings.db_path, rid)

        # Corrupt the snapshot: a cited scan detail is gone.
        import sqlite3

        conn = sqlite3.connect(settings.db_path)
        try:
            conn.execute(
                "DELETE FROM result_scan_details WHERE result_id = ? AND scan_id = 'SC-2'",
                (rid,),
            )
            conn.commit()
        finally:
            conn.close()

        response = client.get(
            f"/batches/B-1/results/{rid}", params={"receiving_point": "A"}
        )
        assert response.status_code == 500
        assert "取证" in response.text
        assert "SC-2" in response.text


def test_conflict_page_scans_accepted_during_backfill(tmp_path):
    """A slow backfill must not block scan intake (writers only serialise)."""
    import threading
    import time

    settings = Settings(db_path=str(tmp_path / "concurrent.db"), recalc_delay_seconds=0.0)
    app = create_app(settings)
    with TestClient(app) as client:
        conflict_batch(client)
        rid = result(client)["result_id"]
        drop_conflict_index(settings.db_path, rid)

        from app import service

        original = service._build_conflict_index_locked

        def slow(conn, result_id):
            time.sleep(0.4)
            return original(conn, result_id)

        service._build_conflict_index_locked = slow
        outcome: dict[str, object] = {}

        def page():
            outcome["response"] = client.get(
                f"/batches/B-1/results/{rid}", params={"receiving_point": "A"}
            )

        try:
            thread = threading.Thread(target=page)
            thread.start()
            time.sleep(0.05)  # let the backfill's writer transaction start
            accepted = scan(client, "B-1", "SC-7", "A", serial_no="SN-4")
            assert accepted.status_code == 201
            thread.join(timeout=10)
            assert not thread.is_alive()
        finally:
            service._build_conflict_index_locked = original

        assert outcome["response"].status_code == 200, outcome["response"].text
        body = outcome["response"].json()
        assert [c["serial_no"] for c in body["conflicts"]] == ["SN-1", "SN-2", "SN-3"]
