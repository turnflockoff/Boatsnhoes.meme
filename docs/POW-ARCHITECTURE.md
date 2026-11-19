# Boatsnhoes.meme — Proof-of-Work Architecture (Draft)

> Status: research prototype / design proposal. This is not a launched coin, audited consensus implementation, or promise of mining profitability.

## Executive recommendation

The repository README currently describes a proof of work on the Ethereum blockchain. That phrase needs a precise architecture decision:

- **Ethereum-issued token:** Deploy an ERC-20 token on Ethereum. Ethereum validators secure the chain; a token contract cannot replace Ethereum's consensus or make the token itself a native Ethereum PoW coin.
- **Independent native PoW chain:** Build and operate a separate blockchain with its own nodes, peer-to-peer network, transaction rules, block production, difficulty adjustment, issuance schedule, wallets, and explorer. This is materially more work and needs independent security review.

For a CPU-oriented native PoW experiment, evaluate **RandomX** as the proof-of-work function instead of inventing a new cryptographic hash. RandomX is designed to favor general-purpose CPUs using randomized program execution and memory-hard techniques. It is not ASIC-proof; specialized hardware and optimized implementations may still gain advantages. Pin an exact reviewed upstream release or commit and verify licensing before integration.

Primary references:
- RandomX project: https://github.com/tevador/RandomX
- Argon2 specification (RFC 9106): https://www.rfc-editor.org/rfc/rfc9106.html
- Bitcoin-style target semantics background: https://bitcoindevelopers.org/docs/reference/block-chain-ref/

## Proposed prototype profile (not mainnet parameters)

| Setting | Initial proposal | Notes |
|---|---:|---|
| Chain name | BoatsnHoes | Working name |
| Symbol | HOES | Branding proposal only; check ticker conflicts before launch |
| Consensus | Native Proof of Work | Only if a separate chain is selected |
| PoW function | RandomX | Use an established implementation; do not fork its cryptography casually |
| Hash output | 256-bit | Compare as an unsigned integer to target |
| Block interval | 120 seconds | Simulation target; tune after testnet measurements |
| Difficulty retarget | Every 60 blocks | Use median-time-past and bounded adjustment |
| Max adjustment | 4x easier / 4x harder per window | Prevent extreme single-window swings |
| Epoch seed | Deterministic from ancestor block hash at fixed epoch boundary | Define exact epoch and seed transition to prevent miner disagreement |
| Coinbase maturity | 100 blocks | Testnet starting value |
| Max block weight | 2,000,000 weight units | Must be finalized with transaction encoding |
| Subsidy | Unset | Do not invent economics before supply and issuance decisions |
| Premine / treasury | Unset | Explicit governance and disclosure decision required |

Do not launch with these defaults without adversarial testing and a published consensus specification.

## PoW header and verification model

A candidate block header must include consensus-critical fields, encoded canonically and identically by every node:
- version
- previous block hash
- transaction Merkle root
- timestamp
- compact target / difficulty field
- nonce
- RandomX key/seed derivation context (deterministically derived; never miner-chosen)

Conceptual algorithm:
1. Validate the parent and derive the epoch seed from the chain's deterministic seed schedule.
2. Serialize the header with a single canonical byte order and fixed-width encoding.
3. Compute the PoW hash by running the upstream RandomX implementation against the serialized header using the deterministic epoch seed.
4. Decode the compact target with strict overflow and range checks.
5. Accept only if the 256-bit PoW result is less than or equal to the target and the target is no easier than the chain's POW_LIMIT.
6. Independently validate transactions, block weight, coinbase amount, timestamps, and all state transitions.

The actual RandomX API uses a keyed VM/cache/dataset configuration. Integration must follow the upstream implementation's exact API and test vectors; this is a protocol outline, not drop-in code.

## Difficulty adjustment requirements

For each retarget window:
- Use a robust timestamp statistic, preferably median-time-past, rather than trusting one miner timestamp.
- Clamp elapsed time to a documented range before calculating the new target.
- Use integer arithmetic with explicit rounding and overflow checks.
- Enforce 0 < target <= POW_LIMIT.
- Specify behavior for missing history, genesis, timestamp ties, reorgs, and testnet minimum-difficulty exceptions.
- Add deterministic test vectors at boundaries and on reorged branches.

Do not copy Bitcoin's retarget algorithm partially. Either adopt a fully specified known algorithm with its assumptions, or specify and test this chain's exact rules.

## Miner software layout

Suggested modules:
- `consensus/pow`: header serialization, RandomX wrapper, seed schedule, target decoding, PoW verification.
- `consensus/difficulty`: deterministic retarget calculation.
- `node/chain`: block validation, fork choice, chain state.
- `node/p2p`: peer protocol, rate limits, anti-DoS.
- `miner`: template retrieval, nonce search, result submission, graceful cancellation.
- `wallet`: key management and address format (separate security review required).
- `rpc`: authenticated local RPC; never expose wallet-control RPC publicly by default.
- `tests/vectors`: canonical header, PoW, target and retarget vectors.

## Required test plan

Before even a public testnet:
- Upstream RandomX test vectors pass on every supported platform.
- Header serialization has byte-for-byte golden vectors.
- Same block validates identically across at least two independent implementations.
- Target decoder rejects zero, negative-equivalent, overflow, non-canonical and above-limit values.
- Difficulty tests cover boundary windows, manipulated timestamps, integer rounding, reorgs and extreme elapsed times.
- Coinbase issuance cannot exceed the exact scheduled subsidy plus fees.
- Reorg tests confirm deterministic chain selection and no double application of state transitions.
- Fuzz parsers, serialization, compact-target decoding, block validation and P2P messages.
- Run a testnet with independent operators; commission external consensus and cryptography review.

## Important product and security notes

- A RandomX PoW function alone does not create a blockchain. Nodes, networking, transaction validation, chain selection, issuance, wallets, and upgrade rules are also consensus/security-critical.
- CPU-friendly does not mean fair, profitable, ASIC-resistant, or secure against majority attacks.
- Never promise returns, mining income, price appreciation, or guaranteed decentralization.
- Do not collect seed phrases or private keys. Do not place wallet secrets in logs, frontend code, GitHub, or committed environment files.
- Do not advertise the project as audited or production-ready until independent audits are completed and published.

## Open architecture decision

The current README says proof of work on the Ethereum blockchain. Choose one before implementing a real miner:

A. **Ethereum token + off-chain/contract-verified mining rewards:** easier to prototype, but it is not native PoW and introduces oracle, anti-Sybil, and reward-contract design problems.

B. **Independent RandomX-based PoW chain:** the correct direction if the requirement is actual native PoW mining, but requires a full node and chain implementation.

Recommended next milestone: a local-only PoW harness that integrates the upstream RandomX library, tests header hashing and target verification, benchmarks CPU memory use and hashes per second, and has no token value or mainnet claims. Then decide whether to build a native chain or an Ethereum token.