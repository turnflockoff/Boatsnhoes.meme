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
| Signed manifests (HMAC) | A fake coordinator. Workers refuse jobs whose signature doesn't check out. | Yes, tested |
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
| 3 | **Colluding auditor** confirms a friend's fake answer | Self-audit and same-owner audit blocked; audit hidden among normal work | Two ring members agree on a fake: marked "confirmed" | Random auditor assignment, challenges sent to suspected rings, trust weighting |
| 4 | **Racing for audits.** Audits go to whichever eligible worker asks next, so a ring that polls fast gets more of them | None | A busy ring audits itself more often than chance | Coordinator assigns each audit to a randomly chosen eligible worker instead of first-come |
| 5 | **Spotting an audit by its payload.** An audit carries the same prompt as the original | Same envelope, same deadline window, private events hidden | Colluders who compare notes can match prompts | Mix in duplicate normal jobs so a repeated prompt isn't a tell; later, re-run on a slightly different but equivalent input where the task allows |
| 6 | **Two of three in the tie-breaker are colluding** | Tie-breaker excluded from both sides of the dispute and from their owners | Majority of three can **outvote an honest auditor** if the original worker and the tie-breaker are in the same ring | Same as #2 and #4, plus never letting a single tie-breaker decide on low-trust workers |
| 7 | **Only two rigs online**, so no third worker exists | Tie-breaker expires, dispute stays held (`tiebreak_not_run`) | A person has to look at it | Operator review queue (manual for now) |
| 8 | **Model swapping** (smaller or tampered model) | Fingerprint check runs on the worker, which the cheater controls | A cheating worker can skip the check entirely | Audits catch it only if the swapped model's output differs. Challenges with known outputs from the pinned model help |
| 9 | **Shared secret leaks.** HMAC is symmetric, so every worker holds the signing key | Secret kept out of logs and API responses | Any worker could forge job manifests to other workers | Asymmetric signatures (coordinator signs, workers only verify) |
| 10 | **Trusted coordinator/operator** | Append-only `events` table, `check_consistency` invariants anyone can run | The operator picks auditors, stores expected answers and could edit the database | Publish signed ledger snapshots; later, independent re-verification |
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
2. Random auditor assignment instead of first-come (closes #4).
3. Measure real GPU nondeterminism on the RX 470s and tune the tolerance (#13).
4. Asymmetric manifest signatures (#9).
5. Signed, published ledger snapshots (#10). The Phase 5 `report_sha256` is the first step: a reproducible fingerprint of each report, not yet signed.
6. A penalty rule for caught cheaters in scoring v2 (#15).
