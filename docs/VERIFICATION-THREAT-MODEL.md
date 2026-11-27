# Verification threat model: fraud and collusion (GPU worker prototype)

**Status:** draft, written 2026-11-22 against prototype v0.4.1 (Phase 4 complete in software); rows updated through 0.5.43.
**Scope:** the coordinator/worker prototype in `prototype/`. Everything here is tested in
software only. No real GPU has produced output for any of these checks yet.

The short version: the coordinator can't prove a GPU ran a model. It can only make cheating
expensive and visible. Rewards are simulated, off-chain points, so the worst outcome today is
wrong numbers in a test ledger, not lost money.

## What we protect

| Asset | Why it matters |
|-------|----------------|
| Correct results | People will eventually rely on the AI output a worker returns. |
| The points ledger | Points are how contributors are credited. Inflated or stolen points make the whole scoreboard meaningless. |
| Worker reputation (trust report) | Decides who gets more work and whose results are believed. |
| Contributor machines | A worker must never run code a fake coordinator sends it. |

## Who might attack

| Actor | Goal |
|-------|------|
| Lazy worker | Get points without running the model (return a cached, short or made-up answer). |
| Fabricator | Return plausible fake output at scale, many workers at once. |
| Colluding ring / Sybil | One person runs many "independent" workers so they can audit each other. |
| Model swapper | Run a smaller, cheaper or modified model and claim it was the pinned one. |
| Fake coordinator | Trick contributors' rigs into running its own jobs. |
| Malicious or careless operator | Edit the ledger, pick friendly auditors, or leak which jobs are tests. |
| Network attacker | Replay, reorder or alter messages between worker and coordinator. |

## How each check works today

| Check | What it catches | Built |
|-------|-----------------|-------|
| Exact recompute (`demo:sha256`) | Any wrong answer on tasks the coordinator can redo cheaply. | Yes, tested |
| Shape check (`llm:generate`) | Malformed output only. It does **not** show the answer is right. | Yes, tested |
| Hidden re-run audit | A fabricated AI answer, when a different worker re-runs the job and gets a different result. Rate `AUDIT_RATE` (default 25%). | Yes, tested |
| Hidden tie-breaker | Which side of a disputed audit is wrong: a third worker (neither side) re-runs the job and the majority of three wins. | Yes, tested |
| Hidden challenge jobs | A worker that fakes answers on a job where the operator already knows the answer. Looks like normal work, and a wrong answer looks accepted. | Yes, tested |
| Signed manifests (Ed25519 since 0.5.9; HMAC before) | A fake coordinator, and workers forging jobs for each other. Workers refuse jobs whose signature doesn't check out against the pinned coordinator public key. | Yes, tested |
| Leases, idempotent receipts, deadlines | Replays, double payment, late answers from a worker that lost its lease. | Yes, tested |
| Model fingerprint (sha256) before each run | A swapped or corrupted model file **on an honest worker**. | Yes, tested |

### How fast spot-checks catch a cheater

If every fake answer has an independent chance `p` of being re-run by an honest worker, the
chance a cheater survives `n` fake answers is `(1 - p)^n`.

| Audit rate `p` | Caught within 5 fakes | 10 fakes | 20 fakes |
|----------------|----------------------|----------|----------|
| 10% | 41% | 65% | 88% |
| 25% (default) | 76% | 94% | 99.7% |
| 50% | 97% | 99.9% | ~100% |

This assumes the auditor is honest. The rest of this document is about when that's false.

## Attacks, current defense, and what is still open

| # | Attack | Defense today | Remaining risk | Planned fix |
|---|--------|---------------|----------------|-------------|
| 1 | **Lazy/fake answers** on AI jobs | Re-run audits, challenges, held points on mismatch. **Since 0.5.15 (`audit-tier-v1`):** the re-check rate follows the worker's record: `new` rigs (fewer than 5 independently checked results) at 0.5, rigs with a strike (failed hidden test or outvoted) at 1.0, established rigs at the base 0.25; never below the base. **Since 0.5.21:** the operator can aim hidden known-answer tests at one rig (`target_worker`) or a tier (`target_tier`: a random active `new`/`suspect`/`established` rig), so a new rig can't graduate only through re-runs by friends | Survives `(1-p)^n` of the time on established rigs; unaudited results show as "unverified", not "confirmed". A cheater who stays honest until established, then cheats, gets the base rate until the first strike. A ring that audits itself can graduate fake rigs out of `new` (#3), unless the operator also aims tests at new rigs; aimed tests only exist if the operator creates them. **Since 0.5.33** (`test-pool-v1`): `python -m app.challenge_pool` keeps a private stock of prepared tests and issues them a few at a time (aimed at a rig or tier), each used once; but the stock is empty until someone runs the pinned model to get real answers | Weight by recent history instead of lifetime counts; let strikes age out under an owner-approved rule; fill the pool with known answers from the real model (Phase 3) and issue from it regularly |
| 2 | **Sybil: one person, many workers** | One-time operator enrollment codes tied to an `owner` label; per-worker keys (stored as hashes) on every claim and submit; same-owner workers never audit or break ties for each other; revocation | A person who gets codes under several names is still a ring. Labels are only as good as the operator's hand-out | Hand out codes one per verified person; later, reputation built up over time that is costly to fake |
| 3 | **Colluding auditor** confirms a friend's fake answer | Self-audit and same-owner audit blocked; audit hidden among normal work; auditor picked at random among active workers, so a ring of k in n active workers audits a friend only about k/n of the time. **Since 0.5.43** (`vouch-check-v1`, read-only): once a rig is caught (failed hidden test or outvoted), `/v1/operator/vouches` and `python -m app.vouches` list every rig whose answer agreed with it (two caught rigs that agreed come first) and every confirmation that rests only on a caught rig; operator health warns about leads that never passed a hidden test, so the operator can aim one at them. **Since 0.5.44** (`lead-tests-v1`): the report shows which leads already have an aimed test waiting (`open_aimed_tests`, `leads_to_test`), and `python -m app.challenge_pool issue-leads` aims one test from the private pool at each lead that needs one, of a task type it agreed on, without piling tests onto a rig that already has one | Two ring members agree on a fake: marked "confirmed". The vouch check only starts once one ring member is caught, lists honest rigs too, and changes no points: weakly confirmed points stay paid until scoring v2 says otherwise | ~~Random auditor assignment~~ (done, 0.5.8); ~~find suspected rings~~ (leads listed, 0.5.43) ~~and send them hidden tests~~ (`issue-leads`, 0.5.44; run by the operator, not automatic); trust weighting; an owner-approved rule for weakly confirmed points (scoring v2) |
| 4 | **Racing for audits.** Audits used to go to whichever eligible worker asked next, so a ring that polled fast got more of them | **Fixed in software (0.5.8):** each audit and tie-breaker is assigned to one eligible worker chosen with secure randomness from those active in the last `AUDITOR_ACTIVE_SECONDS`; only that worker can claim it; re-assigned at random if it doesn't claim in `AUDIT_ASSIGN_SECONDS`. Consistency check flags an audit taken by anyone else | A ring can still raise its share by keeping more enrolled workers active (that's #2, Sybil). A ring member that refuses an audit it doesn't like just passes it on to another random pick. **Since 0.5.17** those drops are counted per rig (`audit_follow_through`: not picked up, lease ran out, result refused) and the operator health page warns at `AUDIT_DROP_WARN`, but nothing happens automatically and an offline rig looks the same. **Since 0.5.37** (`follow-through-v2`): a pick that was busy with older jobs during its window counts as `not_reached`, not as a drop, because jobs go out oldest first and it never saw the audit. **Since 0.5.23** (`auditor-pick-v1`, opt-in `AUDITOR_PICK=trusted-first`): picks go to the most trusted tier with an eligible rig (`established`, then `new`, then `suspect`), so a caught rig only audits when nobody else can; off by default because it shifts who earns audit points | Switch `trusted-first` on (owner decision); an owner-approved rule for what repeated drops should change (e.g. treat as `new` for audit rate) |
| 5 | **Spotting an audit by its payload.** An audit carries the same prompt as the original | Same envelope, same deadline window, private events hidden | Colluders who compare notes can match prompts | Mix in duplicate normal jobs so a repeated prompt isn't a tell; later, re-run on a slightly different but equivalent input where the task allows |
| 6 | **Two of three in the tie-breaker are colluding** | Tie-breaker excluded from both sides of the dispute and from their owners. **Since 0.5.23** (opt-in `AUDITOR_PICK=trusted-first`): the tie-breaker comes from the most trusted tier with an eligible rig, so a rig with a strike is the deciding vote only when nobody else is eligible. **Since 0.5.34** (`tiebreak-trust-v1`, opt-in `TIEBREAK_DECIDES=established-only`): a majority settles a dispute only when the tie-breaker and the rig it sided with are both `established` (records from before the vote); otherwise all three stay held, nobody gets a strike, and the case goes to the review queue | Majority of three can **outvote an honest auditor** if the original worker and the tie-breaker are in the same ring. `trusted-first` and `established-only` don't help against a ring whose rigs are already `established`, and both are off by default. Held cases wait for a person, and there is no override for their points yet (scoring v2) | Same as #2 and #4; switch `trusted-first` and `established-only` on (owner decisions); ~~never let a single tie-breaker decide on low-trust workers~~ (done, software only, 0.5.34) |
| 7 | **Only two rigs online**, so no third worker exists | Tie-breaker expires, dispute stays held (`tiebreak_not_run`). **Since 0.5.18:** the operator review queue (`/v1/operator/review`, `python -m app.review`) lists it as `needs_review` with both answers side by side, together with `unresolved` disputes and failed hidden tests; operator health warns while any case needs a person | A person still has to decide, and there is no override: the points stay held whatever they conclude | An owner-approved rule (scoring v2) for what a reviewer's decision does to held points, recorded in the audit trail |
| 8 | **Model swapping** (smaller or tampered model) | Fingerprint check runs on the worker, which the cheater controls. Since 0.5.10 each model job carries the pinned `model_sha256` under the signature and the receipt must echo it, which catches honest wrong-model setups but not a liar | A cheating worker can skip the check entirely | Audits catch it only if the swapped model's output differs. Challenges with known outputs from the pinned model help; **since 0.5.21** they can be aimed at the rig you suspect (`target_worker`) |
| 9 | **Forged job manifests.** Under HMAC every worker held the signing key | **Fixed in software (0.5.9):** coordinator signs with an Ed25519 private key (owner-only file, refused if others can read it); workers verify with the public key, pinned on first contact or given by the operator, and stop if it changes. **Since 0.5.16:** planned rotation (`app.key_rotation`): a handover signed by both the old and new key; pinned workers follow a valid chain and refuse anything else; queued jobs are re-signed only if their old signature is from a retired key of ours. **Since 0.5.24** (`handover-key-v1`, opt-in): a separate offline key (`make-handover-key`, kept on a USB stick) whose public half the coordinator announces, signed by its current key; workers pin it, and from then on only follow handovers that it also signed; `rotate` refuses to run without it | First-contact pinning trusts the first answer. Stealing the coordinator's key file still lets someone sign jobs until the owner rotates. Workers that never pinned a handover key can still be moved by a thief's handover. Losing the stick means re-pinning by hand | Give workers both keys out of band; make the handover key and rotate with it right after a suspected theft; keep the signing key off shared machines |
| 10 | **Trusted coordinator/operator** | Append-only `events` table, `check_consistency` invariants anyone can run. **Since 0.5.11:** closed-period reward reports are signed with the coordinator's Ed25519 key (separate `purpose`), so nobody can edit a report after it's issued and anyone with the public key can check it offline. **Since 0.5.13:** `app.ledger_copy export` makes a redacted ledger copy of a closed, settled period that anyone can re-run the report and every consistency invariant against | The operator picks auditors, stores expected answers and could edit the database before signing and exporting. A copy proves the report matches the copy, not that the copy matches what really happened. **Since 0.5.30:** `ledger_copy compare` shows whether a later copy rewrote, deleted or back-dated anything in an earlier copy's period, so editing history after sharing it is caught by anyone who kept the earlier copy; editing *before* the first export is not | ~~Publish signed reports and copies in a fixed public place~~ (done, 0.5.42: `reports/`, on the site and GitHub); ~~compare copies across periods~~ (done, 0.5.30); later, independent re-verification |
| 11 | **Challenge leak.** If workers learn which jobs are tests, they only behave on those | Challenges get the same envelope, receipt and public events; public ledger merges challenge outcomes into confirmed/held. **Since 0.5.31** (`test-reuse-v1`): every ledger copy export is recorded, and a new hidden test with the exact input of a hidden test from a period a copy covers is refused (`409`); operator health warns while open hidden tests share an input with another one. **Since 0.5.39** (`test-reuse-v2`): near copies count as the same input: the comparison uses a fingerprint of the recognisable text (for `llm:generate` the prompt only, so a new seed, `max_tokens`, timeout or model doesn't make a new test) with case, spacing, punctuation, invisible characters and NFKC look-alike forms ignored; the private pool skips near copies too. **Since 0.5.35** (`llm-run-v1`): the worker hands prompts to llama-cli in a private file instead of on its command line, so other users on a rig can't read them with `ps`, and llama-cli gets no tokens or codes from the worker's environment | The operator API itself (protected by `OPERATOR_TOKEN`). Reworded versions of a revealed test (other words, another language, look-alike letters from another alphabet) aren't caught; re-parameterised and re-punctuated ones are since 0.5.39. A copy made from a backup isn't recorded | Keep the token off worker machines; rotate it; write each hidden test fresh. **Since 0.5.60** (`operator-client-v1`): the operator tools refuse to send the token and the answers over plain http to another machine |
| 12 | **Replay / duplicate submissions** | Lease holder check, idempotent receipts, deadlines | None known in software | Re-test across a real network |
| 13 | **Real GPU nondeterminism** makes honest rigs disagree | Tolerance rule: ignore whitespace only | Unknown until two real RX 470s run the same job. Honest rigs could be held or outvoted | Measure in Phase 3, then tune the tolerance per task |
| 14 | **Reading the reward report to spot hidden tests.** A failed challenge is "held" the moment it's accepted, while normal work is pending or unverified | Reward reports (Phase 5) are operator-only and list buckets, not job kinds. **Since 0.5.38** (`public-delay-v1`): the public `GET /v1/ledger` counts every result as `pending` and withholds its `confidence` for `PUBLIC_SETTLE_DELAY_SECONDS` (default 3600) after it was accepted, whatever kind of job it was. Before, a rig could read the public totals just before and after submitting: a passed hidden test landed straight in `confirmed`, a failed one in `held`, normal AI work in `pending`/`unverified`. The delay also hides a re-run's `confidence` flipping to `replicated`, which told the auditor its job was a re-check. The operator token sees it live; `0` restores the live view and operator health warns. **Since 0.5.40** (`public-no-hashes-v1`): public entries no longer show `result_hash`. It is the sha256 of the answer, so a re-check that agreed had the same hash as its original: anyone could pair the two at any time, a rig could count its own re-checks (and so guess its trust tier), the auditor could spot its own hash on another rig's row, and a passed hidden test showed the hash of the expected answer. The operator view keeps the hashes; ledger copies already blanked them. **Since 0.5.41** (`public-counts-v1`): the public job counts (`/health`, `GET /v1/ledger`) are taken once per clock-aligned window of `PUBLIC_COUNTS_INTERVAL_SECONDS` (default 900) and repeated until the next one, for everyone and every job kind. Live, `queued` rose by one the instant a rig's result was picked for a re-check; now a rig sees at most one change per window, the sum of everything in it. The operator token sees live counts; `0` = live and operator health warns | A worker who sees a published per-job report could guess which held job was a test. A shared ledger copy (0.5.13) goes further and shows which past jobs were tests and re-runs (not their prompts or answers). After the delay, a result whose bucket is already settled while normal work from the same moment is still `pending` is a weaker hint (only if audits take longer than the delay). On a quiet coordinator (one or two rigs, few jobs) a 15-minute window may hold only one rig's activity, so the batched counts still hint at re-checks; a longer window or more rigs helps. One snapshot per coordinator process | Publish reports only after the period closes; share ledger copies with a delay. **Since 0.5.31:** inputs of revealed hidden tests can't be reused as hidden tests (`test-reuse-v1`, see #11). Public queue counts batched for everyone (`public-counts-v1`, done in software, 0.5.41). Simply leaving re-checks out of the live counts wouldn't have worked: the auditor would see the counts *not* move when it claimed its job, which tells it the job was a re-check. Later, if needed: a longer window, or dropping public counts entirely |
| 15 | **Cheating pays on unsampled jobs.** Scoring `sim-points-v1` pays unverified work and adds no penalty when a worker is caught | Caught results are held, not paid. **Since 0.5.36** (opt-in `REWARD_SCORING=sim-points-v2-forfeit`): a rig with a strike in a period (failed hidden test or outvoted) forfeits its unverified points for that period; confirmed points are kept | A cheater still keeps points for the fakes nobody re-ran | Scoring v2 penalty rule (owner decision), e.g. hold a worker's unverified points for the period after a failed challenge or outvote |
| 16 | **Minting points by queuing your own job.** `POST /v1/jobs` needed no login, so any rig could queue a 1000-point job on an empty queue and claim it | **Fixed in software (0.5.19):** queuing a job needs the operator token or a separate submitter token (`SUBMITTER_TOKEN`), which can only queue normal jobs; worker keys never work there. **Since 0.5.22** (`points-table-v1`): with `POINTS_TABLE` set, every new job and hidden test gets a fixed number per task type and any other number is refused (`422`), for the operator too; operator health warns while a submitter token is set without a table | Off by default: without a table the submitter still picks points (up to 1000). With a table, a submitter can still queue many jobs (slowed by the request limit). The numbers themselves are a guess until real GPU cost is measured | Set `POINTS_TABLE`; give the submitter token only to programs you run; later, points tied to measured work (tokens generated, GPU seconds) |
| 17 | **Flooding the coordinator or guessing tokens** | **Since 0.5.19** (`app/rate_limit.py`): per-address request limit (`RATE_LIMIT_PER_MINUTE`, `429` + `Retry-After`) and a lock-out after `AUTH_FAILURE_LIMIT` wrong keys, tokens or codes in `AUTH_FAILURE_WINDOW_SECONDS`; `/health` exempt; addresses kept out of logs (keyed tag instead); operator health lists locked-out addresses. **Since 0.5.20** (`app/client_address.py`): `TRUSTED_PROXIES` (empty by default) lets limits apply to the real client behind your own reverse proxy; `X-Forwarded-For` is read from the right and only from listed proxies, so a client can't choose its address; trust-everyone refused; missing/garbled headers counted and warned about. **Since 0.5.52** (`input-limits-v1`): request bodies over `MAX_BODY_BYTES` (256 KiB) refused with `413` before they are read, JSON nested deeper than 32 levels refused with `400`, `422` errors no longer echo what was sent; a route-by-route fuzz checks no hostile input gives a `5xx`. **Since 0.5.62** (`guess-slot-v1`): failures used to be counted only when a check finished, so wrong tokens sent at the same moment were all checked before the first one counted (shown on a real server: 230 at once, all 230 checked against a limit of 10). Now a request with an operator or submitter token takes a guess slot first; failures plus slots in use never go above `AUTH_FAILURE_LIMIT`, and a request without a slot gets `429` without its token being looked at (the same burst: exactly 10 checked). **Since 0.5.63** (`ledger-pages-v1`): flooding doesn't need many requests if each one is expensive. With 100,000 results the public `GET /v1/ledger` sent all of them (20 MB) and re-ran the whole consistency check on every request (0.86 s each), so one address inside the request limit (4 a second) made a rig wait up to 7.8 s for a job. Now entries come in pages (default 100, at most 1000, indexed), the public point totals and consistency check are taken once per counts window, and one job's trail is found through an index: 9 ms per public reply, rig unaffected. **Since 0.5.64** (`refusal-log-v1`): one enrolled rig could make the coordinator *write* without end: every refused submission (conflict, wrong worker, receipt mismatch, the last on any job id) added an audit-trail row (shown on a real server: 180 B each, 345,600 rows or about 62 MB a day inside the request limit, and a day of them on another rig's job made its public `GET /v1/jobs/{id}` 54 MB and 1.5 s). Now the same refusal (job, rig, reason) is written once, repeats are counted for operator health (warning at 100), the public job trail is filtered and capped in the database (200, `events_total`), and an index keeps each accepted result's audit-rate lookup to that rig's own results | In memory only (restart clears it); rigs behind one home router still share limits (that is their real address); many addresses get many buckets, so a distributed flood isn't stopped; a misconfigured listed proxy could let clients choose their address | Put the coordinator behind a host with DDoS protection before it's public |

## Honest limits

- **Agreement is evidence, not proof.** Two or three workers agreeing only means a cheater
  would have needed to control all of them.
- **Nothing here proves which hardware ran the job.** A worker that runs the pinned model on a
  rented cloud GPU, or on a CPU slowly, gives the same answer and passes every check.
  That is fine for useful work; it matters only if rewards are ever meant to be "GPU only".
- **The coordinator is trusted.** This is a central service run by the project, not a
  trustless network.
  Trusted to sign honest jobs, not to send well-formed replies: since 0.5.53
  (`worker-input-v1`) the worker refuses oversized, slow, compressed or malformed replies,
  prints none of the coordinator's text with terminal control codes, and won't send its key
  over plain http to another machine, so a fake coordinator or a man in the middle can't
  crash a rig or read its key. Since 0.5.60 (`operator-client-v1`) the same goes for the
  operator's own tools (`challenge_pool issue|issue-leads`, `rehearsal --url`), which
  carry the operator token and hidden tests' expected answers: no plain http to another
  machine without `--allow-plain-http`, no redirects, size/time/compression limits, safe
  printing, and a reply without a proper job id is never counted as an issued test.
- **Ledger copies are files from strangers.** Anyone re-checking a published copy runs our
  tools on a file someone else made. Since 0.5.54 (`copy-open-v1`, `app/copy_guard.py`)
  a copy must hold exactly the plain tables `ledger_copy export` writes (no views, which
  could make the numbers depend on the date or the reader's machine; no triggers, virtual
  or extra tables), pass `integrity_check`, stay under size limits, and is opened read-only
  and immutable, so checking it never changes it. That makes reading a copy safe, not its
  numbers true.
- **What rigs, the submitter and strangers write ends up on the operator's screen.** A rig's
  answer is any text up to 8 KiB, and the review queue shows held answers side by side.
  Before 0.5.61 the text views printed it as it was, so an answer with terminal escape
  codes could clear the operator's screen, rename the window or print a fake instruction
  (shown with a real ledger). Since 0.5.61 (`operator-screen-v1`, `app/screen.py`) the
  review queue, vouch check, rig list, pause status, `ledger_copy info`/`compare` and the
  site and dependency checkers print every line with control codes and invisible marks as
  `?`. The JSON views and the API still give the exact text, so a person can see what was
  really sent. Tools that write JSON for another program to show are only as safe as that
  program.
- **Reports, release files and the public index are JSON from strangers too.** Since
  0.5.55 (`json-file-v1`, `app/json_file.py`) they are read with size, depth, encoding,
  `NaN` and duplicate-key limits. A report's `fingerprint` field is only a label (not
  signed), so it must match the key that signed it and tools print the fingerprint they
  compute: before, a report signed by anyone could show the coordinator's fingerprint to
  someone checking without `--public-key`. `publish_report check` also checks that every
  number the /reports/ page shows matches the signed report.
- **Rig reports for the hardware check are files from strangers as well.** Since 0.5.56
  (`rig-report-v1`) `phase3_check` reads them through `json_file` and type-checks every
  field it uses: before, the text "false" counted as true, so a crashed run sent in as
  `"exit_ok": "false"` passed as a clean smoke test. They are still unsigned: this
  catches mistakes and sloppy edits, not a careful forger; a repeat by someone else is
  what makes them evidence.
- **A release folder is a download from a stranger too.** Since 0.5.57
  (`release-folder-v1`) `release verify --root` treats anything Python could import that
  isn't in the signed release as a problem, refuses links and unreadable folders, and only
  accepts a `__pycache__` file that is byte-for-byte what this Python builds from the
  checked source. Before, a sourceless `json.pyc`, a cache file with the source's time and
  size in its header, or a linked folder named `json` passed the check and ran when the
  worker started. It checks the folder when you run it, not afterwards.
- **The model file comes from the internet too.** Since 0.5.58 (`model-fetch-v1`)
  `model_registry download` follows only https redirects (at most 5), refuses compressed
  replies, caps the size (the pin, or twice the registry's estimate) and the total time,
  checks against `--expect-sha256` (the hash on the model page) while it writes, and never
  writes through a link or over an existing file; `pin` needs that hash (or an explicit
  opt-out) and won't silently change an existing pin. Before, a redirect to plain http
  was followed and wrote 3.2 GB in 6 seconds. The registry's file names can no longer
  point outside the model folder. A published hash proves the file wasn't changed on the
  way, not that the publisher's model is honest.
- **Other accounts on the same machine.** Since 0.5.59 (`secret-file-v1`) the ledger
  (which holds every hidden test's expected answer), the signing keys and a worker's key
  are created owner-only, and the coordinator and worker refuse to use them, or a pinned
  coordinator key, when other users can read or change the file or its folder. Before,
  the ledger was readable by every account (one copied it and read a hidden test's
  answer). This does not protect against root or anyone running as the same user.
- **The tolerance rule is a guess** until Phase 3 produces real output from two cards.

## Order of the next fixes

1. ~~Per-worker keys and authenticated registration~~ **Done (software only):** enrollment codes, per-worker keys, owner-aware exclusions, revocation. Narrows #2, #3 and #6 to "rings the operator was fooled into enrolling".
2. ~~Random auditor assignment instead of first-come (closes #4).~~ **Done (software only, 0.5.8).** Held for a person when a rig on a tie-breaker's winning side isn't established (`tiebreak-trust-v1`, opt-in, #6): **done (software only, 0.5.34).** Counting audits a rig lets go (`audit_follow_through`): **done (software only, 0.5.17).** Trust-aware pick for audits and tie-breakers (`auditor-pick-v1`, opt-in): **done (software only, 0.5.23);** switching it on is an owner decision.
3. Measure real GPU nondeterminism on the RX 470s and tune the tolerance (#13).
4. ~~Asymmetric manifest signatures (#9).~~ **Done (software only, 0.5.9).** Planned key rotation with handovers signed by both keys: **done (software only, 0.5.16).** Offline handover key so a stolen signing key can't move pinned workers: **done (software only, 0.5.24);** making the key is an owner step.
5. Signed, published ledger snapshots (#10). **First step done (software only, 0.5.11):** reward reports for closed periods are signed and verifiable offline. **Second step done (software only, 0.5.13):** redacted ledger copies others can re-run reports against. Comparing copies across periods (`ledger_copy compare`): **done (software only, 0.5.30).** Fixed public place to post both: **done (software only, 0.5.42, `reports-v1`):** the repo's `reports/` folder, served at boatsnhoes.meme/reports/; `publish_report add` checks before posting and never replaces anything, `publish_report check` (run by CI) re-verifies all of it. Nothing published yet.
6. A penalty rule for caught cheaters in scoring v2 (#15). **Candidate built (software only, 0.5.36):** `sim-points-v2-forfeit`, off by default; switching it on (or asking for a different rule) is an owner decision.
7. ~~Raise the audit rate for new or low-trust workers (#1).~~ **Done (software only, 0.5.15):** `audit-tier-v1`. Changes how often a rig is checked, not its points. Aimed hidden tests for one rig or a tier: **done (software only, 0.5.21).**
8. ~~Operator review queue for held cases the software can't settle (#7).~~ **Done (software only, 0.5.18):** read-only list with answers side by side; resolving a case is still an owner decision.
9. ~~Close open job creation and add request limits (#16, #17).~~ **Done (software only, 0.5.19):** submitter/operator token to queue jobs, per-address rate limit, lock-out after wrong credentials. Trusted-proxy setting so limits apply to the real client: **done (software only, 0.5.20).** Fixed points per task type (`POINTS_TABLE`): **done (software only, 0.5.22).**
10. Follow up on rigs that agreed with a caught rig (#3). **Done (software only, 0.5.43):** `vouch-check-v1` lists the leads and the confirmations that rest only on a caught rig; what weakly confirmed points should become is part of scoring v2. Aiming one hidden test at each lead from the private pool: **done (software only, 0.5.44, `lead-tests-v1`):** `challenge_pool issue-leads`, one command, skips leads that already have a test waiting; the operator runs it (nothing is sent automatically), and it needs real model answers in the pool to mean much.
