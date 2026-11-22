# BOATS N HOES: Project Task List (Claude + dev work)
**Version:** 0.5.7 | **Last updated:** 2026-11-22 | Personal to-dos live in USER_TODO.md.

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
- [ ] Random (not first-come) auditor assignment (threat model #4)
- [x] GPU worker Phase 5: simulated reward reports (off-chain points, versioned scoring `sim-points-v1`, reproducible sha256, `verify` tool; 61 tests)
- [ ] Decide a penalty rule for caught cheaters (scoring v2; owner decision)
- [x] Authenticated worker registration / per-worker keys: one-time operator codes, per-worker keys stored as hashes, same-owner workers never audit each other, revocation (73 tests)
- [ ] GPU worker Phase 3: RX 470 hardware validation (needs the real rig)
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
