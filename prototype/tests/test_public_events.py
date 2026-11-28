"""Code audit, nineteenth pass (`public-events-v1`): the public job page and event ids.

`GET /v1/jobs/{id}` needs no login and listed each event with its `id`. Ids count up
across the whole ledger, one per event, so they put events of *different* jobs in one exact
order. A re-check is created inside the transaction that accepts the result it re-checks:

    id 9   result_accepted   (original job, public)
    id 10  job_created       (the re-check, public)
    id 11  audit_scheduled   (private)
    id 12  auditor_assigned  (private)

So a rig handed a re-check could read its own job page (job_created id 10), page through the
public ledger, open each listed job and find the one whose `result_accepted` is id 9: it knew
at once that its job was a re-check and of whose work, however busy the coordinator was.
The rig whose work was re-checked could do the same the other way round and count its own
re-checks (its trust tier). Gaps in one job's ids also showed where private events were.

The public page now shows events without ids, in the same real order. The operator's own
views keep them.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from app.ledger import PUBLIC_EVENT_COLUMNS
from tests.test_aimed_tests import aim
from tests.test_audits import OP, T0, claim, llm_payload, make, pinned, sha, submit  # noqa: F401

DEMO = "demo:sha256"


# -- the attack, written once, run on public data and on the operator's data ------------------
def public_events(client, job_id: str) -> list[dict[str, Any]]:
    r = client.get(f"/v1/jobs/{job_id}")
    assert r.status_code == 200, r.text
    return r.json()["events"]


def public_job_ids(client) -> list[str]:
    """Every job id the public ledger lists, page by page."""
    ids, after = [], None
    while True:
        body = client.get("/v1/ledger", params={"limit": 100, **({"after": after}
                                                                 if after else {})}).json()
        ids += [e["job_id"] for e in body["entries"]]
        after = body["next_after"]
        if after is None:
            return ids


def link(mine: list[dict[str, Any]], others: dict[str, list[dict[str, Any]]]) -> str | None:
    """What a rig could do with event ids. A re-check is created in the same transaction
    that accepts the result it re-checks, so its `job_created` id is that `result_accepted`
    id plus one. Find the other job on either side of that pair; None without ids."""
    created = {e["id"] for e in mine if e["event"] == "job_created" and "id" in e}
    accepted = {e["id"] for e in mine if e["event"] == "result_accepted" and "id" in e}
    for job_id, events in others.items():
        for e in events:
            if "id" not in e:
                continue
            if e["event"] == "result_accepted" and e["id"] + 1 in created:
                return job_id  # my job was made by accepting theirs: I'm re-checking them
            if e["event"] == "job_created" and e["id"] - 1 in accepted:
                return job_id  # their job was made by accepting mine: it re-checks me
    return None


def public_link(client, job_id: str) -> str | None:
    others = {j: public_events(client, j) for j in public_job_ids(client) if j != job_id}
    return link(public_events(client, job_id), others)


def private_link(ledger, client, job_id: str) -> str | None:
    """The same attack on the operator's full event list, to show it is a real attack."""
    others = {j: ledger.events(j) for j in public_job_ids(client) if j != job_id}
    return link(ledger.events(job_id), others)


def queue(client, task_type: str, payload: str) -> str:
    r = client.post("/v1/jobs", json={"task_type": task_type, "payload": payload})
    assert r.status_code == 200, r.text
    return r.json()["job_id"]


def run(client, job_id: str, worker: str, answer: str) -> None:
    assert claim(client, worker).json()["job_id"] == job_id
    assert submit(client, job_id, worker, answer).status_code == 200


def recheck_in_a_busy_second(client, clock):
    """A batch is queued: five exact demo jobs (never re-checked) and one AI job. In one
    second wk-c finishes three demo jobs, wk-a the AI job, wk-c two more, so six results
    share that second and the time alone can't say which one a re-check belongs to.
    wk-b then takes the re-check. Returns (original, re-check)."""
    jobs = []
    for i in range(6):  # one second apart, so they're handed out in this order
        clock.t = T0 + i
        jobs.append(queue(client, "llm:generate", llm_payload()) if i == 3
                    else queue(client, DEMO, f"busy {i}"))
    clock.t = T0 + 50
    for i, job_id in enumerate(jobs):
        if i == 3:
            run(client, job_id, "wk-a", "The Salty Hoe")
        else:
            run(client, job_id, "wk-c", sha(f"busy {i}"))
    clock.t += 10
    recheck = claim(client, "wk-b").json()
    assert recheck["payload"] == llm_payload() and recheck["job_id"] != jobs[3]
    clock.t += 10
    assert submit(client, recheck["job_id"], "wk-b", "The Salty Hoe").status_code == 200
    return jobs[3], recheck["job_id"]


# -- tests ------------------------------------------------------------------------------------
def test_the_attack_is_real_on_the_operators_data(pinned, tmp_path: Path) -> None:
    """Guards the tests below: with ids, the attack finds the pair both ways, even in a
    second with five other results in it."""
    client, clock, ledger = make(tmp_path)
    original, recheck = recheck_in_a_busy_second(client, clock)
    same_second = [e for e in client.get("/v1/ledger", headers=OP).json()["entries"]
                   if e["accepted_at"] == T0 + 50]
    assert len(same_second) == 6  # the time can't tell them apart
    assert private_link(ledger, client, recheck) == original
    assert private_link(ledger, client, original) == recheck


def test_a_rig_cant_link_its_recheck_to_the_original(pinned, tmp_path: Path) -> None:
    # Before the fix: public_link(recheck) == original, from public pages only.
    client, clock, ledger = make(tmp_path)
    original, recheck = recheck_in_a_busy_second(client, clock)
    assert private_link(ledger, client, recheck) == original  # the attack is real...
    assert public_link(client, recheck) is None  # ...but the public pages can't feed it


def test_a_rig_cant_find_the_recheck_of_its_own_work(pinned, tmp_path: Path) -> None:
    # Before the fix: public_link(original) == recheck, so a rig could count its re-checks.
    client, clock, ledger = make(tmp_path)
    original, recheck = recheck_in_a_busy_second(client, clock)
    assert private_link(ledger, client, original) == recheck
    assert public_link(client, original) is None


def test_public_events_have_only_the_listed_fields(pinned, tmp_path: Path) -> None:
    """Every kind of job: normal work, re-check, tie-breaker, hidden test, aimed test."""
    client, clock, _ = make(tmp_path)
    original, recheck = recheck_in_a_busy_second(client, clock)
    disputed = client.post("/v1/jobs", json={"task_type": "llm:generate",
                                             "payload": llm_payload("Name a dinghy.")}).json()
    assert claim(client, "wk-a").json()["job_id"] == disputed["job_id"]
    submit(client, disputed["job_id"], "wk-a", "Dinghy One")
    audit = claim(client, "wk-b").json()["job_id"]
    submit(client, audit, "wk-b", "Something else")
    tiebreak = claim(client, "wk-c").json()["job_id"]
    hidden = client.post("/v1/challenges", headers=OP, json={
        "task_type": "llm:generate", "payload": llm_payload("Name a ship."),
        "expected_result": "The Flagship"}).json()["job_id"]
    aimed = aim(client, "Name a yacht.", target_worker="wk-a").json()["job_id"]
    jobs = [original, recheck, disputed["job_id"], audit, tiebreak, hidden, aimed]
    for job_id in jobs:
        body = client.get(f"/v1/jobs/{job_id}").json()
        assert body["events"], job_id
        for e in body["events"]:
            assert tuple(e) == PUBLIC_EVENT_COLUMNS, e
        assert body["public_rules"] == ["public-events-v1"]
    assert PUBLIC_EVENT_COLUMNS == ("job_id", "worker_id", "event", "detail", "at")


def test_no_public_reply_carries_an_event_id(pinned, tmp_path: Path) -> None:
    """Nowhere a stranger can read (ledger, health, every job page) holds an `id` key."""
    client, clock, _ = make(tmp_path)
    recheck_in_a_busy_second(client, clock)

    def keys(value: Any) -> set[str]:
        if isinstance(value, dict):
            return set(value) | set().union(*(keys(v) for v in value.values()))
        if isinstance(value, list):
            return set().union(*(keys(v) for v in value)) if value else set()
        return set()

    replies = [client.get("/v1/ledger").json(), client.get("/health").json()]
    replies += [client.get(f"/v1/jobs/{j}").json() for j in public_job_ids(client)]
    assert len(replies) == 2 + 6 + 1
    assert all("id" not in keys(r) for r in replies)


def test_public_events_keep_their_real_order_within_one_second(pinned, tmp_path: Path) -> None:
    """Without ids the page still lists events in the order they happened, even when they
    all share one second (sorting by time, or by time and name, would reorder them)."""
    client, _, ledger = make(tmp_path)
    job_id = queue(client, DEMO, "count me")
    assert claim(client, "wk-a").json()["job_id"] == job_id
    assert submit(client, job_id, "wk-a", sha("wrong")).status_code == 422  # requeued
    run(client, job_id, "wk-b", sha("count me"))
    page = public_events(client, job_id)
    assert [e["event"] for e in page] == ["job_created", "job_leased",
                                          "result_rejected_requeued", "job_leased",
                                          "result_accepted"]
    assert [e["worker_id"] for e in page] == [None, "wk-a", "wk-a", "wk-b", "wk-b"]
    assert len({e["at"] for e in page}) == 1
    private = [(e["event"], e["worker_id"]) for e in ledger.events(job_id)]
    assert [(e["event"], e["worker_id"]) for e in page] == private


def test_operator_views_keep_event_ids(pinned, tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path)
    original, _ = recheck_in_a_busy_second(client, clock)
    ids = [e["id"] for e in ledger.events(original)]
    assert ids == sorted(ids) and len(ids) >= 3


def test_public_page_still_hides_private_events(pinned, tmp_path: Path) -> None:
    client, clock, ledger = make(tmp_path)
    original, recheck = recheck_in_a_busy_second(client, clock)
    private = {e["event"] for j in (original, recheck) for e in ledger.events(j)}
    assert {"audit_scheduled", "auditor_assigned", "audit_match"} <= private
    for job_id in (original, recheck):
        shown = {e["event"] for e in public_events(client, job_id)}
        assert shown <= {"job_created", "job_leased", "result_accepted"}
        body = client.get(f"/v1/jobs/{job_id}").json()
        assert body["events_total"] == len(body["events"])
