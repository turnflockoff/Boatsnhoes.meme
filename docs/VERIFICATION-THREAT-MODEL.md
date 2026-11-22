# Verification threat model: fraud and collusion (GPU worker prototype)

**Status:** draft, written 2026-11-22 against prototype v0.4.1 (Phase 4 complete in software).
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
| 1 | **Lazy/fake answers** on AI jobs | Re-run audits, challenges, held points on mismatch | Survives `(1-p)^n` of the time; unaudited results show as "unverified", not "confirmed" | Raise audit rate for new or low-trust workers |
| 2 | **Sybil: one person, many workers** | One-time operator enrollment codes tied to an `owner` label; per-worker keys (stored as hashes) on every claim and submit; same-owner workers never audit or break ties for each other; revocation | A person who gets codes under several names is still a ring. Labels are only as good as the operator's hand-out | Hand out codes one per verified person; later, reputation built up over time that is costly to fake |
| 3 | **Colluding auditor** confirms a friend's fake answer | Self-audit and same-owner audit blocked; audit hidden among normal work; auditor picked at random among active workers, so a ring of k in n active workers audits a friend only about k/n of the time | Two ring members agree on a fake: marked "confirmed" | Random auditor assignment, challenges sent to suspected rings, trust weighting |
| 4 | **Racing for audits.** Audits used to go to whichever eligible worker asked next, so a ring that polled fast got more of them | **Fixed in software (0.5.8):** each audit and tie-breaker is assigned to one eligible worker chosen with secure randomness from those active in the last `AUDITOR_ACTIVE_SECONDS`; only that worker can claim it; re-assigned at random if it doesn't claim in `AUDIT_ASSIGN_SECONDS`. Consistency check flags an audit taken by anyone else | A ring can still raise its share by keeping more enrolled workers active (that's #2, Sybil). A ring member that refuses an audit it doesn't like just passes it on to another random pick | Weight the pick by trust/age; count refused or timed-out audits in the trust report |
| 5 | **Spotting an audit by its payload.** An audit carries the same prompt as the original | Same envelope, same deadline window, private events hidden | Colluders who compare notes can match prompts | Mix in duplicate normal jobs so a repeated prompt isn't a tell; later, re-run on a slightly different but equivalent input where the task allows |
| 6 | **Two of three in the tie-breaker are colluding** | Tie-breaker excluded from both sides of the dispute and from their owners | Majority of three can **outvote an honest auditor** if the original worker and the tie-breaker are in the same ring | Same as #2 and #4, plus never letting a single tie-breaker decide on low-trust workers |
| 7 | **Only two rigs online**, so no third worker exists | Tie-breaker expires, dispute stays held (`tiebreak_not_run`) | A person has to look at it | Operator review queue (manual for now) |
| 8 | **Model swapping** (smaller or tampered model) | Fingerprint check runs on the worker, which the cheater controls. Since 0.5.10 each model job carries the pinned `model_sha256` under the signature and the receipt must echo it, which catches honest wrong-model setups but not a liar | A cheating worker can skip the check entirely | Audits catch it only if the swapped model's output differs. Challenges with known outputs from the pinned model help |
| 9 | **Forged job manifests.** Under HMAC every worker held the signing key | **Fixed in software (0.5.9):** coordinator signs with an Ed25519 private key (owner-only file, refused if others can read it); workers verify with the public key, pinned on first contact or given by the operator, and stop if it changes | First-contact pinning trusts the first answer; no key rotation yet; stealing the coordinator's key file still lets someone sign | Give workers the key out of band; add a rotation procedure; keep the key off shared machines |
| 10 | **Trusted coordinator/operator** | Append-only `events` table, `check_consistency` invariants anyone can run. **Since 0.5.11:** closed-period reward reports are signed with the coordinator's Ed25519 key (separate `purpose`), so nobody can edit a report after it's issued and anyone with the public key can check it offline | The operator picks auditors, stores expected answers and could edit the database, then sign a report built from it. A signature proves who issued the numbers, not that they're right | Publish signed reports in a fixed public place; share ledger copies so others can re-run `verify --db`; later, independent re-verification |
| 11 | **Challenge leak.** If workers learn which jobs are tests, they only behave on those | Challenges get the same envelope, receipt and public events; public ledger merges challenge outcomes into confirmed/held | The operator API itself (protected by `OPERATOR_TOKEN`) | Keep the token off worker machines; rotate it |
| 12 | **Replay / duplicate submissions** | Lease holder check, idempotent receipts, deadlines | None known in software | Re-test across a real network |
| 13 | **Real GPU nondeterminism** makes honest rigs disagree | Tolerance rule: ignore whitespace only | Unknown until two real RX 470s run the same job. Honest rigs could be held or outvoted | Measure in Phase 3, then tune the tolerance per task |
| 14 | **Reading the reward report to spot hidden tests.** A failed challenge is "held" the moment it's accepted, while normal work is pending or unverified | Reward reports (Phase 5) are operator-only and list buckets, not job kinds | A worker who sees a published per-job report could guess which held job was a test | Publish reports only after the period closes; consider delaying the public bucket for challenges |
| 15 | **Cheating pays on unsampled jobs.** Scoring `sim-points-v1` pays unverified work and adds no penalty when a worker is caught | Caught results are held, not paid | A cheater still keeps points for the fakes nobody re-ran | Scoring v2 penalty rule (owner decision), e.g. hold a worker's unverified points for the period after a failed challenge or outvote |

## Honest limits

- **Agreement is evidence, not proof.** Two or three workers agreeing only means a cheater
  would have needed to control all of them.
- **Nothing here proves which hardware ran the job.** A worker that runs the pinned model on a
  rented cloud GPU, or on a CPU slowly, gives the same answer and passes every check.
  That is fine for useful work; it matters only if rewards are ever meant to be "GPU only".
- **The coordinator is trusted.** This is a central service run by the project, not a
  trustless network.
- **The tolerance rule is a guess** until Phase 3 produces real output from two cards.

## Order of the next fixes

1. ~~Per-worker keys and authenticated registration~~ **Done (software only):** enrollment codes, per-worker keys, owner-aware exclusions, revocation. Narrows #2, #3 and #6 to "rings the operator was fooled into enrolling".
2. ~~Random auditor assignment instead of first-come (closes #4).~~ **Done (software only, 0.5.8).**
3. Measure real GPU nondeterminism on the RX 470s and tune the tolerance (#13).
4. ~~Asymmetric manifest signatures (#9).~~ **Done (software only, 0.5.9).**
5. Signed, published ledger snapshots (#10). **First step done (software only, 0.5.11):** reward reports for closed periods are signed and verifiable offline. Still open: a public place to post them and ledger copies others can re-run.
6. A penalty rule for caught cheaters in scoring v2 (#15).
