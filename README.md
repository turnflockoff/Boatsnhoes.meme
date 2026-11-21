# Boatsnhoes.meme

**Boats N Hoes (HOES)** — experimental GPU useful-compute rewards project.

> Status: early design and hardware-validation stage. No working miner, verified compute network, deployed token, audited contract, live rewards, or guaranteed payouts are claimed.

## Project website

The site merges the $BNH meme-coin brand with this repo's GPU-compute work. Pages: [`index.html`](index.html) (home), [`contribute.html`](contribute.html) (GPU check + tester sign-up via Netlify Forms), `thanks.html`, `404.html`. Graphics live in [`assets/`](assets/); security headers in [`_headers`](_headers). The coin/casino plan, decisions and to-dos are in [`docs/plan/`](docs/plan/README.md). The complete milestone checklist is in [`docs/ROADMAP.md`](docs/ROADMAP.md). Deploy the repository root as a static site (for example, with Netlify; see `netlify.toml`). A public deployment URL is not claimed until one is configured and verified.

## Current prototype direction

The current research direction is an **RX 470 AI useful-compute prototype**: use available AMD GPUs to perform small AI inference tasks, validate results using task-specific checks and independent audits, and record accepted work in an off-chain ledger. Ethereum may be used later for ERC-20 reward settlement; it is not the compute network or mining algorithm.

See [docs/AI-USEFUL-COMPUTE-PROTOTYPE.md](docs/AI-USEFUL-COMPUTE-PROTOTYPE.md) for the proposed hardware, architecture, software compatibility checks, verification limits, reward boundary, and implementation milestones.

## Important distinctions

- Ethereum Mainnet uses proof-of-stake; it is not GPU-mined.
- This prototype is not Ethereum consensus and should not be marketed as Ethereum mining.
- Useful AI work is not automatically proof that a worker ran a model. Results require independent, task-specific verification; hashes and self-reported runtimes alone are not proof.
- Token issuance, reward rules, and any future Ethereum settlement are separate from the GPU compute prototype.

## Earlier hash-mining research

[docs/POW-ARCHITECTURE.md](docs/POW-ARCHITECTURE.md) is retained as historical research into a separate GPU hash-mining rewards design. It is not the current prototype direction.

## Next milestone

Validate both RX 470 cards on the actual Ubuntu installation, then complete one small Vulkan inference smoke test per GPU before building coordinator or reward infrastructure.
