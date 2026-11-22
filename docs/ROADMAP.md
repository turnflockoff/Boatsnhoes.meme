# Boats N Hoes — Master Roadmap

**Current phase:** hardware validation / early prototype  
**Source:** [GitHub repository](https://github.com/turnflockoff/Boatsnhoes.meme)  
**Public landing page:** `index.html` + `contribute.html` (Netlify project `boatsnhoes-dik6`; GitHub link pending)
**Merged plan (meme coin + compute):** [`docs/plan/`](plan/README.md)

This roadmap separates implemented repository assets from proposed work. A checked box means there is evidence in the repository or a reproducible test result—not merely an idea.

## Track A — Public project foundation
- [x] Publish an honest project README with current scope and non-claims.
- [x] Add a responsive static landing page.
- [x] Publish this master roadmap.
- [x] Review page on desktop and mobile browsers (Playwright, 390/768/1440 px).
- [x] Add SVG project favicon and social-sharing metadata.
- [x] Add custom social preview artwork (`assets/og-image.png`).
- [ ] Configure production domain and HTTPS.
- [x] Add a simple privacy-respecting contact / contribution path (`/contribute`, Netlify Forms).

## Track B — Hardware validation (highest priority)
- [ ] Record Ubuntu release: `lsb_release -a`.
- [ ] Record kernel: `uname -r`.
- [ ] Capture PCI IDs and bound drivers: `lspci -nnk | grep -A3 -E 'VGA|Display'`.
- [ ] Capture Vulkan devices: `vulkaninfo --summary`.
- [ ] Confirm `/dev/dri` permissions.
- [ ] Confirm both RX 470 cards enumerate independently.
- [ ] Run a minimal Vulkan compute smoke test on GPU 0.
- [ ] Repeat on GPU 1.
- [ ] Record temperature, power, VRAM use, throughput, errors, and stability.
- [ ] Document exact working driver/runtime versions.

## Track C — Single-node inference
- [ ] Select a small, reproducible workload that fits the 4 GB card.
- [ ] Pin model files and cryptographic hashes. *(Registry and `pin` tool built; first model listed but not yet downloaded or pinned.)*
- [ ] Build or obtain a compatible llama.cpp Vulkan runtime (only after hardware validation).
- [ ] Run inference separately on each GPU.
- [ ] Record model, quantization, prompt/input, output, timing, and resource use.
- [ ] Define expected output and task-specific acceptance criteria.

## Track D — Coordinator and workers
- [x] Define job and receipt schemas (Pydantic models; versioning still to add).
- [ ] Implement worker identity and authenticated registration.
- [x] Implement coordinator job lifecycle and deadlines (SQLite state machine).
- [x] Implement worker polling / job leasing / retries.
- [ ] Add structured logs and health reporting.
- [x] Add idempotent receipt submission.
- [x] Test coordinator restart (jobs persist) and silent-worker recovery (lease timeout → requeue).

## Track E — Verification and accounting
- [ ] Implement schema, input hash, model hash, and job-version checks.
- [x] Reject duplicate and expired submissions.
- [ ] Define deterministic task checks and tolerances for nondeterministic tasks. *(Verdict confidence levels in place: `exact` vs `schema-only`.)*
- [ ] Add independent re-run / spot-audit flow.
- [x] Record queued, leased, accepted, rejected, expired and failed results with an audit trail (disputes not yet).
- [x] Keep rewards simulated and off-chain.
- [ ] Export an auditable reward report.
- [ ] Document collusion, fabrication, and verification limitations.

## Track F — Two-GPU proof of concept
- [ ] Run two independent workers on the two RX 470 cards.
- [ ] Demonstrate coordinator dispatch to both workers.
- [ ] Demonstrate accepted and rejected receipts.
- [ ] Test duplicate, malformed, expired, and replayed submissions.
- [ ] Compare throughput and result quality.
- [ ] Measure CPU, RAM, VRAM, thermal, and power behavior.
- [ ] Publish reproducible setup and benchmark results.

## Track G — Optional settlement research (blocked until A–F)
- [ ] Publish reward allocation, emission, scoring, dispute, and cutoff policy.
- [ ] Review legal, tax, security, and operational implications.
- [ ] Specify capped test token and claim/distributor design.
- [ ] Write automated contract tests.
- [ ] Obtain independent security review.
- [ ] Test on a public testnet.
- [ ] Make a separate explicit decision before any mainnet deployment.

## Deployment checklist
- [ ] Deploy root `index.html` using Netlify or GitHub Pages.
- [ ] Verify HTTPS, canonical URL, mobile layout, and outbound links.
- [ ] Add a custom domain only after DNS ownership and target are confirmed.
- [ ] Add deployment status to README after the site is actually live.

## Guardrails
- Do not describe the project as Ethereum mining or proof-of-useful-work consensus.
- Do not claim GPU compatibility until the actual cards pass tests.
- Do not show fake live network statistics.
- Do not promise profitability, investment returns, payouts, or token value.
- Do not deploy a token or accept funds as part of the initial prototype.
- Treat worker receipts and hashes as records, not proof of GPU execution.

## Immediate next action
Run the hardware inventory commands on the actual Ubuntu host and save the unedited output (remove usernames, hostnames, and other private details first if desired). Use those results to choose the smallest viable Vulkan inference smoke test.
