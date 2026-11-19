# Boatsnhoes.meme

**Boats N Hoes (HOES)** — proof-of-work crypto project research and prototype.

> Status: early design stage. No mainnet, audited miner, token deployment, or guaranteed mining rewards are being claimed.

## Architecture decision required

The original project note described proof of work on Ethereum. An ERC-20 token on Ethereum does not run its own mining algorithm: Ethereum validators secure the chain. For actual native proof-of-work mining, BoatsnHoes would need an independent blockchain with nodes, networking, transaction validation, block rewards, difficulty adjustment, wallets, and an explorer.

The current recommendation for a CPU-focused native-chain research prototype is to evaluate the established **RandomX** proof-of-work algorithm rather than invent a new cryptographic hash. RandomX is designed to favor general-purpose CPUs, but it is not guaranteed ASIC-proof or profitable.

## Design notes

See [`docs/POW-ARCHITECTURE.md`](docs/POW-ARCHITECTURE.md) for the draft consensus architecture, proposed test parameters, miner/node module layout, verification requirements, and launch decision between an Ethereum token and an independent PoW chain.

## Next milestone

Build a local-only RandomX proof-of-work harness: deterministic block-header serialization, upstream RandomX integration, target validation, known test vectors, and CPU/memory benchmarks. Keep it test-only until consensus rules have independent review.

## References

- [RandomX](https://github.com/tevador/RandomX)
- [RFC 9106 — Argon2](https://www.rfc-editor.org/rfc/rfc9106.html)
- [Bitcoin block-chain reference](https://bitcoindevelopers.org/docs/reference/block-chain-ref/)
