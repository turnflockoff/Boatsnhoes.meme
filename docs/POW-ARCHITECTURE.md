# Boatsnhoes.meme — GPU mining campaign on Ethereum (revised design)

> Status: concept and technical plan only. No deployed token, audited contracts, live mining rewards, or guaranteed payouts are claimed.

## Correct architecture

The product goal is GPU proof-of-work mining that earns BoatsnHoes tokens, with reward issuance stopping six months before a declared project end date, and token/reward settlement recorded on Ethereum.

Ethereum Mainnet itself cannot be GPU-mined. Ethereum switched from proof-of-work to proof-of-stake at The Merge on September 15, 2022; mining was switched off. See:
- https://ethereum.org/roadmap/merge/
- https://ethereum.org/developers/docs/consensus-mechanisms/pow/mining/
- https://ethereum.org/developers/docs/consensus-mechanisms/pow/mining/mining-algorithms/

Therefore this project must not market its GPU workload as Ethereum block mining. The accurate description is: **GPU-mined project rewards, settled on Ethereum**.

## Recommended system

1. **Ethereum token contract:** ERC-20 token for balances and transfers.
2. **GPU PoW miner:** Runs locally on a miner's GPU and searches for valid shares against a published job.
3. **Mining pool / coordinator:** Issues jobs, tracks shares, and prevents duplicate submissions. This is an operational trust point unless replaced by a more elaborate decentralized validation design.
4. **Reward accounting:** Contract or audited distributor pays only finalized, verified reward claims. Never let an unaudited public API mint tokens.
5. **Ethereum settlement:** Publish claim roots or verified claim records on Ethereum; miners claim with Merkle proofs, or use a contract-based mechanism appropriate to expected volume and gas costs.
6. **Hard stop:** Smart contract enforces a fixed mining end timestamp. Mining reward accrual and claim creation stop at the configured cutoff. Define the actual project end date first; the cutoff is exactly six calendar months earlier, using UTC and a documented calendar-month rule. Ethereum has no announced end date, so do not claim the cutoff is tied to an Ethereum shutdown.

## GPU algorithm choice

For a GPU-first mining experience, do not use RandomX as the default: RandomX is intentionally CPU-oriented. Evaluate a well-studied GPU-suitable proof-of-work algorithm and existing audited implementations rather than inventing a hash function. Ethash is historically associated with Ethereum GPU mining, but Ethereum Mainnet's PoW is ended; Ethash can only be used as this project's own work function or on another compatible PoW network. Its DAG/memory-hard design enabled GPU mining, but ASICs were also developed.

Potential choices to benchmark:
- **Ethash-style:** Familiar GPU/DAG workload and established ecosystem history. Use only as this project's own work function, not Ethereum consensus.
- **Autolykos-style or another established GPU PoW:** Evaluate implementation maturity, licenses, ASIC dynamics, hardware support, and independent security analysis before selection.
- Never advertise any algorithm as ASIC-proof or profitable without evidence.

## Protocol outline

A mining job should include:
- chain/project identifier and algorithm identifier
- monotonically increasing job ID
- challenge / previous accepted job reference
- dataset epoch and seed, derived deterministically
- target / share difficulty
- job expiration time
- payout epoch and pool identifier

Miner submission should include:
- job ID
- miner payout address (validated as an Ethereum address)
- worker ID (display-only; not an authentication credential)
- nonce / solution
- claimed share difficulty

Coordinator validation:
- reject unknown or expired jobs
- recompute proof locally with the exact pinned algorithm version and parameters
- reject duplicate `(job_id, miner, nonce)` submissions
- enforce per-miner rate limits and anti-abuse controls
- record valid shares idempotently
- compute rewards from a published deterministic schedule
- expose auditable share and payout history

A pool must not accept a miner's claimed hash or difficulty without recomputing it independently.

## Six-month early stop

Use explicit immutable schedule fields:
- `project_end_at` — public declared end date (UTC)
- `mining_cutoff_at` — computed as six calendar months before `project_end_at`
- `claims_deadline_at` — separately defined; should not be confused with mining cutoff
- `paused` — emergency stop controlled by disclosed governance or multisig

Recommended contract behavior:
- mining/reward accrual rejects submissions at or after `mining_cutoff_at`
- no admin function can silently extend the cutoff
- any emergency pause is logged on-chain
- claims for rewards already earned remain available until `claims_deadline_at`
- unclaimed reward handling is published before launch
- if a schedule change is ever allowed before launch, emit events and provide advance notice; after launch, do not allow unilateral extension

Do not set an actual cutoff until the project end date is chosen. There is no known Ethereum end date to use.

## Initial development milestones

1. Benchmark candidate GPU algorithms on NVIDIA and AMD devices. Choose based on hashrate, VRAM requirement, power use, reproducible verification, license, and attack surface.
2. Build a local-only miner harness and CPU/reference verifier. Do not connect payouts yet.
3. Implement deterministic job encoding, share validation, duplicate rejection, and share difficulty retargeting.
4. Add a testnet ERC-20 and audited claim/distributor contract with an explicit emission cap and immutable cutoff.
5. Test with independent miners and adversarial submissions; audit contracts and reward logic.
6. Only then consider mainnet deployment and token launch disclosures.

## Tokenomics intentionally unset

Do not guess token supply, initial allocation, emissions, or exchange value. Define a hard cap, reward schedule, treasury allocation (if any), and treatment of unclaimed rewards transparently before deployment. Do not promise investment returns or mining profitability.

## Clear project wording

Use: **Boats N Hoes is a GPU-mined rewards project with token settlement on Ethereum. Ethereum secures the settlement layer; the project's own GPU proof-of-work campaign is separate from Ethereum consensus.**

Do not use: Mine Ethereum, Ethereum GPU mining, or Ethereum PoW coin.