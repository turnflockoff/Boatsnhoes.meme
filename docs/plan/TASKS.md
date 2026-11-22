# BOATS N HOES: Project Task List (Claude + dev work)
**Version:** 0.5.19 | **Last updated:** 2026-11-22 | Personal to-dos live in USER_TODO.md.

## Phase 0: Foundation
- [x] Buy boatsnhoes.meme
- [x] Super prompts, README, task lists
- [x] Project folder/repo for code and assets (GitHub: turnflockoff/Boatsnhoes.meme)
- [ ] Decide chain path
- [ ] Final ticker check on explorers/trackers

## Phase 1: Legal and Compliance
- [ ] Choose gambling jurisdiction and license route
- [ ] Hire crypto/gambling lawyer
- [ ] KYC/AML and age verification plan
- [ ] Securities-risk review and disclaimers
- [ ] AgentWiki content liability and moderation policy

## Phase 2: Tokenomics
- [x] Draft model page (sketch pad)
- [ ] Lock: 60s blocks, 3.5-year halving, ~100M cap, reward 27
- [ ] Fair launch vs premine final
- [ ] Tail emission decision

## Phase 3: Chain and Mining
- [x] GPU worker backend, Phase 1: durable SQLite job queue, leases and retries, signed manifests verified by workers, audit log (13 tests passing)
- [x] GPU worker Phase 2: pluggable task executors, `llm:generate` task, pinned model registry with licenses, opt-in download (32 tests)
- [ ] Pin the first model (download on the GPU box, then `model_registry pin`)
- [x] GPU worker Phase 4 (first slice): hidden re-run audits, hidden known-answer challenges, tolerance rule, worker trust report (44 tests)
- [x] GPU worker Phase 4 (rest): tie-breaker re-run for disputes, written fraud/collusion threat model (`docs/VERIFICATION-THREAT-MODEL.md`, 51 tests)
- [x] Random (not first-come) auditor assignment (threat model #4): one random active, eligible worker per audit/tie-breaker, re-assigned if silent (82 tests)
- [x] GPU worker Phase 5: simulated reward reports (off-chain points, versioned scoring `sim-points-v1`, reproducible sha256, `verify` tool; 61 tests)
- [ ] Decide a penalty rule for caught cheaters (scoring v2; owner decision)
- [x] Authenticated worker registration / per-worker keys: one-time operator codes, per-worker keys stored as hashes, same-owner workers never audit each other, revocation (73 tests)
- [x] Asymmetric job signatures (threat model #9): coordinator signs with an Ed25519 private key, workers verify with a pinned public key, no shared secret (89 tests)
- [x] Versioned job/receipt schemas: signed job version and pinned model hash, receipts must match version, input hash and model hash (97 tests)
- [x] Signed reward reports (threat model #10, first step): coordinator signs closed-period reports with its Ed25519 key, anyone can check offline with the public key (109 tests)
- [x] Structured logs and health reporting: one JSON line per event, no secrets or prompts in logs, request ids, public `/health` (503 when the database fails), operator `/v1/operator/health` with plain-language warnings (127 tests)
- [x] Shareable ledger copies: redacted copy of a closed, settled period (no keys, codes, prompts, answers or real owner names) that anyone can re-run a signed report and the consistency checks against (137 tests)
- [ ] Post signed reports and ledger copies in a fixed public place (owner decision: where)
- [x] Read-only rig inventory tool for Phase 3: `python -m app.hw_inventory` (OS, kernel, PCI IDs, bound driver, Vulkan devices/Mesa version, /dev/dri access, redacted JSON; 151 tests, format samples only)
- [x] Trust-based audit rate (threat model #1, `audit-tier-v1`): new rigs and rigs caught once (failed hidden test or outvoted) are re-checked more often; operator-only, changes checks not points (158 tests)
- [x] Planned coordinator key rotation (threat model #9 follow-up): `python -m app.key_rotation rotate`, handover signed by old and new key, pinned workers follow a valid chain, queued jobs re-signed on claim, crash-safe (168 tests)
- [x] Audit follow-through (threat model #4 follow-up): per-rig count of assigned audits/tie-breakers it never picked up, let time out, or had refused; operator health warns at `AUDIT_DROP_WARN`; operator-only, changes nothing about points (177 tests)
- [ ] Decide what repeated audit drops should change, if anything (owner decision; today it's a warning only)
- [x] Operator review queue (threat model #7): read-only list of held cases (stuck disputes, three-way disagreements, failed hidden tests) with answers side by side, `python -m app.review`, health warning (190 tests)
- [ ] Decide what a reviewer's decision should do to held points (part of scoring v2; owner decision)
- [x] Close open job creation and limit requests (threat model #16, #17): `POST /v1/jobs` needs the operator or a job-only submitter token; per-address request limit with `429`/`Retry-After`; lock-out after repeated wrong keys, tokens or codes; addresses kept out of logs (210 tests)
- [ ] GPU worker Phase 3: RX 470 hardware validation (needs the real rig; start by running the inventory tool there)
- [ ] Memory-hard, ASIC-resistant algorithm (pseudocode, reference miner)
- [ ] Per-block difficulty adjustment
- [ ] Testnet, then public testnet
- [ ] Checkpoint/finality design against 51% attacks
- [ ] Hashrate monitoring dashboard
- [ ] Code audit and bug bounty

## Phase 4: Security (see README SECURITY)
- [x] Stay Safe page built
- [x] `_headers` security file built
- [x] `_headers` added to repo; CSP verified locally against all pages
- [ ] Verify `_headers` on the live Netlify URL
- [ ] Signed miner releases with checksums (multi-signer)
- [ ] Many seed nodes in different regions
- [ ] Node peer limits and rate limits
- [ ] Public status page
- [ ] Mining fairness review: pool distribution, botnet/insider checks
- [ ] Casino: cold storage, withdrawal limits/delays, confirmations by deposit size, provably fair audit
- [ ] Incident response plan (who does what if hacked)

## Phase 5: Wallet, Casino, AgentWiki
- [ ] Wallet and explorer
- [ ] Deposit/withdrawal flow
- [ ] Provably fair games and casino MVP
- [ ] Responsible-gambling tools and self-exclusion
- [ ] AgentWiki design: storage proofs, spam fees, moderation

## Phase 6: Brand and Web
- [x] Home v1, Mine, Tokenomics, Stay Safe (drafts)
- [x] Home v2: FAQ, share card (community links pending handles)
- [x] Custom graphics: logo mark, favicon, boat art, OG share image
- [x] Contribute a GPU page (browser check, Linux check, tester form)
- [x] Desktop/mobile testing (390, 768, 1440 px)
- [x] Subpages restyled to match the orange/black homepage
- [ ] Rebuild or upload Mine, Tokenomics, Stay Safe pages
- [ ] Litepaper, Casino coming-soon, Meme Lab
- [x] Waitlist backend: Netlify Forms (`gpu-testers`)
- [x] Netlify project created (`boatsnhoes-dik6`), form detection on
- [x] Link Netlify to GitHub and confirm live deploy
- [x] Point boatsnhoes.meme DNS at Netlify (Cloudflare CNAMEs, DNS only)
- [ ] Verify HTTPS, security headers and share card on the live URL

## Phase 7: Launch
- [ ] Mainnet plan, miner onboarding, listings outreach
- [ ] Community campaign, post-launch monitoring
