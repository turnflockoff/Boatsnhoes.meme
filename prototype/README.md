# Boats N Hoes — Useful Compute MVP

This directory contains an early local coordinator/worker scaffold for the RX 470 useful-compute prototype. It is not a blockchain miner and does not use Ethereum consensus.

## Implemented (Phase 1: durable local MVP)

- FastAPI coordinator built by `create_app()`; all job state lives in SQLite (WAL mode), so jobs and leases **survive a coordinator restart**.
- Explicit job state machine: `queued → leased → accepted`, with `rejected`, `expired` and `failed` final states. Every transition is written to an `events` audit table.
- **Atomic claiming** with `BEGIN IMMEDIATE` transactions: two workers, or two coordinator processes on the same database, can't lease the same job.
- **Leases** (`LEASE_SECONDS`, default 60): if a worker goes silent, its job is requeued. After `MAX_ATTEMPTS` (default 3) the job is marked `failed`. Wrong answers requeue the job until attempts run out, then it's `rejected`.
- **Idempotent submissions**: resubmitting the same accepted result returns the original receipt and is paid only once. A different worker submitting after acceptance gets `409`. Late submissions from a worker that lost its lease get `403`. Expired jobs get `410`.
- **The worker verifies the coordinator's Ed25519 manifest signature** (HMAC-SHA256 until 0.5.9), the payload hash, the task type and the deadline before it executes anything. A job signed by a different coordinator is refused.
- `GET /v1/ledger` includes job counts and `consistency_problems`, an invariant check an auditor can run at any time. `GET /v1/jobs/{id}` shows a job's status and audit trail.
- A local CPU demo worker that runs a deterministic SHA-256 task (a test fixture, not useful work).
- Vulkan device-enumeration diagnostic helper.
- 13 tests: restart persistence, lease expiry and requeue, retry exhaustion, job expiry, wrong worker, duplicate and conflicting submissions, malformed results, 20-thread concurrent claims, tampered or foreign manifests, and secrets kept out of API responses.

## Implemented (Phase 2: task interface + pinned models)

- **Pluggable task executors** (`app/tasks.py`). Every job type implements `validate`, `available`, `execute` and `verify`. The coordinator validates payloads when a job is created, and workers only advertise task types they can actually run.
- **Verdicts state how much they prove.** `exact` means the coordinator recomputed or fully checked the answer (`demo:sha256`). `schema-only` means the result is well-formed but unproven (`llm:generate`). Receipts and the ledger record `verifier_method`, `confidence` and `needs_audit`.
- **`llm:generate` task** for small text generation via llama.cpp: greedy decoding (temperature 0), a fixed seed, `max_tokens` capped at 256, and a 5–300 s timeout. The worker re-hashes the model file before every run.
- **Pinned model registry** (`models/registry.json`, `app/model_registry.py`) records source URL, upstream, license, approximate size and minimum VRAM. A model can't be used until its sha256 and size are **pinned from a file you downloaded**. The first candidate, Qwen2.5-0.5B-Instruct Q4_K_M (Apache-2.0, ~400 MB), ships **unpinned**.
- **Opt-in download only:** `python -m app.model_registry download <id> <dir> --yes-download`. Model weights are git-ignored.
- Older Phase 1 databases are migrated in place.
- 32 tests in total.

## Implemented (Phase 4: audits, tie-breakers and challenges)

A result hash is not proof that a GPU ran a model. Phase 4 adds the ways the coordinator gathers evidence anyway. Every check is tested in software only; none has run against real GPU output yet.

- **Hidden re-run audits.** When an unproven (`schema-only`) result is accepted, the coordinator re-queues the same job for a *different* worker at rate `AUDIT_RATE` (default 0.25, chosen with the OS's secure randomness). The original worker can never claim its own audit. The audit job is created in the same database transaction as the acceptance, and it gets the same time window as the original, so its deadline doesn't give it away.
- **Trust tiers for the audit rate** (`audit-tier-v1`, threat #1, since 0.5.15). The rate depends on the worker's own record, read from the ledger at the moment it submits: a rig with a **strike** (any `failed-challenge` or `outvoted` result) is `suspect` and re-checked at `AUDIT_RATE_SUSPECT` (default 1.0, every result); a rig with fewer than `AUDIT_NEW_UNTIL` (default 5) results that passed an independent check (`confirmed` or `passed-challenge`) is `new` and re-checked at `AUDIT_RATE_NEW` (default 0.5); everyone else is `established` at `AUDIT_RATE`. The rate used is never below `AUDIT_RATE`. Tiers change how often a rig is checked, **never its points**. The tier is operator-only (trust report, `/v1/audits` lists the policy, `/v1/operator/health` names rigs with a strike); it is not in any event, envelope or receipt. Limits: two ring members who audit each other can "confirm" fakes and graduate out of `new` (threat #3); strikes don't expire in v1, and until real GPU output is measured (threat #13) an honest rig could be outvoted over a tiny difference and get one; more re-checks cost the other rigs compute. Software only, 7 tests.
- **Tolerance rules** (`results_agree` on each task): SHA must match byte for byte; AI text must match after collapsing whitespace. Anything else (one word, letter case) is a disagreement.
- **Outcomes.** Agreement marks both results `confirmed` with confidence `replicated`. Disagreement marks both `disputed` and their points are held. An audit that never runs (expires, fails, or is rejected) leaves the original `unaudited`. Audits never spawn audits.
- **Hidden tie-breakers** (`TIEBREAK`, default on). When an audit disagrees, the coordinator queues the same job for a *third* worker that is neither side of the dispute, in the same transaction and with the same time window. Majority of three: if the tie-breaker agrees with exactly one side, that side and the tie-breaker become `confirmed` and the other side becomes `outvoted` (points stay held). If it agrees with neither (or both), all three are `unresolved` and held for a person. If no third worker picks it up, the dispute stays held and a `tiebreak_not_run` event is recorded. Tie-breakers never spawn more audits or tie-breakers.
- **Random auditor assignment** (closes threat #4). An audit or tie-breaker is not first-come. The moment it is queued, the coordinator picks one eligible worker at random (OS secure randomness) and only that worker can claim it. Eligible means: not revoked, not excluded (directly or through its owner), and active, i.e. it asked for a job, sent a result or enrolled within `AUDITOR_ACTIVE_SECONDS` (default 300). If the pick doesn't claim it within `AUDIT_ASSIGN_SECONDS` (default 120), is revoked, or gives it back (lease timeout, rejected result), it is re-assigned at random, avoiding the previous pick when anyone else is eligible. If nobody is eligible yet, the audit waits and is assigned on the next request. Polling faster no longer wins more audits. The pick is recorded as a private `auditor_assigned` event and shown to the operator in `/v1/audits`; workers never see it.
- **Audit follow-through** (threat #4 follow-up, since 0.5.17). Random picks stop a rig from *grabbing* audits, but a rig can still *let go* of ones it doesn't like and they move on to someone else. The trust report (`/v1/workers/{id}/trust`) now has `audit_follow_through`: how many audits/tie-breakers the rig was `assigned`, how many it `completed`, and how many it dropped, split into `not_claimed` (never picked up within `AUDIT_ASSIGN_SECONDS`, logged as a private `audit_assignment_expired` event), `lease_expired` (took it, then went silent) and `refused` (its result failed the shape check). A rig picked again for the same audit is judged on its latest pick only, a lease that ran out is never counted twice, and a pick the operator revoked is not a drop. `/v1/operator/health` lists every rig's drop count in `audit_dropouts` and warns about rigs at or above `AUDIT_DROP_WARN` (default 3). Lifetime counts, operator-only, and they change **nothing** about points or audit rates: a rig that just went offline looks exactly like one skipping checks on purpose, so this is a reason to look, not proof. Software only, 9 tests.
- **Who gets picked to audit** (`auditor-pick-v1`, threat #4 and #6, since 0.5.23). Random picks treat every eligible rig the same, so a rig that was just caught cheating (`suspect` under `audit-tier-v1`) still checks other rigs' work and can be the deciding vote in a tie-breaker. `AUDITOR_PICK=trusted-first` changes the pick for audits and tie-breakers: still random, but only among the most trusted tier that has an eligible rig, in the order `established`, `new`, `suspect`. A rig with a strike only audits when nobody else is eligible (better than no check at all). Tiers are worked out at the moment of each pick, the previous pick is still avoided first (so a silent established rig hands over to a `new` one, not straight back to itself), and same-owner, revoked and inactive rigs stay excluded as before. Hidden tests aimed at a rig or tier are unchanged. The private `auditor_assigned` event notes the pool (`candidates=1 pool=established`); ledger copies drop event details as before. `/v1/audits` shows the rule under `audit_policy.auditor_pick`, operator health shows `auditor_pick` and, while it's `uniform` and some rig has a strike, warns that the rig can still audit others. **Off by default (`uniform`)**: audit and tie-breaker work pays the same points as normal work, so `trusted-first` shifts that share toward established rigs, and a ring that has graduated its rigs to `established` (threat #3) gets more of the audits, not fewer. Switching it on is the owner's call. Software only, 11 tests.
- **Operator review queue** (threat #7, since 0.5.18). Some held points can't be settled by the software: a dispute whose tie-breaker never ran (often only two rigs online) or was never scheduled (`TIEBREAK=false`), an `unresolved` three-way disagreement, and a failed hidden test (cheat, wrong model, or honest GPU differences). `GET /v1/operator/review` (or `python -m app.review [--all] [--json]`, which opens the database read-only) lists every case with points held: its `stage` (`needs_review`, `waiting` for a tie-breaker, or `decided_by_majority`, listed for spot checks because two colluding rigs can outvote an honest one), why, the input, every answer side by side with the rig, owner label and strike count, which answers agree under the task's own tolerance rule, points held and how long. `?needs_person_only=true` hides the rest. `/v1/operator/health` adds `review` counts and a warning when anything needs a person. **Read-only**: there is no override; what a person's decision should do to points is part of scoring v2 (owner decision). It shows answers and hidden tests' expected answers, so it is operator-only and never logged. Software only, 13 tests.
- **Hidden challenge jobs.** The operator queues a known-answer job with `POST /v1/challenges`. Workers get the same envelope, the same receipt and the same audit-trail events as normal work. A wrong (but well-formed) answer is accepted on the surface, so a cheater can't tell they were caught, and recorded as `failed-challenge` with points held.
- **Aimed hidden tests** (threat #1 and #8, since 0.5.21). A normal hidden test goes to whichever rig asks first, so you can't make sure the brand-new rig, or the one that was just caught, is the one that gets tested. `POST /v1/challenges` now takes one optional target:
  - `"target_worker": "rig-c"`: only that rig can take it, until the job's own deadline, even if it is offline for a while. Unknown rig `404`, revoked rig `409`. If you revoke it later, the test waits and nobody else gets it.
  - `"target_tier": "new"` (or `suspect`, `established`): a random **active** rig in that `audit-tier-v1` tier, worked out at the moment it is handed out. If the pick doesn't show up within `AUDIT_ASSIGN_SECONDS`, another rig in the same tier gets it. If no rig is in that tier yet, it waits.
  - Setting both is refused (`422`); setting neither keeps the old first-come behaviour.
  The rig gets the same envelope as any job and can't see the target. Who was picked is recorded privately (`test_assigned`, `test_unassigned`, `test_assignment_expired`), never in the public job view or the logs, and kept separate from audit follow-through counts. `/v1/audits` shows each test's `target`; `/v1/operator/health` adds `aimed_tests` (`open`, `waiting_for_rig`) and a warning when one has no rig. Ledger copies drop the target (tiers are operator-only); copies made before 0.5.21 still check cleanly. Two new consistency checks: an aimed test taken by a rig it wasn't assigned to, or by a rig other than the one it names. It changes **who gets checked**, never points. 15 tests.
- **Operator endpoints** (`/v1/challenges`, `/v1/audits`, `/v1/workers/{id}/trust`, `/v1/operator/review`, `/v1/operator/health`) require the `X-Operator-Token` header to match `OPERATOR_TOKEN`. If the token isn't set, they are disabled.
- **Public ledger** (`GET /v1/ledger`) adds `points_by_status`: confirmed, pending, unverified, held. Challenge outcomes are folded into confirmed/held so the public view doesn't reveal which jobs were challenges. The public job view hides audit and challenge events.
- New consistency checks: a pending audit with no open audit job, a job leased to an excluded worker, a worker auditing its own result, a worker breaking a tie on its own dispute, an outvoted/unresolved result with no tie-breaker behind it, and an audit or tie-breaker taken by a worker it wasn't assigned to.
- 19 tests for Phase 4 plus 9 for random assignment (82 in total), including an in-place migration of a Phase 2 database and a tampered-database check.
- **Written threat model:** [`docs/VERIFICATION-THREAT-MODEL.md`](../docs/VERIFICATION-THREAT-MODEL.md) lists each attack, today's defense, what is still open, and the order of fixes.

## Implemented (Phase 5: simulated reward reports)

Off-chain points only. Nothing is minted, sent or promised.

- **Versioned scoring rule** `sim-points-v1` (`app/rewards.py`), integer math only. Each accepted result sits in one public bucket: `confirmed` and `unverified` are payable; `pending` (a hidden re-run is still queued) and `held` (disputed, outvoted, unresolved, or a failed hidden test) are not. Unverified work counts because spot-checking only works if unsampled honest work is paid. v1 has no extra penalty for a caught cheater (open decision).
- **Per-worker report** for a period (`since <= accepted_at < until`): accepted results, points per bucket, payable points, and activity (leases, rejected answers, lease timeouts, bad submissions).
- **Auditable and reproducible.** The report carries `report_sha256` over its canonical JSON. The same ledger and the same period always give the same hash. `python -m app.rewards verify report.json --db <db>` re-runs it and lists any entry that differs, and catches an edited report file.
- **Self-checks** in every report: the ledger's consistency problems, and whether per-worker sums match the totals.
- **Never leaks hidden checks.** Reports list job ids, buckets, verifier method and confidence only: no job kinds, results, payloads, expected answers or secrets.
- **Operator-only** (`GET /v1/rewards/report`, needs `X-Operator-Token`). A live per-job view would let a worker see a "held" right after submitting and guess it was a hidden test. Publish a report only after its period has closed.
- Statuses are read when the report is built, so a pending re-run that settles later changes the report for the same period; `verify` says so.
- 10 tests for Phase 5 (61 in total). Also run end to end with a real coordinator process, two demo workers, the HTTP endpoint and the CLI `report`/`verify`.

Build and check a report:

    python -m app.rewards report --db ./data/prototype.sqlite3 --since 0 --until 1800000000 --out report.json
    python -m app.rewards verify report.json --db ./data/prototype.sqlite3

**Fraud risks that remain (be honest about these):**
- **Collusion.** Two workers run by the same person can agree on a wrong answer. Agreement is evidence, not proof. Workers now need an operator code and a per-worker key, and same-owner workers never audit each other, but a person who gets codes under several names is still a ring. In a tie-breaker, two colluders can even outvote an honest auditor.
- **Rings with many enrolled rigs.** Audits now go to a random eligible rig (fixed in 0.5.8), but a ring that keeps more rigs active still gets more of the draws.
- **Repeated payloads.** An audit has the same payload as the original. Colluding workers who compare notes could spot it.
- **Trusted coordinator.** The coordinator picks auditors, stores expected answers, and could lie about all of it. Signed reward reports (0.5.11) stop anyone from editing a report after it's issued, but the coordinator can still sign wrong numbers.
- **Real GPU nondeterminism is unknown.** The whitespace-only tolerance is a guess until two real rigs produce output for the same job. It may need loosening (or a different check) after Phase 3.
- **Two rigs aren't enough for a tie-breaker.** With only two workers online, a dispute stays held until a person looks at it.

**Not done yet:** `llm:generate` has **never run** on project hardware: no llama.cpp build, no downloaded model and no RX 470 test yet (Phase 3). Inference results are only checked for shape (`schema-only`). Phase 4 re-run audits are needed before they mean anything.

To try inference on a GPU box later:

    python -m app.model_registry download qwen2.5-0.5b-instruct-q4_k_m ./models --yes-download
    python -m app.model_registry pin qwen2.5-0.5b-instruct-q4_k_m ./models/Qwen2.5-0.5B-Instruct-Q4_K_M.gguf
    LLAMA_CLI=/path/to/llama-cli MODEL_DIR=./models python demo_worker.py

**Still missing (honest limits):** anyone who can reach the API can create normal jobs, and owner labels depend on the operator handing out codes carefully. The coordinator is trusted. (The old shared-secret forgery risk is fixed in 0.5.9: see asymmetric manifest signatures below.) An inference adapter exists (Phase 2) but is untested on real hardware, and there is no RX 470 validation yet (Phase 3).

## Implemented: per-worker keys and authenticated registration

Closes threat-model item #2 *as far as the operator hands out codes carefully*. Tested in software only.

- **One-time enrollment codes.** The operator makes a code tied to an `owner` label (the person or group running the rig) with `POST /v1/enrollments` (needs `X-Operator-Token`) or `python -m app.workers enroll --owner <label>`. A code works once and expires (default 24 h, at most 7 days).
- **Per-worker keys.** A rig sends the code once to `POST /v1/workers/register` and gets its own random key back (`bnhw_…`, 256 bits). The key is shown once; the coordinator stores only its sha256, and never stores codes in plain text either.
- **Every claim and submit needs the key** (`Authorization: Bearer <key>`). The worker id comes from the key. Naming a different worker id gets `403`, a missing or unknown key gets `401`. One rig can't claim or submit as another.
- **No take-overs.** A code can't be used to grab a worker id that already has a key (`409`, and the code is not used up). An existing worker changes its capabilities by registering again with its own key, and gets no new key.
- **Revocation.** `POST /v1/workers/{id}/revoke` (operator) or `python -m app.workers revoke <id>` blocks the key at once and puts any job the worker is holding back in the queue. Its past results stay for the operator to review. A revoked worker id can't re-enroll.
- **One owner never checks itself.** Hidden re-runs and tie-breakers are closed to every worker with the same owner as the worker(s) they exclude, not just the same worker id. A new consistency check flags any job given to a same-owner worker.
- `python -m app.workers list` shows each worker's owner and whether it is active, revoked, or keyless. The trust report shows `owner` and `revoked`. Keys never appear in any API response, listing or log.
- Older databases are migrated in place. Workers registered before this change have no key and must enroll with a code (same worker id is fine).
- The demo worker saves its key to `./data/worker-<id>.key` (owner-only file permissions, git-ignored) on the first run and reuses it after.
- 12 new tests (73 in total). Also run end to end with a real coordinator process: one code from the API, one from the CLI, two demo workers, a reused code refused, a saved key reused, and a revoked worker refused.

**What this does not fix:** owner labels are only as honest as the operator's hand-out. One person who talks the operator into several codes under different names is still a Sybil ring. There is no proof of personhood. (Until 0.5.19 there was also no rate limit on the API and anyone could queue normal jobs; both are fixed, see "who may queue jobs" below.)

**Your two RX 470s:** if both cards get the same owner label, they will never audit each other, so with only those two rigs online no audit ever runs. For a two-card test, enroll them under two labels (for example `owner-gpu0` and `owner-gpu1`) and remember that agreement between two cards you own proves less than agreement between strangers.

## Implemented: asymmetric manifest signatures (threat model #9)

Tested in software only (0.5.9).

- **Only the coordinator can sign jobs.** It holds an Ed25519 private key in `./data/coordinator-signing.key` (or `COORDINATOR_SIGNING_KEY_FILE`), created on first start with owner-only permissions. The coordinator refuses to start if other users on the machine can read the file. Signatures look like `ed25519:<128 hex>`.
- **Workers hold no secret.** They check signatures with the coordinator's public key from `GET /v1/coordinator/key` (public key + a short fingerprint like `0c74-ea31-24dd-92f4`). Before, every worker had the same HMAC secret, so any worker could sign fake jobs for the others.
- **Pinning.** The demo worker saves the public key on first contact (`./data/coordinator.pub`, owner-only) and prints its fingerprint so the rig owner can compare it with the operator's. If the coordinator ever serves a different key, the worker stops instead of trusting it. For a stronger first run, the operator gives the key out of band: `--coordinator-key` or `BNH_COORDINATOR_KEY`.
- `python -m app.signing` prints the public key and fingerprint for the operator to share.
- `PROTOTYPE_SECRET` is no longer used; the coordinator logs a warning if it is still set.
- 7 new tests (89 in total), including a worker that tries to sign a job with its own key, a genuine signature copied onto another job, legacy HMAC signatures, and an impostor coordinator. Also run end to end: coordinator + demo worker (first-contact pin, job accepted), then a second coordinator with a new key on the same address (worker refused to run).

**Limits:** jobs created before 0.5.9 carry the old HMAC signature and are refused by new workers, so start with a fresh database. Replacing the key file by hand breaks every worker's pin; use the rotation tool below instead (0.5.16). First-contact pinning trusts whatever answers the first time, so on a real network give workers the key directly. The same key also signs closed-period reward reports since 0.5.11 (see below); it still does not sign the raw ledger.

## Implemented: planned key rotation (threat model #9 follow-up)

Tested in software only (0.5.16).

- **`python -m app.key_rotation rotate`** (stop the coordinator first) makes a new Ed25519 key and writes a **handover**: old public key → new public key, a sequence number and a time, **signed by both keys** (`purpose: bnh-coordinator-key-rotation/1`, so it can never pass as a job or report signature). The handover is added to the public file `coordinator-signing.key.history.json` next to the key, then the new key replaces the old one. The old private key is not kept. `python -m app.key_rotation show` lists the handovers.
- **Workers follow it on their own.** `GET /v1/coordinator/key` now also returns `rotations`. A demo worker whose pinned key differs from the served one checks the chain of handovers from its pin to the served key (every step signed by both keys, numbered 1, 2, 3..., each starting where the last ended, no key reused, forward only) and only then updates its pin. Anything else and it stops, as before.
- **Queued jobs keep working.** A job signed before the rotation is re-signed with the new key when a worker claims it, but only if its old signature checks out against a key this coordinator handed over from. A job signed by anyone else (for example a row edited in the database) is sent unchanged, so the worker refuses it. Each re-sign is logged (`job_resigned`, old → new fingerprint).
- **Crash-safe.** The new key waits in `coordinator-signing.key.next` until the handover is saved. If the machine dies before the handover is saved, nothing changes and the next `rotate` starts fresh. If it dies after, the coordinator refuses to start ("rotation was interrupted") and re-running `rotate` finishes the same handover instead of making a second one. The coordinator also refuses to start if the key file doesn't match the end of its history.
- Operator health shows `signing_key_rotations`.
- 10 new tests (168 in total). I switched off ten rules one at a time (old-key signature, new-key signature, numbering, linking, no reuse, the start-up history check, finishing an interrupted rotation, the worker's chain check, re-signing on claim, refusing to re-sign a foreign signature) and a test failed every time. Also run end to end: coordinator + demo worker pinned key A, a job queued, coordinator stopped, `rotate`, restarted on key B; the worker followed the handover, the queued job was re-signed and accepted.

**Limits:** a handover only helps for a **planned** rotation. Whoever holds the old private key can sign a handover (since 0.5.24 the optional offline handover key below closes this for workers that pinned it), so if that key may have been stolen, give every worker the new key directly (`--coordinator-key`) and delete its old pin instead. Workers started with `--coordinator-key` don't follow handovers; give them the new key. Old signed reward reports still check out against the old public key, which stays listed in the history file.

## Implemented: offline handover key (threat model #9 follow-up, `handover-key-v1`)

Tested in software only (0.5.24).

- **Why.** A handover signed by the old and new key works for planned rotations, but anyone who steals the coordinator's key file can sign one too, to a key of their own, and every worker would follow it. Now you can add a second key that **only signs handovers** and lives offline (a USB stick in a drawer), never on the coordinator machine.
- **`python -m app.key_rotation make-handover-key --out /media/usb/bnh-handover.key`** makes it (readable by you only). It refuses to overwrite a file and refuses the coordinator key's own folder (a key sitting next to the signing key is stolen together with it). It prints a line to add to `prototype/.env`: `COORDINATOR_HANDOVER_PUBLIC_KEY=<hex>` (public half only).
- **Workers pin it.** With that setting, `GET /v1/coordinator/key` adds `handover_key`: the handover public key, vouched for by a signature from the current coordinator key (`purpose: bnh-handover-key-announcement/1`). A demo worker that already trusts the served coordinator key checks that signature and pins the handover key in `coordinator-handover.pub` next to its other pin, on first contact or on a later run. A tampered announcement is ignored with a warning; a different announced key never replaces a pinned one (a person has to). The operator can also hand it out directly: `--handover-key` / `BNH_HANDOVER_KEY`.
- **From then on, every handover the worker follows must also be signed by that key.** `follow_chain` checks a third signature (`signatures.handover`) on each step after the worker's pinned key. Older handovers from before the key existed aren't checked. The coordinator key file alone can no longer move the worker.
- **Rotating needs the stick.** `python -m app.key_rotation rotate --handover-key-file /media/usb/bnh-handover.key`. With `COORDINATOR_HANDOVER_PUBLIC_KEY` set, `rotate` refuses to run without the file, or with a different one, because workers would refuse the handover and stop. `show` marks handovers that carry the third signature.
- The coordinator refuses to start if the handover key is one of its own signing keys (current or retired). Operator health shows `handover_key` (`rule` and `fingerprint`), or `null` when off.
- 13 new tests (294 in total). I switched off 21 rules one at a time (requiring the third signature, refusing a handover key that's on the chain, the announcement's purpose, coordinator key, signature and self-check, `rotate` needing the stick, needing the right stick, refusing a coordinator key as the stick, adding the signature, the key-making folder and overwrite checks, loading never creating a key, the coordinator's start-up check, serving the announcement, reading the setting, the worker passing its pin to the chain check, pinning on a later run, the given-vs-pinned conflict, never replacing a pin, checking the announcement) and a test failed every time. Also run end to end with the real server and `demo_worker.py`: the worker pinned the coordinator key, then the announced handover key after a restart; `rotate` without the stick was refused, with it the worker followed and a job was accepted; a "thief" server with a copy of the current key file and a handover to its own key (signed by both keys) was refused, and the pin stayed put. No keys or tokens in the server log.

**Limits:** off until you make the key and set it. A thief with the coordinator key file can still sign **jobs** until you rotate, so rotate (with the stick) as soon as you suspect a theft. Workers that never pinned a handover key (or were started with `--coordinator-key`) still follow any handover signed by the old and new key; give them the new key directly. If the stick is lost, workers that pinned it can't follow any further rotation: re-pin them by hand. The first pin is still trust on first use, unless you hand out the key directly.

## Implemented: signed reward reports (threat model #10, first step)

Tested in software only (0.5.11). Simulated points only.

- **The coordinator signs a closed period's report.** `python -m app.rewards report --sign --out r.json` (or `GET /v1/rewards/report?until=...&sign=true`) adds a `signature` block: algorithm, the coordinator's public key, its fingerprint and an Ed25519 signature. It is the same key that signs job manifests; the signed message carries `purpose: bnh-reward-report-signature/1`, so a report signature can never pass as a job signature.
- **What is signed:** the report type, `report_sha256` (which covers the whole body), the scoring version and the period. Editing any number breaks the hash; recomputing the hash breaks the signature.
- **Anyone can check it without the database:** `python -m app.rewards verify r.json --no-db --public-key <hex>` (or `BNH_COORDINATOR_KEY`). Without a trusted key it still checks, but tells you to compare the fingerprint, because anyone can sign a file with their own key.
- **Refuses to sign** a period that hasn't ended (`until` in the future; its numbers can still change and a live per-job view can reveal hidden tests, threat model #14), a report that fails its own checks, or a report whose hash doesn't match. The CLI never creates a new key: `--sign` with a missing key file is an error.
- `verify --db` with `--public-key` now also demands a valid signature. An unsigned report still verifies against the ledger as before.
- 12 new tests (109 in total), including an edited-and-rehashed report, a report signed by an impostor key, a report signature tried as a job signature, and an open period. Each new check was switched off once to confirm a test fails. Also run end to end: a coordinator process, a demo worker that finished two jobs, a signed report checked offline and against the ledger, a tampered copy refused, and the API refusing to sign an open period.

**What this does not fix:** a signature proves *who issued* the report, not that the numbers are right. The coordinator can still sign a report built from a database it edited; only an independent re-run against a copy of the ledger (`verify --db`) checks the numbers, and nobody outside the operator has that copy yet. There is no key rotation and no public place where reports are posted yet.

## Implemented: shareable ledger copies (threat model #10, second step)

Tested in software only (0.5.13). Simulated points only.

A signed report proves who issued the numbers. To check the numbers themselves, someone needs the ledger they came from. `app/ledger_copy.py` makes a redacted copy of the ledger up to the end of a closed period that anyone can re-run the report against:

    python -m app.rewards report --db ./data/prototype.sqlite3 --until 1800000000 --sign --out report.json
    python -m app.ledger_copy export --db ./data/prototype.sqlite3 --until 1800000000 --out ledger-copy.sqlite3
    # anyone, with the two files and the coordinator's public key:
    python -m app.rewards verify report.json --db ledger-copy.sqlite3 --public-key <hex>
    python -m app.ledger_copy info ledger-copy.sqlite3

- **What it keeps:** the structure the checks need. Which job re-ran which, who did each job, which workers share an owner, timestamps, points and settlement. Both the report and every `check_consistency` invariant (no self-audit, no same-owner audit, every outvoted result has a tie-breaker, ...) give the same answer on the copy as on the coordinator.
- **What it removes:** worker key hashes, enrollment codes, payloads and prompts, job signatures, answers and answer hashes, expected answers of hidden tests, event details (they can name owners), worker capabilities and last-seen times. Owner labels become `owner-1`, `owner-2`, ... (same owner, same label). Payload hashes are replaced by an HMAC with a random key that is thrown away, so a re-run still visibly matches its original inside the copy but can't be matched against live jobs.
- **What it leaves out:** jobs created at or after `until` (except re-checks of earlier jobs, which settle them), events at or after `until`, and workers that joined later.
- **Refuses** an open period, a period with any job (or re-check of one) still queued or running, a ledger that fails its own consistency check, an existing output file, and writing over the ledger itself. Deleted rows are wiped (`secure_delete` plus `VACUUM`), and the file is built under a temporary name, so a failed export never leaves a half-redacted copy.
- **Checking never changes the file.** `rewards report/verify` open a copy read-only, so the sha256 printed at export stays valid. A report that reaches past the copy's `until` is refused, since it would be built from missing data.
- 10 new tests (137 in total), including a byte search of the copy for every prompt, answer, expected answer, key, key hash, code hash, signature, real owner label and original payload/answer hash used. Six checks were switched off one at a time (wiping, hash masking, the open-job check, the past-the-end guard, read-only opening, event-detail removal) and a test failed every time. Also run end to end: a coordinator process and a demo worker, an export refused while a job was still queued, then a signed report re-checked against the copy with "OK", no secrets found in the copy, and its sha256 unchanged after checking.

**Trade-off and limits:** for the closed period, the copy shows which jobs were hidden re-runs and hidden tests and how they came out (not their prompts or answers). Outsiders need that to check that no worker audited itself, but it is more than the report shows (threat model #14), so share copies with a delay. A copy proves the report matches *this* copy; an operator could still edit the ledger before exporting. Copies from many periods that later disagree, or independent observers, are what catch that. There is still no fixed public place to post reports and copies (owner decision).

## Implemented: versioned job and receipt schemas (Track E)

Tested in software only (0.5.10). Code: `app/schemas.py`.

- **Every job says which layout it uses.** Manifests carry `schema_version` (`bnh-job/1`), covered by the Ed25519 signature. A worker refuses any version it doesn't know instead of guessing what the fields mean.
- **Model jobs name their exact model file.** `llm:generate` jobs carry `model_sha256`, copied from the pinned registry when the job is created and covered by the signature. Audits and tie-breakers copy it from the original job, so re-pinning the registry later never changes what an existing job means. Jobs with no model carry `null`.
- **The worker checks its own pin first.** Before running anything, the demo worker compares the job's `model_sha256` with its own registry's pin (and the executor re-hashes the file against that pin before every run). A different or missing pin means the job is refused.
- **Receipts echo what the worker used.** A result now carries `receipt_version` (`bnh-receipt/1`), `job_schema_version`, `payload_hash` (the worker hashes the input it ran) and `model_sha256`. The coordinator refuses (`422`) a receipt with an unknown version or any value that doesn't match the job. A refused receipt leaves the lease alone, is logged as a public `receipt_mismatch` event and counts as a bad submission in reward reports.
- `GET /health` lists the job and receipt versions this coordinator speaks.
- **Older databases** are migrated in place. Jobs stored before versioning are marked `bnh-job/0`; new workers refuse them with a clear message, so start new work from new jobs.
- Two new consistency checks: a re-run whose version, input or model hash differs from its original, and a model job created without a pinned model hash.
- 8 new tests (97 in total). I disabled each new check in turn (receipt match, worker pin check, audits copying the original hash) and the matching tests failed. Also run end to end: coordinator process + demo worker (versioned job accepted) and an old-style receipt refused with a clear message.

**What this does not prove:** a receipt is the worker's own claim. A dishonest worker can copy the job's hashes into its receipt without running anything. These checks catch honest mistakes (stale model file, outdated worker, an answer sent to the wrong job); hidden re-runs and challenges are still what catch cheating (threat model #8).

## Implemented: structured logs and health reporting (Track D)

Tested in software only (0.5.12).

- **One JSON object per log line** (`app/observability.py`): `ts`, `level`, `logger`, `msg`, then named fields. `LOG_FORMAT=text` gives a readable `key=value` version for a terminal; `LOG_LEVEL` picks debug/info/warning/error (default info). Start uvicorn with `--no-access-log` so requests aren't logged twice.
- **One line per request** (`bnh.access`): method, path, status, duration in ms and a request id. The id comes back in the `X-Request-ID` header; a worker can send its own (letters, digits, `.`, `_`, `-`, up to 64 characters) so its error can be matched to the coordinator's line.
- **Lifecycle lines** (`bnh.coordinator`): job created, job leased, result submitted (with the outcome), worker enrolled, worker revoked. **Warnings** for refused worker keys, refused operator tokens and refused receipts, with the reason.
- **Safe to paste into a bug report:** no keys, tokens, enrollment codes, prompts, answers or expected answers, no query strings and no headers. Fields with those names are replaced with `[redacted]` even if code passes them by mistake. A challenge is logged exactly like a normal job. Logs are still operator data: the request lines show *when* you called an operator endpoint, so don't hand raw logs to workers.
- **`GET /health` (public):** `200` with `status: ok`, uptime and job counts when the database can be read; `503` with `status: down` when it can't (the error itself goes to the log, not the response). It says nothing about workers or audits.
- **`GET /v1/operator/health` (operator only):** registered, active and revoked workers; queued and leased jobs, the oldest queued job's age, leases past due and jobs past their deadline; open audits and those **waiting for an eligible auditor**; event counts for the last hour; the ledger's consistency problems; and plain-language `warnings`. `status` is `attention` whenever there is a warning. It only reads: past-due leases are requeued by the next worker request, as before.
- 18 new tests (127 in total), including a full workflow whose logs are searched for every key, token, code, prompt and answer used. Each safety check was broken on purpose once (no redaction, query strings logged, a key logged under another name, the database check skipped) and a test failed every time. Also run end to end: a coordinator with JSON logs, a demo worker finishing a job, a refused fake key matched by its request id, and both health endpoints.

**Limits:** logs go to stderr only (no rotation or shipping yet; use your service manager), and no metrics endpoint for graphs. The waiting-auditor warning is exactly what your two-card test will show if both RX 470s share one owner label.

## Implemented: fixed points per task type (0.5.22)

Tested in software only. Simulated off-chain points only.

Since 0.5.19 only the operator or a submitter token can queue jobs, but whoever holds the submitter token still chose each job's points (0-1000), so it could queue a 1000-point job for a friend's rig to pick up (threat model #16). New in `app/points.py` (rule `points-table-v1`):

- **`POINTS_TABLE`** (empty by default = no change): one fixed number per task type, e.g. `POINTS_TABLE=demo:sha256=1,llm:generate=10`. It must list every task type the coordinator runs, exactly once, each a whole number 0-1000; anything else stops the coordinator at start-up with a plain error.
- **With a table, every new job and hidden test gets the table's number.** `reward_points` can be left out (recommended) or set to that same number. Any other number is refused with `422` and nothing is queued, for the submitter **and** the operator, so a hidden test always pays exactly what normal work of its type pays and can't be spotted by its points.
- **Without a table nothing changes:** the request's number, or 1 if left out.
- **Changing the table** means editing `.env` and restarting. Jobs already queued keep the points they were signed with; only new jobs use the new numbers.
- **`/v1/operator/health`** adds `points` (`rule`: `points-table-v1` or `creator-chooses`, and the `table`) and warns while `SUBMITTER_TOKEN` is set without a `POINTS_TABLE`.
- 25 new tests (270 in total). Each rule was switched off once on purpose (12 changes: using the table at all, refusing other numbers, hidden tests using the table, the health warning, the start-up check that the table covers every task type, refusing a missing task type, a duplicate, a number over 1000, an unknown task type, reading the setting, showing it on the health page, normal jobs using the table) and a test failed every time. Also run end to end with a real coordinator and `POINTS_TABLE=demo:sha256=2,llm:generate=10`: a submitter asking for 1000 got `422`, leaving it out got 2, the operator's hidden test asking for 50 got `422`, a demo worker finished the job and the public ledger showed 2 points; after a restart with `demo:sha256=5` a new job paid 5 (ledger 7), and the server log held no tokens or payloads. A table missing a task type stopped start-up.

**Limits:** the table fixes *who* picks the number, not *what it should be*: the numbers themselves are your call (a real measure like tokens generated or seconds of GPU time would be fairer later, once the RX 470s are measured). A submitter can still queue many jobs; the per-address request limit slows that down but the submitter token should still only go to programs you run.

## Implemented: real client addresses behind a proxy (0.5.20)

Tested in software only.

Request limits and lock-outs count per address. Behind a reverse proxy (nginx, Caddy, a tunnel, a load balancer) every request arrives from the proxy, so all rigs shared one bucket and one rig with a bad key could lock everyone out. New in `app/client_address.py`:

- **`TRUSTED_PROXIES`** (comma or space separated addresses or ranges, e.g. `127.0.0.1` or `10.0.0.0/8`; **empty by default**). Only a request whose connection comes from a listed proxy has its `X-Forwarded-For` header read. From anyone else the header is ignored, so a rig can't pick its own address to dodge a lock-out.
- **Read from the right.** Each proxy appends the address it saw, so the rightmost entries were written by your proxies and anything further left may be made up by the client. The first entry from the right that isn't a listed proxy is the client. Ports (`1.2.3.4:5555`, `[2001:db8::7]:443`) and IPv4 seen over IPv6 (`::ffff:10.0.0.5`) are understood; at most 20 hops are read.
- **No header, or an entry that isn't an address:** the request keeps the last address we can vouch for (usually the proxy) and shares its limits. `/v1/operator/health` → `rate_limits.proxies` counts these and adds a plain warning, because it usually means the proxy isn't set to send the header.
- **"Trust everyone" is refused** (`0.0.0.0/0`, `::/0`): the coordinator won't start, because any client could then choose its address. Bad entries are refused the same way.
- **The real client is what gets limited, locked out, tagged in logs (`client_tag`) and named on the operator health page**, so you can block that address rather than your own proxy. Addresses still never appear in log lines.
- **Start uvicorn with `--no-proxy-headers`** (shown in the quick start). uvicorn has its own proxy-header handling, on by default for `127.0.0.1`; turning it off makes `TRUSTED_PROXIES` the one setting that decides.
- 20 new tests (230 in total). Each rule was switched off once on purpose (12 changes: trusting any peer, reading left to right, allowing trust-everyone, counting missing headers, stopping at garbage, IPv4-over-IPv6, ports, the hop cap, working the address out once per request, the health warning, reading the setting, using the resolver at all) and a test failed every time. Also run end to end with a real coordinator and `TRUSTED_PROXIES=127.0.0.1`: three wrong keys from "rig A" (`X-Forwarded-For: 203.0.113.50`) locked A out (`429`), "rig B" behind the same proxy still got `200`, A sending a made-up `203.0.113.51` in front of its real address was still `429`, the operator health page named `203.0.113.50` and warned about one request with no header, and the log held no addresses.

**Limits:** only `X-Forwarded-For` is read, not the newer `Forwarded` header (nginx, Caddy, Cloudflare and most load balancers send `X-Forwarded-For`). If a listed proxy doesn't overwrite or append the header correctly, a client could choose its address, so list only proxies you run and have configured. Rigs behind one home router still share one address (that's their real address). Same in-memory, single-process limits as 0.5.19.

## Implemented: who may queue jobs, request limits and lock-outs (0.5.19)

Tested in software only.

- **Queuing a job needs a token.** `POST /v1/jobs` used to be open to anyone who could reach the coordinator. That let any enrolled rig queue a job worth up to 1000 simulated points on an empty queue, claim it itself and get paid. Now it needs either `X-Operator-Token` or a new `X-Submitter-Token` (`SUBMITTER_TOKEN` in `.env`, at least 24 characters, different from the operator token). The submitter token can **only** queue normal jobs: it can't see hidden tests, answers, audits or reports, enroll rigs or make challenges, so a program that feeds work in never needs the operator token. With neither token set, job creation is off. A worker key never works here.
- **Requests per minute per address** (`app/rate_limit.py`, `RATE_LIMIT_PER_MINUTE`, default 240, `0` = off). Each client address gets a bucket of that many requests that refills at the same rate. An empty bucket gets `429 Too Many Requests` with `Retry-After`. The demo worker waits as told (at most 60 s, a few times) before claiming or submitting again; submits are idempotent, so a retry is safe.
- **Lock-out after wrong credentials** (`AUTH_FAILURE_LIMIT`, default 10, `0` = off, within `AUTH_FAILURE_WINDOW_SECONDS`, default 600). A wrong, revoked or someone else's worker key, a wrong operator or submitter token, or a bad enrollment code counts as one failure. That many failures in the window locks the address out (`429` on every endpoint) until the oldest falls out of the window. A **missing** key, token or code doesn't count: that's a setup mistake, not a guess. Worker keys and codes are 256-bit random, so this mostly protects the operator and submitter tokens, which you type in yourself.
- **`/health` is never limited**, so a monitor can't be locked out by someone else's traffic.
- **Logs never contain the address.** Limit and lock-out lines carry a short `client_tag` (a keyed hash with a random key made at start-up) so one client's lines can be grouped. Run uvicorn with `--no-access-log` as shown below, because uvicorn's own access log prints addresses. `/v1/operator/health` (operator only) adds `rate_limits`: totals since start, and each locked-out address with its tag and how long until it's let back in, plus a warning, so you can block it in a firewall if needed.
- 20 new tests (210 in total). Each rule was switched off once on purpose (13 changes: the token check, counting each kind of wrong credential, not counting missing ones, the `/health` exemption, the lock-out, the bucket, the refill cap, forgetting old failures, the tag in logs, the health warning, a submitter header when no submitter token is set) and a test failed every time. Also run end to end with a real coordinator: no token `403`, submitter token `200` (and `403` on an operator endpoint), a demo worker finished the job, the worker's own 1000-point job refused, three wrong credentials locked `127.0.0.1` out (`429`, `Retry-After: 600`) while `/health` still answered, and the operator health page, reached from a second address, named it. A second run at 5 requests a minute gave `429` on the 6th request with `Retry-After: 12`.

**Limits:** counts and lock-outs live in memory, so a restart clears them, and one coordinator process is assumed. "Client" means the connecting address: behind a home router, many machines share one address and its limits, and one rig with a bad key can lock the others on that address out for the window. (Behind your own reverse proxy, set `TRUSTED_PROXIES`; see 0.5.20 above.) Someone with many addresses gets many buckets; this slows a flood, it doesn't stop a distributed one (that needs the host's DDoS protection). A submitter could still queue jobs with up to 1000 points for a friend's rig to claim (fixed with `POINTS_TABLE`, see 0.5.22 above), so give the submitter token only to programs you run.

## Safety and correctness boundaries

The Ed25519 signature shows a manifest came from the holder of the coordinator's private key, but only to a worker that checks it against the right public key. The demo worker verifies it before executing. Result hashes are integrity identifiers, not proofs of execution. The coordinator is trusted in this MVP. Rewards are simulated ledger points only; no token minting, private-key handling, wallet signing, or on-chain transfer is implemented.

The reference repositories:
- https://github.com/MooreThreads/ethminer-musa
- https://github.com/simplechain-org/go-ethereum-pow

are research references only. Their PoW/consensus implementations are not integrated into this AI task execution MVP. The first is an Ethash GPU miner with vendor/backend-specific code; the second is an archived Go-Ethereum PoW fork. Neither provides verified AI inference for RX 470 by itself. Review exact licenses and notices before any code reuse.

## Quick start (Ubuntu)

    cd prototype
    python3 -m venv .venv
    . .venv/bin/activate
    pip install -r requirements.txt
    cp .env.example .env
    python -m app.signing   # creates the signing key; share the printed public key
    uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log --no-proxy-headers

In another terminal, make a one-time code for the rig and start the worker with it:

    cd prototype
    . .venv/bin/activate
    CODE=$(python -m app.workers enroll --owner owner-gpu0)
    python demo_worker.py --url http://127.0.0.1:8000 --worker-id rig-gpu0 --enrollment-code "$CODE" \
        --coordinator-key <public key printed above>

Later runs reuse the saved worker key and the pinned coordinator key:

    python demo_worker.py --url http://127.0.0.1:8000 --worker-id rig-gpu0

Create a demo job using the API docs at http://127.0.0.1:8000/docs:
- POST /v1/jobs with {"payload":"hello from Boats N Hoes","task_type":"demo:sha256"}, and your `OPERATOR_TOKEN` (or `SUBMITTER_TOKEN`) in the `x-operator-token` (or `x-submitter-token`) box. Without one you get `403`.
- The demo worker claims and processes queued jobs.
- GET /v1/ledger shows simulated points.

## Tests

    cd prototype
    . .venv/bin/activate
    pytest -q

A GitHub Actions workflow runs these tests on pushes and pull requests affecting prototype files.

## API outline

- `GET /v1/coordinator/key` — public: the coordinator's Ed25519 public key, fingerprint, signed key handovers (`rotations`) and, if set, the announced offline handover key (`handover_key`).
- `python -m app.key_rotation rotate|show|make-handover-key` — CLI only: planned key rotation with a handover signed by both keys, and optionally by the offline handover key (see above).
- `POST /v1/enrollments` — operator only: make a one-time enrollment code for an owner.
- `POST /v1/workers/register` — new worker: send the enrollment code, get a key once. Existing worker: send its key to update capabilities.
- `POST /v1/workers/{worker_id}/revoke` — operator only: block a worker's key and requeue its job.
- `POST /v1/jobs` — queue a job (needs `X-Submitter-Token` or `X-Operator-Token`); with `POINTS_TABLE` set, its points come from the table.
- `POST /v1/jobs/next` — worker claims the next queued job (needs its key).
- `POST /v1/jobs/{job_id}/submit` — submit a versioned receipt (`bnh-receipt/1`) with the result (needs its key).
- `GET /v1/ledger` — view simulated accepted-work entries and points by status.
- `GET /v1/jobs/{job_id}` — job status and its public audit trail.
- `POST /v1/challenges` — operator only: queue a hidden known-answer job; optional `target_worker` or `target_tier` aims it at one rig or a trust tier.
- `GET /v1/audits` — operator only: every audit and challenge with its outcome.
- `GET /v1/workers/{worker_id}/trust` — operator only: how a worker's results held up, plus its audit tier (`new`/`established`/`suspect`), the re-check rate that applies to it, `checks_passed` and `strikes`.
- `GET /v1/rewards/report?since=&until=&sign=` — operator only: simulated reward report for a period; `sign=true` adds the coordinator's signature (closed periods only).
- `python -m app.ledger_copy export|info` — CLI only: redacted ledger copy for a closed, settled period (see above).
- `GET /health` — public health check: `200` ok / `503` down, uptime, job counts, and the job/receipt schema versions spoken.
- `GET /v1/operator/health` — operator only: workers, queue, waiting audits, review-queue counts, recent events, consistency problems and warnings.
- `GET /v1/operator/review?needs_person_only=` — operator only: held cases with every answer side by side and whether a person needs to look (read-only). CLI: `python -m app.review`.

## GPU diagnostic

From the prototype directory:

    python -m app.gpu_probe_cli

This checks whether `vulkaninfo --summary` can run. Device enumeration is not proof that AI inference works.

For the full rig inventory (Phase 3 step one), run on the Ubuntu machine with the cards:

    python -m app.hw_inventory --out inventory.json

It reads `/etc/os-release`, runs only `uname -r`, `lspci -nnk` and `vulkaninfo --summary` (an allowlist, no shell, no sudo, nothing installed), lists `/dev/dri`, and writes a versioned JSON report (`bnh-hw-inventory/1`) with plain-language findings:

- how many AMD cards `lspci` sees, with PCI IDs, and which kernel driver each uses (warns on `radeon` or none: Vulkan RADV needs `amdgpu`);
- each Vulkan device with its driver and Mesa version (warns when Vulkan only sees a CPU renderer such as llvmpipe, or sees fewer AMD cards than `lspci`);
- `/dev/dri/renderD*` nodes your user can't open (usually the `render` group).

Hostname, username and UUIDs are replaced with `[host]`, `[user]` and `[uuid]`. It refuses to overwrite an existing file and always says `"proves_inference": false`. Missing tools are reported with the Ubuntu package name; installing them, changing drivers or groups is the owner's decision. Tested only against format samples of command output (`tests/test_hw_inventory.py`), not on the real rig yet. Then test each GPU separately.

### GPU sensor readings (temperature, power, VRAM, clocks)

While a test runs on one card, record how hot, busy and full it gets:

    python -m app.gpu_sensors                                   # one snapshot of every card
    python -m app.gpu_sensors --card card1 --samples 120 --interval 1 --out gpu1-run1-sensors.json

It only **reads** the files the amdgpu driver already publishes under `/sys/class/drm/cardN/device` (`mem_info_vram_*`, `gpu_busy_percent`, `pp_dpm_sclk`/`pp_dpm_mclk`) and its `hwmon` folder (`temp1_*`, `power1_average` or `power1_input`, `power1_cap`, `fan1_input`). No commands, no sudo, never writes to sysfs (no overclocking, fan or power changes), never reads serial numbers or `unique_id`. Connectors (`card0-DP-1`) and render nodes are skipped.

The JSON report (`bnh-gpu-sensors/1`) has each card's PCI slot (to match the inventory), driver, last reading and, over the watch, the lowest/highest/average temperature, power, VRAM use, busy %, core/memory clock and fan speed. Findings warn when a card reaches 85 °C or comes within 5 °C of the driver's own critical limit, when VRAM use reaches 90 %, when a card isn't on `amdgpu`, when a sensor is missing, or when a card stayed at 0 % busy for the whole watch (so the test didn't run there). `--samples` is 1-3600 and `--interval` 0.5-60 s; Ctrl-C keeps the readings taken so far (`stopped_early`). It refuses to overwrite a file, checked before the watch starts, and always says `"proves_inference": false`. Tested only against fake sysfs trees laid out like amdgpu's (`tests/test_gpu_sensors.py`), not on the real rig yet. Throughput and errors still come from the test itself.

## Not implemented

- Real model inference on RX 470.
- Public deployment security: TLS, DDoS protection and abuse prevention beyond the simple per-address limits above. Workers authenticate with per-worker keys; queuing jobs needs a submitter or operator token.
- Ethereum ERC-20 integration or reward distribution.

Treat this as an engineering starter, not production infrastructure.
