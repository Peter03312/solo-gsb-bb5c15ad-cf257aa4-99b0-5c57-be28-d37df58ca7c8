"""实体放映拷贝：登记、分配、占用冲突、取消释放、改签重检、领码门禁与并发不超订。"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.service import Store

FUTURE = "2030-06-01T20:00:00Z"


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def make_license(client, *, film="film-copy", venue="hall-a",
                 valid_from="2020-01-01T00:00:00Z",
                 valid_to="2040-01-01T00:00:00Z", cap=10):
    resp = client.post(
        "/licenses",
        json={
            "film_id": film, "venue_id": venue,
            "valid_from": valid_from, "valid_to": valid_to,
            "max_sessions": cap,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def register_copy(client, code="cp-1", *, film="film-copy", duration=100,
                  transport=30, expected=201):
    resp = client.post(
        "/copies",
        json={
            "film_id": film, "copy_code": code,
            "duration_minutes": duration, "transport_minutes": transport,
        },
    )
    assert resp.status_code == expected, resp.text
    return resp.json()


def verify(client, key, *, film="film-copy", venue="hall-a", start=FUTURE):
    resp = client.post(
        "/verifications",
        json={"film_id": film, "venue_id": venue,
              "start_time": start, "idempotency_key": key},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def assign(client, key, code, expected=200):
    resp = client.post(
        f"/verifications/{key}/copy-assignment", json={"copy_code": code}
    )
    assert resp.status_code == expected, resp.text
    return resp.json()


# ----------------------------------------------------------------------
# registration
# ----------------------------------------------------------------------
def test_register_copy_returns_configuration(client):
    make_license(client)
    cp = register_copy(client, "cp-1")
    assert cp["copy_code"] == "cp-1"
    assert cp["duration_minutes"] == 100
    assert cp["transport_minutes"] == 30
    assert cp["used_sessions"] == 0
    assert cp["occupancies"] == []


def test_register_validation_errors(client):
    make_license(client)
    for payload in (
        {"film_id": "f", "copy_code": "x", "duration_minutes": 0, "transport_minutes": 0},
        {"film_id": "f", "copy_code": "x", "duration_minutes": 1, "transport_minutes": -1},
        {"film_id": "f", "copy_code": "", "duration_minutes": 1, "transport_minutes": 0},
    ):
        assert client.post("/copies", json=payload).status_code == 422


def test_copy_code_must_be_unique(client):
    register_copy(client, "dup", film="f1")
    body = client.post(
        "/copies",
        json={"film_id": "f2", "copy_code": "dup",
              "duration_minutes": 100, "transport_minutes": 10},
    )
    assert body.status_code == 422
    assert body.json()["code"] == "COPY_CODE_TAKEN"


def test_same_film_metadata_must_match(client):
    register_copy(client, "cp-1", duration=100, transport=30)
    body = register_copy(client, "cp-2", duration=101, transport=30, expected=422)
    assert body["code"] == "COPY_METADATA_MISMATCH"
    body = register_copy(client, "cp-2", duration=100, transport=31, expected=422)
    assert body["code"] == "COPY_METADATA_MISMATCH"
    # identical metadata adds a second copy
    assert register_copy(client, "cp-2", duration=100, transport=30)["id"] == 2


def test_registration_blocked_after_first_approval(client):
    make_license(client)
    verify(client, "v-1")
    body = register_copy(client, "late", expected=422)
    assert body["code"] == "COPY_FILM_ALREADY_IN_USE"


def test_registration_allowed_for_other_film_after_approval(client):
    make_license(client, film="film-a", venue="h")
    verify(client, "v-1", film="film-a", venue="h")
    # a different film has no approvals yet
    cp = register_copy(client, "other", film="film-b")
    assert cp["film_id"] == "film-b"


def test_registration_blocked_even_when_approval_later_cancelled(client):
    make_license(client)
    verify(client, "v-1")
    client.post("/verifications/v-1/cancel", json={"reason": "x"})
    body = register_copy(client, "late", expected=422)
    assert body["code"] == "COPY_FILM_ALREADY_IN_USE"


def test_list_and_get_copies(client):
    register_copy(client, "cp-1", film="f-a")
    register_copy(client, "cp-2", film="f-b")
    assert len(client.get("/copies").json()) == 2
    assert len(client.get("/copies", params={"film_id": "f-a"}).json()) == 1
    detail = client.get("/copies/cp-1").json()
    assert detail["copy_code"] == "cp-1"
    assert detail["occupancies"] == []
    assert client.get("/copies/nope").status_code == 404


# ----------------------------------------------------------------------
# assignment: gates, idempotency, atomic replacement
# ----------------------------------------------------------------------
def test_assign_copy_happy_path(client):
    make_license(client)
    register_copy(client, "cp-1")
    verify(client, "s-1")
    out = assign(client, "s-1", "cp-1")
    assert out["copy_code"] == "cp-1"
    assert out["replaced"] is False
    assert out["replay"] is False
    assert out["released"] is False
    assert out["start_time"] == "2030-06-01T20:00:00.000000Z"


def test_assign_same_copy_replays_current_state(client):
    make_license(client)
    register_copy(client)
    verify(client, "s-1")
    first = assign(client, "s-1", "cp-1")
    again = assign(client, "s-1", "cp-1")
    assert again["replay"] is True
    assert again["replaced"] is False
    assert again["assigned_at"] == first["assigned_at"]
    assert client.get("/copies/cp-1").json()["used_sessions"] == 1


def test_switching_copy_atomically_replaces(client):
    make_license(client)
    register_copy(client, "cp-1")
    register_copy(client, "cp-2")
    verify(client, "s-1")
    assign(client, "s-1", "cp-1")
    out = assign(client, "s-1", "cp-2")
    assert out["replaced"] is True
    assert out["replay"] is False
    assert out["copy_code"] == "cp-2"
    assert client.get("/copies/cp-1").json()["used_sessions"] == 0
    assert client.get("/copies/cp-2").json()["used_sessions"] == 1
    # one assignment row only
    status = client.get("/verifications/s-1/copy-assignment").json()
    assert status["assigned"] is True
    assert status["copy_code"] == "cp-2"


def test_replacement_conflict_leaves_original_assignment(client):
    make_license(client, venue="hall-a")
    make_license(client, venue="hall-b")
    register_copy(client, "cp-1")
    register_copy(client, "cp-2")
    verify(client, "a", venue="hall-a", start="2030-06-01T19:00:00Z")
    assign(client, "a", "cp-1")
    # switch a to free copy cp-2
    assert assign(client, "a", "cp-2")["replaced"] is True
    # blocker c takes cp-1 at a clashing slot; switching a back must fail
    verify(client, "c", venue="hall-a", start="2030-06-01T19:30:00Z")
    assign(client, "c", "cp-1")
    body = assign(client, "a", "cp-1", expected=422)
    assert body["code"] == "COPY_CONFLICT"
    assert [x["idempotency_key"] for x in body["conflicts"]] == ["c"]
    status = client.get("/verifications/a/copy-assignment").json()
    assert status["copy_code"] == "cp-2"  # unchanged
    assert client.get("/copies/cp-1").json()["used_sessions"] == 1
    assert client.get("/copies/cp-2").json()["used_sessions"] == 1


def test_assignment_gates(client):
    make_license(client)
    register_copy(client)
    # unknown verification
    body = client.post(
        "/verifications/nope/copy-assignment", json={"copy_code": "cp-1"}
    )
    assert body.status_code == 404
    # rejected verification
    client.post(
        "/verifications",
        json={"film_id": "other", "venue_id": "x",
              "start_time": FUTURE, "idempotency_key": "rej"},
    )
    body = assign(client, "rej", "cp-1", expected=422)
    assert body["code"] == "VERIFICATION_NOT_APPROVED"
    # unknown copy
    verify(client, "s-1")
    body = assign(client, "s-1", "nope", expected=422)
    assert body["code"] == "COPY_NOT_FOUND"
    # copy of another film
    register_copy(client, "gx", film="other-film")
    body = assign(client, "s-1", "gx", expected=422)
    assert body["code"] == "COPY_FILM_MISMATCH"
    # cancelled session
    assign(client, "s-1", "cp-1")
    client.post("/verifications/s-1/cancel", json={"reason": "x"})
    body = assign(client, "s-1", "cp-1", expected=422)
    assert body["code"] == "SESSION_CANCELLED"


def test_cannot_assign_to_started_session(client, monkeypatch):
    import app.service as service
    make_license(client)
    register_copy(client)
    verify(client, "s-1", start="2030-06-01T20:00:00Z")
    monkeypatch.setattr(
        service, "now_iso", lambda: "2030-06-01T20:00:00.000000Z"
    )
    body = assign(client, "s-1", "cp-1", expected=422)
    assert body["code"] == "SESSION_ALREADY_STARTED"


# ----------------------------------------------------------------------
# occupancy rules: same-venue overlap, cross-venue transport, boundaries
# ----------------------------------------------------------------------
def _two_venues(client):
    make_license(client, venue="hall-a")
    make_license(client, venue="hall-b")
    register_copy(client, "cp-1")


def test_same_venue_half_open_overlap(client):
    _two_venues(client)
    verify(client, "a", venue="hall-a", start="2030-06-01T19:00:00Z")
    assign(client, "a", "cp-1")
    # [19:00, 20:40): 20:39 overlaps, 20:40 is free (touching boundary)
    verify(client, "b", venue="hall-a", start="2030-06-01T20:39:00Z")
    body = assign(client, "b", "cp-1", expected=422)
    assert body["code"] == "COPY_CONFLICT"
    assert body["conflicts"][0]["relation"] == "same_venue_overlap"
    verify(client, "c", venue="hall-a", start="2030-06-01T20:40:00Z")
    assert assign(client, "c", "cp-1")["copy_code"] == "cp-1"


def test_cross_venue_transport_exact_boundary_allowed(client):
    _two_venues(client)
    verify(client, "a", venue="hall-a", start="2030-06-01T19:00:00Z")
    assign(client, "a", "cp-1")
    # ready in hall-b = 19:00 + 100 + 30 = 21:10 exactly
    verify(client, "b", venue="hall-b", start="2030-06-01T21:09:00Z")
    body = assign(client, "b", "cp-1", expected=422)
    assert body["conflicts"][0]["relation"] == "cross_venue_transport"
    verify(client, "c", venue="hall-b", start="2030-06-01T21:10:00Z")
    assert assign(client, "c", "cp-1")["copy_code"] == "cp-1"


def test_cross_venue_transport_checked_in_both_directions(client):
    _two_venues(client)
    # Rule applied symmetrically when the PLANNED screening is earlier and
    # the existing occupancy later in another venue: the copy must leave the
    # target venue, travel, and arrive before the later screening starts.
    # Existing: hall-a 20:00, occupies [20:00,21:40), copy must be in
    # hall-a by 20:00. Target hall-b 17:31 occupies to 19:11, ready in
    # hall-a 19:41 <= 20:00 (feasible); 17:32 ends 19:12 -> ready 19:42
    # > 20:00? No: 19:42 <= 20:00 too. Latest feasible target start is
    # 17:50 (ends 19:30, ready 20:00 exactly); 17:51 is one minute too late.
    verify(client, "late", venue="hall-a", start="2030-06-01T20:00:00Z")
    assign(client, "late", "cp-1")
    verify(client, "early-bad", venue="hall-b", start="2030-06-01T17:51:00Z")
    body = assign(client, "early-bad", "cp-1", expected=422)
    assert body["conflicts"][0]["relation"] == "cross_venue_transport"
    verify(client, "early-ok", venue="hall-b", start="2030-06-01T17:50:00Z")
    assert assign(client, "early-ok", "cp-1")["copy_code"] == "cp-1"


def test_conflict_never_books(client):
    _two_venues(client)
    verify(client, "a", venue="hall-a", start="2030-06-01T19:00:00Z")
    assign(client, "a", "cp-1")
    verify(client, "b", venue="hall-a", start="2030-06-01T19:30:00Z")
    assign(client, "b", "cp-1", expected=422)
    # no row exists for b; copy still has exactly one occupancy
    st = client.get("/verifications/b/copy-assignment").json()
    assert st["assigned"] is False
    detail = client.get("/copies/cp-1").json()
    assert detail["used_sessions"] == 1
    assert [o["idempotency_key"] for o in detail["occupancies"]] == ["a"]


def test_occupancies_and_probe(client):
    _two_venues(client)
    verify(client, "a", venue="hall-a", start="2030-06-01T19:00:00Z")
    assign(client, "a", "cp-1")
    occ = client.get("/copies/cp-1/occupancies").json()
    assert occ == [{
        "idempotency_key": "a",
        "venue_id": "hall-a",
        "start_time": "2030-06-01T19:00:00.000000Z",
        "end_time": "2030-06-01T20:40:00.000000Z",
        "ready_time": "2030-06-01T21:10:00.000000Z",
    }]
    bad = client.post(
        "/copies/conflicts",
        json={"film_id": "film-copy", "copy_code": "cp-1",
              "venue_id": "hall-b", "start_time": "2030-06-01T21:00:00Z"},
    ).json()
    assert bad["conflict"] is True
    good = client.post(
        "/copies/conflicts",
        json={"film_id": "film-copy", "copy_code": "cp-1",
              "venue_id": "hall-b", "start_time": "2030-06-01T21:10:00Z"},
    ).json()
    assert good["conflict"] is False
    # excluding a simulates the move/replacement self-release
    excluded = client.post(
        "/copies/conflicts",
        json={"film_id": "film-copy", "copy_code": "cp-1",
              "venue_id": "hall-a", "start_time": "2030-06-01T19:00:00Z",
              "exclude_idempotency_key": "a"},
    ).json()
    assert excluded["conflict"] is False
    # probe validation
    r = client.post(
        "/copies/conflicts",
        json={"film_id": "film-copy", "copy_code": "nope",
              "venue_id": "hall-a", "start_time": FUTURE},
    )
    assert r.status_code == 404
    r = client.post(
        "/copies/conflicts",
        json={"film_id": "other", "copy_code": "cp-1",
              "venue_id": "hall-a", "start_time": FUTURE},
    )
    assert r.status_code == 422 and r.json()["code"] == "COPY_FILM_MISMATCH"


# ----------------------------------------------------------------------
# cancellation releases assignments
# ----------------------------------------------------------------------
def test_cancel_releases_copy_and_response_carries_copy_code(client):
    make_license(client)
    register_copy(client, "cp-1")
    register_copy(client, "cp-2")
    verify(client, "a", start="2030-06-01T19:00:00Z")
    assign(client, "a", "cp-1")
    can = client.post("/verifications/a/cancel", json={"reason": "故障"}).json()
    assert can["copy_code"] == "cp-1"
    assert client.get("/copies/cp-1").json()["used_sessions"] == 0
    # freed slot is reusable by another screening
    verify(client, "b", start="2030-06-01T19:00:00Z")
    assert assign(client, "b", "cp-1")["copy_code"] == "cp-1"
    # trace retained: GET status shows released
    st = client.get("/verifications/a/copy-assignment").json()
    assert st["assigned"] is True
    assert st["released"] is True
    assert st["copy_code"] == "cp-1"
    view = client.get("/verifications/a").json()
    assert view["copy_code"] == "cp-1"
    assert view["copy_released"] is True


def test_cancel_without_copy_has_null_copy_code(client):
    make_license(client)
    verify(client, "a")
    can = client.post("/verifications/a/cancel", json={"reason": "x"}).json()
    assert can["copy_code"] is None


# ----------------------------------------------------------------------
# reschedule re-checks copy occupancy in the same transaction
# ----------------------------------------------------------------------
def reschedule(client, key, new_time, op):
    return client.post(
        f"/verifications/{key}/reschedule",
        json={"new_start_time": new_time, "operation_key": op},
    )


def test_reschedule_copy_conflict_is_rejected_unchanged_and_replayable(client):
    make_license(client, venue="hall-a")
    make_license(client, venue="hall-b")
    register_copy(client, "cp-1")
    register_copy(client, "cp-2")
    verify(client, "a", venue="hall-a", start="2030-06-01T20:30:00Z")
    assign(client, "a", "cp-2")
    verify(client, "b", venue="hall-b", start="2030-07-01T19:00:00Z")
    assign(client, "b", "cp-2")

    # move b to 20:00 hall-b: a (hall-a, ends 22:10) copy ready 22:40 -> clash
    r = reschedule(client, "b", "2030-06-01T20:00:00Z", "op-b-1")
    j = r.json()
    assert j["decision"] == "rejected"
    assert j["reject_reason"] == "COPY_CONFLICT"
    assert j["to_license_id"] is None
    assert j["copy_conflicts"][0]["idempotency_key"] == "a"
    # schedule / assignment / quota untouched
    cur = client.get("/verifications/b").json()
    assert cur["start_time"].startswith("2030-07-01")
    assert cur["copy_code"] == "cp-2"
    # replay returns the same persisted snapshot
    again = reschedule(client, "b", "2030-06-01T20:00:00Z", "op-b-1").json()
    assert again["replay"] is True
    assert again["copy_conflicts"] == j["copy_conflicts"]
    # reschedule history exposes the rejected trace
    hist = client.get(
        "/reschedules", params={"idempotency_key": "b"}
    ).json()
    assert hist[0]["reject_reason"] == "COPY_CONFLICT"


def test_successful_reschedule_keeps_assignment_and_uses_new_slot(client):
    make_license(client, venue="hall-a")
    make_license(client, venue="hall-b")
    register_copy(client, "cp-1")
    verify(client, "b", venue="hall-b", start="2030-07-01T19:00:00Z")
    assign(client, "b", "cp-1")
    r = reschedule(client, "b", "2030-09-01T19:00:00Z", "op-b-1")
    assert r.json()["decision"] == "approved"
    cur = client.get("/verifications/b").json()
    assert cur["start_time"].startswith("2030-09-01")
    assert cur["copy_code"] == "cp-1"
    occ = client.get("/copies/cp-1").json()["occupancies"]
    assert occ[0]["start_time"].startswith("2030-09-01")


def test_unassigned_screening_reschedule_has_no_copy_gate(client):
    make_license(client)
    # film configured with copies, but this screening never got one
    register_copy(client, "cp-1")
    verify(client, "b", start="2030-07-01T19:00:00Z")
    r = reschedule(client, "b", "2030-09-01T19:00:00Z", "op-b-1")
    assert r.json()["decision"] == "approved"


def test_reschedule_boundary_exact_is_allowed(client):
    make_license(client, venue="hall-a")
    make_license(client, venue="hall-b")
    register_copy(client, "cp-1")
    verify(client, "a", venue="hall-a", start="2030-06-01T20:30:00Z")
    assign(client, "a", "cp-1")
    verify(client, "b", venue="hall-b", start="2030-07-01T19:00:00Z")
    assign(client, "b", "cp-1")
    # a ends 22:10 + 30 transport -> 22:40 exactly is allowed
    r = reschedule(client, "b", "2030-06-01T22:40:00Z", "op-b-1")
    assert r.json()["decision"] == "approved"


# ----------------------------------------------------------------------
# opening-code gate for films with configured copies
# ----------------------------------------------------------------------
def test_claim_requires_copy_when_film_configured(client):
    make_license(client)
    register_copy(client, "cp-1")
    verify(client, "a")
    body = client.post("/verifications/a/opening-code")
    assert body.status_code == 422
    assert body.json()["code"] == "COPY_NOT_ASSIGNED"
    assign(client, "a", "cp-1")
    assert client.post("/verifications/a/opening-code").status_code == 200


def test_film_without_copies_keeps_legacy_claim_behavior(client):
    make_license(client, film="legacy", venue="h")
    client.post(
        "/verifications",
        json={"film_id": "legacy", "venue_id": "h",
              "start_time": "2030-06-01T20:00:00Z", "idempotency_key": "l1"},
    )
    r = client.post("/verifications/l1/opening-code")
    assert r.status_code == 200 and r.json()["code"]


# ----------------------------------------------------------------------
# concurrency: a copy must never be overbooked
# ----------------------------------------------------------------------
def test_concurrent_assignments_never_overbook_single_copy(tmp_path):
    """16 线程把同一份拷贝分配给同场馆同一时段的不同场次：至多一胜。"""
    store = Store(str(tmp_path / "cc.db"))
    store.create_license(
        film_id="fx", venue_id="h1",
        valid_from=datetime(2020, 1, 1, tzinfo=timezone.utc),
        valid_to=datetime(2040, 1, 1, tzinfo=timezone.utc),
        max_sessions=100,
    )
    store.register_copy(
        film_id="fx", copy_code="only",
        duration_minutes=100, transport_minutes=0,
    )
    n = 16
    for i in range(n):
        store.verify(
            film_id="fx", venue_id="h1",
            start_time=datetime(2030, 6, 1, 20, 0, tzinfo=timezone.utc),
            idempotency_key=f"k-{i}",
        )
    ok, conflicts, errors = [], [], []

    def worker(i):
        try:
            store.assign_copy(f"k-{i}", copy_code="only")
            ok.append(i)
        except Exception as exc:
            if getattr(exc, "code", None) == "COPY_CONFLICT":
                conflicts.append(i)
            else:  # pragma: no cover
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(ok) == 1
    assert len(conflicts) == n - 1
    detail = store.get_copy("only")
    assert detail["used_sessions"] == 1


def test_concurrent_copy_replacement_never_double_books(tmp_path):
    """12 线程把同一场次从 cp-1 换到各自冲突的不同拷贝时段，超订为 0。

    cp-1 为该场次当前占用；cp-2 上已放一场与目标冲突的占用，因此所有
    换拷贝请求都应 422 COPY_CONFLICT，分配始终留在 cp-1。
    """
    store = Store(str(tmp_path / "cc2.db"))
    for venue in ("h1", "h2"):
        store.create_license(
            film_id="fx", venue_id=venue,
            valid_from=datetime(2020, 1, 1, tzinfo=timezone.utc),
            valid_to=datetime(2040, 1, 1, tzinfo=timezone.utc),
            max_sessions=100,
        )
    store.register_copy(film_id="fx", copy_code="cp-1",
                        duration_minutes=100, transport_minutes=30)
    store.register_copy(film_id="fx", copy_code="cp-2",
                        duration_minutes=100, transport_minutes=30)
    # target screening (h1, 20:00) currently on cp-1
    store.verify(film_id="fx", venue_id="h1",
                 start_time=datetime(2030, 6, 1, 20, 0, tzinfo=timezone.utc),
                 idempotency_key="target")
    store.assign_copy("target", copy_code="cp-1")
    # blocker on cp-2 same venue overlapping slot
    store.verify(film_id="fx", venue_id="h1",
                 start_time=datetime(2030, 6, 1, 20, 30, tzinfo=timezone.utc),
                 idempotency_key="blocker")
    store.assign_copy("blocker", copy_code="cp-2")

    errors = []

    def worker():
        try:
            store.assign_copy("target", copy_code="cp-2")
        except Exception as exc:
            if getattr(exc, "code", None) != "COPY_CONFLICT":  # pragma: no cover
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    status = store.get_assignment_status("target")
    assert status["copy_code"] == "cp-1"
    assert store.get_copy("cp-1")["used_sessions"] == 1
    assert store.get_copy("cp-2")["used_sessions"] == 1


def test_concurrent_reschedule_and_assignment_never_overbook(tmp_path):
    """8 路并发把已分配拷贝的场次改签到与既有占用冲突的同一时段，拷贝不超订。

    print-1 已被 blocker 占在 hall-a 20:00（[20:00,21:40)，运抵 hall-b
    22:25）。8 路各自持有 print-1 的场次（分散在空闲日期）并发改签到
    hall-b 21:00——与 blocker 跨场馆转运不达标，全部必须 rejected/
    COPY_CONFLICT；print-1 上最终仍是 9 场各自分散的占用，没有任何两场
    挤到 21:00，即不超订。
    """
    store = Store(str(tmp_path / "cc3.db"))
    for venue in ("h1", "h2"):
        store.create_license(
            film_id="fx", venue_id=venue,
            valid_from=datetime(2020, 1, 1, tzinfo=timezone.utc),
            valid_to=datetime(2040, 1, 1, tzinfo=timezone.utc),
            max_sessions=100,
        )
    store.register_copy(film_id="fx", copy_code="print-1",
                        duration_minutes=100, transport_minutes=45)
    store.verify(film_id="fx", venue_id="h1",
                 start_time=datetime(2030, 6, 1, 20, 0, tzinfo=timezone.utc),
                 idempotency_key="blocker")
    store.assign_copy("blocker", copy_code="print-1")
    n = 8
    # each mover starts on its own free day in hall-b and holds print-1
    for i in range(n):
        store.verify(film_id="fx", venue_id="h2",
                     start_time=datetime(2030, 7, 1 + i, 19, 0,
                                         tzinfo=timezone.utc),
                     idempotency_key=f"m-{i}")
        store.assign_copy(f"m-{i}", copy_code="print-1")

    outcomes = []

    def worker(i):
        try:
            out = store.reschedule_verification(
                f"m-{i}",
                new_start_time=datetime(2030, 6, 1, 21, 0,
                                        tzinfo=timezone.utc),
                operation_key=f"m-{i}-op",
            )
            outcomes.append((out["decision"], out["reject_reason"]))
        except Exception as exc:  # pragma: no cover
            outcomes.append(("error", str(exc)))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(outcomes) == n
    assert all(decision == "rejected" and reason == "COPY_CONFLICT"
               for decision, reason in outcomes), outcomes
    detail = store.get_copy("print-1")
    # blocker plus all 8 movers still on their own original days — no overbook
    assert detail["used_sessions"] == n + 1
    for i in range(n):
        cur = store.get_verification(f"m-{i}")
        assert cur["start_time"].startswith(f"2030-07-{1 + i:02d}")
        assert cur["copy_code"] == "print-1"
