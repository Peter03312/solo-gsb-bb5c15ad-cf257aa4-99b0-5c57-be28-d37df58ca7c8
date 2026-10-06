"""End-to-end tests for physical copy registration and session assignment.

覆盖：
- 拷贝登记：正整数片长/非负转运分钟校验、唯一编号、影片级配置固定、
  尚无核准流水门槛（含已取消核准）、同编号同参数重放、编号/配置冲突
- 分配：门槛（未核准/已取消/已开场/异片/未知键/未知拷贝）、同场馆重叠、
  跨场馆转运间隔、恰好满足边界可用、冲突不落账、同拷贝重试返回现状、
  换拷贝原子替换
- 取消释放分配；改签同事务检验新时段（冲突则排片/分配/额度不变）
- 已配置拷贝的影片领开场码前须有有效分配；未配置影片沿用旧行为
- 占用与冲突查询；并发分配与改签不超订；原核验/批量口径不变
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app import service
from app.service import Store
from app.timeutil import to_iso

FUTURE = "2030-06-01T20:00:00Z"
FUTURE_2 = "2030-06-02T20:00:00Z"
WINDOW = {
    "valid_from": "2020-01-01T00:00:00Z",
    "valid_to": "2031-01-01T00:00:00Z",
}

CLOCK_BASE = datetime(2026, 6, 1, 19, 30, tzinfo=timezone.utc)


def _iso_at(**delta) -> str:
    return to_iso(CLOCK_BASE + timedelta(**delta))


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def make_license(client, **overrides):
    body = {
        "film_id": "film-1",
        "venue_id": "hall-a",
        "valid_from": WINDOW["valid_from"],
        "valid_to": WINDOW["valid_to"],
        "max_sessions": 50,
    }
    body.update(overrides)
    resp = client.post("/licenses", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def register_copies(client, film="film-1", count=2, duration=120, transfer=45):
    codes = []
    for i in range(1, count + 1):
        code = f"{film}-COPY-{i:03d}"
        resp = client.post(
            f"/films/{film}/copies",
            json={
                "duration_minutes": duration,
                "transfer_minutes": transfer,
                "copy_code": code,
            },
        )
        assert resp.status_code == 201, resp.text
        codes.append(code)
    return codes


def verify(client, key, start=FUTURE, film="film-1", venue="hall-a"):
    return client.post(
        "/verifications",
        json={
            "film_id": film,
            "venue_id": venue,
            "start_time": start,
            "idempotency_key": key,
        },
    )


def assign(client, key, copy_code):
    return client.post(
        f"/verifications/{key}/copy-assignment", json={"copy_code": copy_code}
    )


# ----------------------------------------------------------------------
# registration
# ----------------------------------------------------------------------
def test_register_copy_creates_profile_and_copy(client):
    resp = client.post(
        "/films/film-1/copies",
        json={"duration_minutes": 120, "transfer_minutes": 45,
              "copy_code": "COPY-001"},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["copy_code"] == "COPY-001"
    assert data["duration_minutes"] == 120
    assert data["transfer_minutes"] == 45
    assert data["replay"] is False

    listing = client.get("/films/film-1/copies").json()
    assert listing["configured"] is True
    assert listing["duration_minutes"] == 120
    assert listing["transfer_minutes"] == 45
    assert [c["copy_code"] for c in listing["copies"]] == ["COPY-001"]


def test_register_copy_validates_params(client):
    base = "/films/film-1/copies"
    assert client.post(base, json={"duration_minutes": 0,
                                   "transfer_minutes": 0,
                                   "copy_code": "C-1"}).status_code == 422
    assert client.post(base, json={"duration_minutes": -5,
                                   "transfer_minutes": 0,
                                   "copy_code": "C-1"}).status_code == 422
    assert client.post(base, json={"duration_minutes": 90,
                                   "transfer_minutes": -1,
                                   "copy_code": "C-1"}).status_code == 422
    assert client.post(base, json={"duration_minutes": 90,
                                   "transfer_minutes": 0,
                                   "copy_code": "  "}).status_code == 422
    # nothing persisted
    assert client.get("/films/film-1/copies").json()["configured"] is False


def test_register_same_copy_same_params_replays(client):
    body = {"duration_minutes": 100, "transfer_minutes": 30,
            "copy_code": "COPY-001"}
    first = client.post("/films/film-1/copies", json=body).json()
    second = client.post("/films/film-1/copies", json=body).json()
    assert second["replay"] is True
    assert second["id"] == first["id"]
    assert len(client.get("/films/film-1/copies").json()["copies"]) == 1


def test_register_second_copy_must_match_profile(client):
    register_copies(client, count=1, duration=120, transfer=45)
    ok = client.post(
        "/films/film-1/copies",
        json={"duration_minutes": 120, "transfer_minutes": 45,
              "copy_code": "COPY-002"},
    )
    assert ok.status_code == 201
    mismatch = client.post(
        "/films/film-1/copies",
        json={"duration_minutes": 90, "transfer_minutes": 45,
              "copy_code": "COPY-003"},
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["code"] == "COPY_PROFILE_MISMATCH"
    mismatch2 = client.post(
        "/films/film-1/copies",
        json={"duration_minutes": 120, "transfer_minutes": 10,
              "copy_code": "COPY-004"},
    )
    assert mismatch2.status_code == 409
    assert len(client.get("/films/film-1/copies").json()["copies"]) == 2


def test_register_copy_code_conflict_across_films(client):
    register_copies(client, film="film-1", count=1)
    resp = client.post(
        "/films/film-2/copies",
        json={"duration_minutes": 120, "transfer_minutes": 45,
              "copy_code": "film-1-COPY-001"},
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "COPY_CODE_CONFLICT"


def test_register_blocked_once_film_has_approved_verification(client):
    make_license(client)
    verify(client, "v-1")
    resp = client.post(
        "/films/film-1/copies",
        json={"duration_minutes": 120, "transfer_minutes": 45,
              "copy_code": "COPY-001"},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "FILM_HAS_APPROVED_VERIFICATIONS"


def test_register_blocked_even_if_approved_verification_cancelled(client):
    make_license(client)
    verify(client, "v-1")
    client.post("/verifications/v-1/cancel", json={"reason": "取消"})
    resp = client.post(
        "/films/film-1/copies",
        json={"duration_minutes": 120, "transfer_minutes": 45,
              "copy_code": "COPY-001"},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "FILM_HAS_APPROVED_VERIFICATIONS"


def test_register_allowed_when_only_rejected_verifications(client):
    # no license at all -> verification rejected; film may still register
    verify(client, "v-rej", film="film-1", venue="hall-a")
    assert client.get("/verifications/v-rej").json()["decision"] == "rejected"
    resp = client.post(
        "/films/film-1/copies",
        json={"duration_minutes": 120, "transfer_minutes": 45,
              "copy_code": "COPY-001"},
    )
    assert resp.status_code == 201


def test_unconfigured_film_listing(client):
    listing = client.get("/films/film-none/copies").json()
    assert listing["configured"] is False
    assert listing["copies"] == []
    assert listing["duration_minutes"] is None


# ----------------------------------------------------------------------
# assignment gates
# ----------------------------------------------------------------------
def test_assign_happy_path(client):
    make_license(client)
    codes = register_copies(client, count=1)
    verify(client, "v-1")
    resp = assign(client, "v-1", codes[0])
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["replay"] is False
    assert data["copy_code"] == codes[0]
    assert data["start_time"].startswith("2030-06-01T20:00:00")
    # occupancy window = start + 120min
    assert data["end_time"].startswith("2030-06-01T22:00:00")
    assert data["previous_copy_code"] is None

    status = client.get("/verifications/v-1/copy-assignment").json()
    assert status["assigned"] is True
    assert status["copy_code"] == codes[0]


def test_assign_unknown_verification_404(client):
    resp = assign(client, "no-such", "COPY-001")
    assert resp.status_code == 404
    assert resp.json()["code"] == "NOT_FOUND"


def test_assign_unknown_copy_404(client):
    make_license(client)
    verify(client, "v-1")
    resp = assign(client, "v-1", "COPY-XXX")
    assert resp.status_code == 404
    assert resp.json()["code"] == "COPY_NOT_FOUND"


def test_assign_rejected_verification_422(client):
    register_copies(client, count=1)
    verify(client, "v-rej", film="film-1", venue="hall-a")  # no license
    resp = assign(client, "v-rej", "film-1-COPY-001")
    assert resp.status_code == 422
    assert resp.json()["code"] == "VERIFICATION_NOT_APPROVED"


def test_assign_cancelled_session_422(client):
    make_license(client)
    codes = register_copies(client, count=1)
    verify(client, "v-1")
    client.post("/verifications/v-1/cancel", json={"reason": "取消"})
    resp = assign(client, "v-1", codes[0])
    assert resp.status_code == 422
    assert resp.json()["code"] == "SESSION_CANCELLED"


def test_assign_started_session_422(client):
    make_license(client)
    codes = register_copies(client, count=1)
    verify(client, "v-past", start="2020-06-01T20:00:00Z")
    resp = assign(client, "v-past", codes[0])
    assert resp.status_code == 422
    assert resp.json()["code"] == "SESSION_ALREADY_STARTED"


def test_assign_copy_of_other_film_422(client):
    make_license(client)
    register_copies(client, film="film-1", count=1)
    register_copies(client, film="film-2", count=1)
    verify(client, "v-1", film="film-1")
    resp = assign(client, "v-1", "film-2-COPY-001")
    assert resp.status_code == 422
    assert resp.json()["code"] == "COPY_FILM_MISMATCH"


def test_assign_same_copy_replay_returns_current(client):
    make_license(client)
    codes = register_copies(client, count=1)
    verify(client, "v-1")
    first = assign(client, "v-1", codes[0]).json()
    second = assign(client, "v-1", codes[0]).json()
    assert second["replay"] is True
    assert second["id"] == first["id"]
    assert second["assigned_at"] == first["assigned_at"]
    # still exactly one assignment row
    detail = client.get(f"/copies/{codes[0]}").json()
    assert len(detail["occupancy"]) == 1


# ----------------------------------------------------------------------
# conflict rules: same-venue overlap / cross-venue transfer gap
# ----------------------------------------------------------------------
def test_same_venue_overlap_conflicts_and_is_not_persisted(client):
    make_license(client)
    codes = register_copies(client, count=1, duration=120, transfer=45)
    verify(client, "v-1", start="2030-06-01T10:00:00Z")
    verify(client, "v-2", start="2030-06-01T11:30:00Z")  # overlaps [10:00,12:00)
    assert assign(client, "v-1", codes[0]).status_code == 200
    resp = assign(client, "v-2", codes[0])
    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == "COPY_CONFLICT"
    assert len(body["conflicts"]) == 1
    assert body["conflicts"][0]["idempotency_key"] == "v-1"
    assert body["conflicts"][0]["end_time"].startswith("2030-06-01T12:00:00")
    # nothing persisted for v-2; v-1 untouched
    assert client.get("/verifications/v-2/copy-assignment").json()["assigned"] is False
    detail = client.get(f"/copies/{codes[0]}").json()
    assert [o["idempotency_key"] for o in detail["occupancy"]] == ["v-1"]


def test_same_venue_back_to_back_boundary_is_usable(client):
    make_license(client)
    codes = register_copies(client, count=1, duration=120)
    verify(client, "v-1", start="2030-06-01T10:00:00Z")
    verify(client, "v-2", start="2030-06-01T12:00:00Z")  # exactly at end
    assert assign(client, "v-1", codes[0]).status_code == 200
    assert assign(client, "v-2", codes[0]).status_code == 200


def test_cross_venue_transfer_gap_enforced_and_boundary_ok(client):
    make_license(client, venue_id="hall-a")
    make_license(client, venue_id="hall-b")
    codes = register_copies(client, count=1, duration=120, transfer=45)
    verify(client, "v-1", start="2030-06-01T10:00:00Z", venue="hall-a")
    # gap 30min < 45min transfer -> conflict
    verify(client, "v-2", start="2030-06-01T12:30:00Z", venue="hall-b")
    # gap exactly 45min -> usable
    verify(client, "v-3", start="2030-06-01T12:45:00Z", venue="hall-b")
    assert assign(client, "v-1", codes[0]).status_code == 200
    resp = assign(client, "v-2", codes[0])
    assert resp.status_code == 409
    assert resp.json()["code"] == "COPY_CONFLICT"
    assert assign(client, "v-3", codes[0]).status_code == 200


def test_cross_venue_gap_checked_in_both_directions(client):
    make_license(client, venue_id="hall-a")
    make_license(client, venue_id="hall-b")
    codes = register_copies(client, count=1, duration=120, transfer=45)
    verify(client, "v-late", start="2030-06-01T10:00:00Z", venue="hall-a")
    # earlier session at another venue ending 30min before -> conflict
    verify(client, "v-early", start="2030-06-01T07:30:00Z", venue="hall-b")
    assert assign(client, "v-late", codes[0]).status_code == 200
    resp = assign(client, "v-early", codes[0])
    assert resp.status_code == 409
    # ending exactly 45min before -> usable
    verify(client, "v-early-ok", start="2030-06-01T07:15:00Z", venue="hall-b")
    assert assign(client, "v-early-ok", codes[0]).status_code == 200


def test_different_copies_are_independent(client):
    make_license(client)
    codes = register_copies(client, count=2, duration=120)
    verify(client, "v-1", start=FUTURE)
    verify(client, "v-2", start=FUTURE)
    assert assign(client, "v-1", codes[0]).status_code == 200
    assert assign(client, "v-2", codes[1]).status_code == 200


# ----------------------------------------------------------------------
# atomic swap
# ----------------------------------------------------------------------
def test_swap_copy_is_atomic_and_releases_old(client):
    make_license(client)
    codes = register_copies(client, count=2)
    verify(client, "v-1")
    first = assign(client, "v-1", codes[0]).json()
    swapped = assign(client, "v-1", codes[1]).json()
    assert swapped["replay"] is False
    assert swapped["id"] == first["id"]  # same assignment row, replaced
    assert swapped["copy_code"] == codes[1]
    assert swapped["previous_copy_code"] == codes[0]
    assert client.get(f"/copies/{codes[0]}").json()["occupancy"] == []
    assert len(client.get(f"/copies/{codes[1]}").json()["occupancy"]) == 1


def test_swap_to_conflicting_copy_fails_and_keeps_old(client):
    make_license(client)
    codes = register_copies(client, count=2)
    verify(client, "v-1", start=FUTURE)
    verify(client, "v-2", start=FUTURE)
    assign(client, "v-1", codes[0])
    assign(client, "v-2", codes[1])
    # swapping v-1 to codes[1] would collide with v-2 -> rejected, unchanged
    resp = assign(client, "v-1", codes[1])
    assert resp.status_code == 409
    assert resp.json()["code"] == "COPY_CONFLICT"
    status = client.get("/verifications/v-1/copy-assignment").json()
    assert status["copy_code"] == codes[0]
    assert len(client.get(f"/copies/{codes[0]}").json()["occupancy"]) == 1
    assert len(client.get(f"/copies/{codes[1]}").json()["occupancy"]) == 1


# ----------------------------------------------------------------------
# cancellation releases the assignment
# ----------------------------------------------------------------------
def test_cancel_releases_assignment_and_frees_slot(client):
    make_license(client)
    codes = register_copies(client, count=1, duration=120)
    verify(client, "v-1", start=FUTURE)
    verify(client, "v-2", start=FUTURE)
    assign(client, "v-1", codes[0])
    assert assign(client, "v-2", codes[0]).status_code == 409

    client.post("/verifications/v-1/cancel", json={"reason": "取消"})
    assert client.get("/verifications/v-1/copy-assignment").json()["assigned"] is False
    assert client.get(f"/copies/{codes[0]}").json()["occupancy"] == []
    # freed slot can be taken by another session
    assert assign(client, "v-2", codes[0]).status_code == 200


# ----------------------------------------------------------------------
# reschedule re-checks the copy window in the same transaction
# ----------------------------------------------------------------------
def test_reschedule_into_copy_conflict_rejected_and_unchanged(client):
    make_license(client)
    codes = register_copies(client, count=1, duration=120)
    verify(client, "v-1", start="2030-06-01T10:00:00Z")
    verify(client, "v-2", start="2030-06-02T10:00:00Z")
    assign(client, "v-1", codes[0])
    assign(client, "v-2", codes[0])

    # move v-2 next to v-1: overlapping same-venue slot
    resp = client.post(
        "/verifications/v-2/reschedule",
        json={"new_start_time": "2030-06-01T11:00:00Z",
              "operation_key": "op-v2-1"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["decision"] == "rejected"
    assert data["reject_reason"] == "COPY_CONFLICT"
    assert data["to_license_id"] is None

    # schedule, assignment and quota all unchanged
    cur = client.get("/verifications/v-2").json()
    assert cur["start_time"].startswith("2030-06-02")
    assert cur["rescheduled"] is False
    status = client.get("/verifications/v-2/copy-assignment").json()
    assert status["assigned"] is True
    assert status["start_time"].startswith("2030-06-02")
    occupancy = client.get(f"/copies/{codes[0]}").json()["occupancy"]
    assert len(occupancy) == 2
    lic = client.get("/licenses").json()[0]
    assert lic["used_sessions"] == 2


def test_reschedule_without_copy_conflict_moves_occupancy(client):
    make_license(client)
    codes = register_copies(client, count=1, duration=120)
    verify(client, "v-1", start="2030-06-01T10:00:00Z")
    verify(client, "v-2", start="2030-06-02T10:00:00Z")
    assign(client, "v-1", codes[0])
    assign(client, "v-2", codes[0])

    resp = client.post(
        "/verifications/v-2/reschedule",
        json={"new_start_time": "2030-06-01T12:00:00Z",  # back-to-back, OK
              "operation_key": "op-v2-ok"},
    )
    assert resp.json()["decision"] == "approved"
    # occupancy follows the new schedule automatically
    occupancy = client.get(f"/copies/{codes[0]}").json()["occupancy"]
    by_key = {o["idempotency_key"]: o for o in occupancy}
    assert by_key["v-2"]["start_time"].startswith("2030-06-01T12:00:00")
    assert by_key["v-2"]["end_time"].startswith("2030-06-01T14:00:00")


def test_reschedule_shift_within_own_window_excludes_self(client):
    """A small shift overlaps the session's *own* old window; the old
    occupancy must not count as a conflict with itself."""
    make_license(client)
    codes = register_copies(client, count=1, duration=120)
    verify(client, "v-1", start="2030-06-01T10:00:00Z")
    assign(client, "v-1", codes[0])
    resp = client.post(
        "/verifications/v-1/reschedule",
        json={"new_start_time": "2030-06-01T10:30:00Z",
              "operation_key": "op-self"},
    )
    assert resp.json()["decision"] == "approved"


def test_reschedule_unassigned_session_on_configured_film_unaffected(client):
    make_license(client)
    register_copies(client, count=1)
    verify(client, "v-1", start=FUTURE)  # no assignment
    resp = client.post(
        "/verifications/v-1/reschedule",
        json={"new_start_time": FUTURE_2, "operation_key": "op-free"},
    )
    assert resp.json()["decision"] == "approved"


# ----------------------------------------------------------------------
# opening-code gate: configured films need an active assignment
# ----------------------------------------------------------------------
def test_claim_opening_code_requires_assignment_when_configured(client):
    make_license(client)
    codes = register_copies(client, count=1)
    verify(client, "v-1")
    resp = client.post("/verifications/v-1/opening-code")
    assert resp.status_code == 422
    assert resp.json()["code"] == "COPY_ASSIGNMENT_REQUIRED"
    # still not issued afterwards
    assert client.get("/verifications/v-1/opening-code").json()["status"] == "not_issued"

    assign(client, "v-1", codes[0])
    ok = client.post("/verifications/v-1/opening-code")
    assert ok.status_code == 200
    assert ok.json()["status"] == "issued"


def test_claim_opening_code_unconfigured_film_keeps_legacy_behaviour(client):
    make_license(client)
    verify(client, "v-1")
    resp = client.post("/verifications/v-1/opening-code")
    assert resp.status_code == 200
    assert resp.json()["status"] == "issued"


# ----------------------------------------------------------------------
# occupancy / conflict query endpoints
# ----------------------------------------------------------------------
def test_copy_detail_occupancy_and_conflicts_preview(client):
    make_license(client, venue_id="hall-a")
    make_license(client, venue_id="hall-b")
    codes = register_copies(client, count=1, duration=120, transfer=45)
    verify(client, "v-1", start="2030-06-01T10:00:00Z", venue="hall-a")
    assign(client, "v-1", codes[0])

    detail = client.get(f"/copies/{codes[0]}").json()
    assert detail["duration_minutes"] == 120
    assert detail["transfer_minutes"] == 45
    assert len(detail["occupancy"]) == 1
    occ = detail["occupancy"][0]
    assert occ["idempotency_key"] == "v-1"
    assert occ["start_time"].startswith("2030-06-01T10:00:00")
    assert occ["end_time"].startswith("2030-06-01T12:00:00")

    # preview: same venue overlapping -> conflict listed
    preview = client.get(
        f"/copies/{codes[0]}/conflicts",
        params={"venue_id": "hall-a", "start_time": "2030-06-01T11:00:00Z"},
    ).json()
    assert len(preview["conflicts"]) == 1
    assert preview["end_time"].startswith("2030-06-01T13:00:00")
    # preview: other venue with enough transfer gap -> no conflict
    ok = client.get(
        f"/copies/{codes[0]}/conflicts",
        params={"venue_id": "hall-b", "start_time": "2030-06-01T12:45:00Z"},
    ).json()
    assert ok["conflicts"] == []
    # unknown copy
    assert client.get("/copies/NOPE").status_code == 404
    assert client.get(
        "/copies/NOPE/conflicts",
        params={"venue_id": "hall-a", "start_time": FUTURE},
    ).status_code == 404


def test_assignment_status_for_unassigned_session(client):
    make_license(client)
    register_copies(client, count=1)
    verify(client, "v-1")
    status = client.get("/verifications/v-1/copy-assignment").json()
    assert status["assigned"] is False
    assert status["copy_code"] is None
    assert client.get("/verifications/no-such/copy-assignment").status_code == 404


# ----------------------------------------------------------------------
# legacy verification surface unchanged
# ----------------------------------------------------------------------
def test_verification_and_batch_payloads_unchanged_by_copies(client):
    make_license(client, max_sessions=5)
    register_copies(client, count=1)
    resp = verify(client, "v-1")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {
        "id", "idempotency_key", "film_id", "venue_id", "start_time",
        "decision", "license_id", "pool_id", "reject_reason",
        "remaining_quota", "pool_remaining_quota", "verified_at",
        "replay", "cancelled", "cancellation",
    }
    replay = verify(client, "v-1").json()
    assert replay["replay"] is True
    assert replay["remaining_quota"] == body["remaining_quota"]

    batch = client.post(
        "/verifications/batch",
        json={"items": [{
            "film_id": "film-1", "venue_id": "hall-a",
            "start_time": FUTURE_2, "idempotency_key": "v-2",
        }]},
    ).json()
    assert batch["results"][0]["decision"] == "approved"
    lic = client.get("/licenses").json()[0]
    assert lic["used_sessions"] == 2


# ----------------------------------------------------------------------
# concurrency: copies are never oversold
# ----------------------------------------------------------------------
def test_concurrent_assignments_same_copy_same_slot(tmp_path):
    store = Store(str(tmp_path / "copy-race.db"))
    store.create_license(
        film_id="film-x", venue_id="hall-1",
        valid_from=datetime(2020, 1, 1, tzinfo=timezone.utc),
        valid_to=datetime(2031, 1, 1, tzinfo=timezone.utc),
        max_sessions=50,
    )
    store.register_copy(
        "film-x", duration_minutes=120, transfer_minutes=45, copy_code="COPY-1"
    )
    n = 10
    for i in range(n):
        store.verify(
            film_id="film-x", venue_id="hall-1",
            start_time=datetime(2030, 6, 1, 20, 0, tzinfo=timezone.utc),
            idempotency_key=f"s-{i}",
        )

    results, errors = [], []
    barrier = threading.Barrier(n)

    def worker(i: int) -> None:
        barrier.wait()
        try:
            results.append(store.assign_copy(f"s-{i}", copy_code="COPY-1"))
        except service.ConflictError as exc:
            results.append({"conflict": True, "code": exc.code})
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    winners = [r for r in results if not r.get("conflict")]
    losers = [r for r in results if r.get("conflict")]
    assert len(winners) == 1
    assert len(losers) == n - 1
    assert all(l["code"] == "COPY_CONFLICT" for l in losers)
    assert len(store.get_copy("COPY-1")["occupancy"]) == 1


def test_concurrent_duplicate_assignment_replays(tmp_path):
    store = Store(str(tmp_path / "copy-dup.db"))
    store.create_license(
        film_id="film-x", venue_id="hall-1",
        valid_from=datetime(2020, 1, 1, tzinfo=timezone.utc),
        valid_to=datetime(2031, 1, 1, tzinfo=timezone.utc),
        max_sessions=5,
    )
    store.register_copy(
        "film-x", duration_minutes=120, transfer_minutes=45, copy_code="COPY-1"
    )
    store.verify(
        film_id="film-x", venue_id="hall-1",
        start_time=datetime(2030, 6, 1, 20, 0, tzinfo=timezone.utc),
        idempotency_key="s-1",
    )
    results, errors = [], []

    def worker() -> None:
        try:
            results.append(store.assign_copy("s-1", copy_code="COPY-1"))
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(results) == 10
    assert sum(1 for r in results if not r["replay"]) == 1
    assert len({r["id"] for r in results}) == 1
    assert len(store.get_copy("COPY-1")["occupancy"]) == 1


def test_concurrent_reschedules_of_assigned_sessions_no_oversell(tmp_path):
    """Two sessions share one copy at different slots; both reschedule to the
    same target slot concurrently: exactly one moves, the other is rejected
    COPY_CONFLICT and keeps its old slot; the copy is never double-booked."""
    store = Store(str(tmp_path / "copy-rs-race.db"))
    store.create_license(
        film_id="film-x", venue_id="hall-1",
        valid_from=datetime(2020, 1, 1, tzinfo=timezone.utc),
        valid_to=datetime(2031, 1, 1, tzinfo=timezone.utc),
        max_sessions=10,
    )
    store.register_copy(
        "film-x", duration_minutes=120, transfer_minutes=45, copy_code="COPY-1"
    )
    store.verify(
        film_id="film-x", venue_id="hall-1",
        start_time=datetime(2030, 6, 1, 10, 0, tzinfo=timezone.utc),
        idempotency_key="s-1",
    )
    store.verify(
        film_id="film-x", venue_id="hall-1",
        start_time=datetime(2030, 6, 2, 10, 0, tzinfo=timezone.utc),
        idempotency_key="s-2",
    )
    store.assign_copy("s-1", copy_code="COPY-1")
    store.assign_copy("s-2", copy_code="COPY-1")

    target = datetime(2030, 6, 3, 10, 0, tzinfo=timezone.utc)
    results, errors = [], []
    barrier = threading.Barrier(2)

    def worker(key: str, op: str) -> None:
        barrier.wait()
        try:
            results.append(
                store.reschedule_verification(
                    key, new_start_time=target, operation_key=op
                )
            )
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    t1 = threading.Thread(target=worker, args=("s-1", "op-1"))
    t2 = threading.Thread(target=worker, args=("s-2", "op-2"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert not errors
    approved = [r for r in results if r["decision"] == "approved"]
    rejected = [r for r in results if r["decision"] == "rejected"]
    assert len(approved) == 1
    assert len(rejected) == 1
    assert rejected[0]["reject_reason"] == "COPY_CONFLICT"

    # copy occupancy: two non-overlapping windows, exactly one at the target
    occupancy = store.get_copy("COPY-1")["occupancy"]
    assert len(occupancy) == 2
    moved = [o for o in occupancy if o["start_time"].startswith("2030-06-03")]
    assert len(moved) == 1


def test_concurrent_assign_and_reschedule_never_oversell(tmp_path):
    """Session A holds COPY-1 at slot T1 and races to reschedule into slot T2,
    while session B races to be assigned COPY-1 at its own slot T2. Exactly
    one of them gets the T2 occupancy; the copy is never double-booked."""
    store = Store(str(tmp_path / "copy-mix-race.db"))
    store.create_license(
        film_id="film-x", venue_id="hall-1",
        valid_from=datetime(2020, 1, 1, tzinfo=timezone.utc),
        valid_to=datetime(2031, 1, 1, tzinfo=timezone.utc),
        max_sessions=10,
    )
    store.register_copy(
        "film-x", duration_minutes=120, transfer_minutes=45, copy_code="COPY-1"
    )
    t1 = datetime(2030, 6, 1, 10, 0, tzinfo=timezone.utc)
    t2 = datetime(2030, 6, 2, 10, 0, tzinfo=timezone.utc)
    store.verify(film_id="film-x", venue_id="hall-1",
                 start_time=t1, idempotency_key="s-1")
    store.verify(film_id="film-x", venue_id="hall-1",
                 start_time=t2, idempotency_key="s-2")
    store.assign_copy("s-1", copy_code="COPY-1")

    outcomes, errors = [], []
    barrier = threading.Barrier(2)

    def rescheduler() -> None:
        barrier.wait()
        try:
            outcomes.append(
                ("reschedule", store.reschedule_verification(
                    "s-1", new_start_time=t2, operation_key="op-move"))
            )
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    def assigner() -> None:
        barrier.wait()
        try:
            outcomes.append(("assign", store.assign_copy("s-2", copy_code="COPY-1")))
        except service.ConflictError as exc:
            outcomes.append(("assign", {"conflict": True, "code": exc.code}))
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    ta = threading.Thread(target=rescheduler)
    tb = threading.Thread(target=assigner)
    ta.start()
    tb.start()
    ta.join()
    tb.join()

    assert not errors
    assert len(outcomes) == 2
    # exactly one of the two operations wins the T2 slot
    res = dict(outcomes)["reschedule"]
    asg = dict(outcomes)["assign"]
    if res["decision"] == "approved":
        # reschedule won the slot; the assignment must have conflicted
        assert asg.get("conflict") and asg["code"] == "COPY_CONFLICT"
        assert store.get_copy_assignment("s-2")["assigned"] is False
    else:
        # assignment won the slot; the reschedule is rejected and stays put
        assert res["reject_reason"] == "COPY_CONFLICT"
        assert asg["replay"] is False
        assert store.get_verification("s-1")["start_time"].startswith("2030-06-01")
        assert store.get_copy_assignment("s-2")["assigned"] is True

    # whatever the order, COPY-1 has exactly one occupancy at T2 and no overlap
    occupancy = store.get_copy("COPY-1")["occupancy"]
    at_t2 = [o for o in occupancy if o["start_time"].startswith("2030-06-02")]
    assert len(at_t2) == 1
    assert len(occupancy) == 2
