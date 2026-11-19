# Boatsnhoes.meme — Earlier GPU hash-mining design (superseded)

> **Status: historical research only.** This document records an earlier alternative in which GPUs performed project-specific hash-based proof-of-work and rewards were settled on Ethereum. It is not the current prototype direction. See [AI-USEFUL-COMPUTE-PROTOTYPE.md](AI-USEFUL-COMPUTE-PROTOTYPE.md).

## Historical proposal

The earlier concept was GPU proof-of-work mining that earned Boats N Hoes tokens, with reward issuance stopping six months before a declared project end date, and token/reward settlement recorded on Ethereum.

Ethereum Mainnet itself cannot be GPU-mined. Ethereum switched from proof-of-work to proof-of-stake at The Merge on September 15, 2022; mining was switched off. See:
- https://ethereum.org/roadmap/merge/
- https://ethereum.org/developers/docs/consensus-mechanisms/pow/mining/

The accurate description for that historical design was **GPU-mined project rewards, settled on Ethereum** — not Ethereum mining.

## Why this is not the current direction

The project has since shifted toward using GPUs for useful AI inference tasks rather than spending compute solely on hash searching. The useful-compute approach has different engineering requirements: task assignment, model/input versioning, result validation, audit sampling, worker reputation, and reward accounting. It must not be represented as a completed or trustless proof-of-useful-work consensus mechanism.

## Historical hash-mining architecture (not selected)

1. An ERC-20 token would represent balances and transfers.
2. A separate GPU workload would search for valid shares using a selected proof-of-work algorithm.
3. A coordinator/pool would issue jobs and independently recompute submitted proofs.
4. A reward distributor would settle only finalized, verified claims.
5. A fixed project-defined cutoff could stop new reward accrual six calendar months before a declared project end date.

Candidate algorithms previously considered included Ethash-style and other established GPU-oriented proof-of-work algorithms. No algorithm was selected, implemented, benchmarked, or audited. No ASIC-resistance or profitability claim was established.

## Historical protocol considerations

A hash-mining job would need a project/algorithm identifier, unique job ID, challenge, dataset epoch/seed, target, expiry, and payout epoch. Submissions would need the job ID, payout address, nonce/solution, and claimed share difficulty. A coordinator must independently recompute proofs, reject expired/duplicate jobs, rate-limit abuse, and maintain auditable share and payout records.

## Historical schedule considerations

Ethereum has no announced end date. Any six-month early-stop rule must be tied to a project-defined end date, not an assumed Ethereum shutdown. The cutoff, claim deadline, emergency pause, and treatment of unclaimed rewards would need explicit, reviewed rules.

## References

- [Ethereum Merge](https://ethereum.org/roadmap/merge/)
- [Ethereum proof-of-work mining documentation](https://ethereum.org/developers/docs/consensus-mechanisms/pow/mining/)
