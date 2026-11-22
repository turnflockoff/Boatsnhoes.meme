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
- [x] Define job and receipt schemas. *(Pydantic models, versioned `bnh-job/1` / `bnh-receipt/1` since 0.5.10.)*
- [x] Implement worker identity and authenticated registration. *(One-time operator enrollment codes, per-worker bearer keys stored as sha256, owner labels that keep one person's rigs from auditing each other, revocation. Software only; `prototype/tests/test_worker_auth.py`.)*
- [x] Implement coordinator job lifecycle and deadlines (SQLite state machine).
- [x] Implement worker polling / job leasing / retries.
- [ ] Add structured logs and health reporting.
- [x] Add idempotent receipt submission.
- [x] Test coordinator restart (jobs persist) and silent-worker recovery (lease timeout → requeue).

## Track E — Verification and accounting
- [x] Implement schema, input hash, model hash, and job-version checks. *(Signed `schema_version` and `model_sha256` on jobs; receipts must echo matching versions, input hash and model hash or get `422`. Catches mistakes, not liars. 8 tests, software only.)*
- [x] Reject duplicate and expired submissions.
- [x] Define deterministic task checks and tolerances for nondeterministic tasks. *(`exact` recompute for SHA; AI text must match after collapsing whitespace. Tolerance not yet tested against real GPU output.)*
- [x] Add independent re-run / spot-audit flow. *(Hidden re-runs by a different worker at `AUDIT_RATE`, plus hidden known-answer challenge jobs. Tested in software only.)*
- [x] Add a tie-breaker re-run when an audit disagrees. *(Third worker, excluded from both sides; majority of three. Software only.)*
- [x] Record queued, leased, accepted, rejected, expired and failed results with an audit trail, plus audit outcomes (confirmed, disputed, held).
- [x] Keep rewards simulated and off-chain.
- [x] Export an auditable reward report. *(`python -m app.rewards report` / `verify`: versioned scoring `sim-points-v1`, per-worker points, sha256 over canonical JSON. Simulated points only.)*
- [x] Document collusion, fabrication, and verification limitations. *([`VERIFICATION-THREAT-MODEL.md`](VERIFICATION-THREAT-MODEL.md))*
- [x] Assign audits to a randomly chosen eligible worker instead of first-come. *(Audits and tie-breakers go to one random active, eligible worker; re-assigned if it doesn't show up. 9 tests, software only.)*
- [x] Sign job manifests asymmetrically so workers can't forge jobs. *(Coordinator Ed25519 key; workers verify with a pinned public key. 7 tests, software only.)*

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
