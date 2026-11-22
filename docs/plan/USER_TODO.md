# YOUR Personal To-Do List (things only you can do)
**Version:** 0.5.24 | **Last updated:** 2026-11-22
Never share passwords, API keys, or recovery phrases with anyone, including Claude.

## This week
- [ ] ~~Cloudflare Pages upload~~ superseded 2026-11-21: hosting is Netlify
- [x] Merge the GitHub pull requests into main (#2, #3)
- [x] Netlify linked to GitHub (branch `main`, auto-deploys on merge)
- [x] Domain live: `boatsnhoes.meme` primary, `www` redirects. Cloudflare DNS: `@` CNAME → apex-loadbalancer.netlify.com, `www` CNAME → boatsnhoes-dik6.netlify.app, both DNS only (grey cloud). Unused Netlify DNS zone deleted
- [x] HTTPS certificate requested (Let's Encrypt via Netlify)
- [ ] Optional: block email spoofing with Cloudflare TXT records `@` = `v=spf1 -all` and `_dmarc` = `v=DMARC1; p=reject;`
- [ ] Netlify: Forms > `gpu-testers` > add an email notification to your Netlify account email
- [ ] Upload mine.html, tokenomics.html, stay-safe.html (not on GitHub yet)
- [ ] Turn on hardware-key or authenticator 2FA for Netlify, GitHub, your registrar, email, and every social account
- [ ] Lock the domain at the registrar and enable auto-renew
- [ ] Registrar: enable DNSSEC if supported. (Cloudflare WAF/Bot Fight only if you later put Cloudflare DNS in front)
- [ ] Check availability of agentwiki domains and register early if you want it (also search for existing trademarks)
- [ ] Register the same handle on X, Telegram, Discord, GitHub; pin official links

## Before the testnet
- [ ] On the RX 470 rig (Ubuntu): from `prototype/`, run `python -m app.hw_inventory --out inventory.json`. No sudo needed; it only reads. Look over the file (hostname and username are already blanked) and share it or commit it so the next step (a Vulkan smoke test on each card) can be planned. If it says a tool is missing, installing `pciutils` or `vulkan-tools` is up to you
- [ ] Decide: chain path, final supply, fair launch (no premine)
- [ ] Decide: should a rig caught cheating lose its other unchecked points for that period? (simulated scoring v2; see TASKS.md)
- [ ] When the RX 470 rig is ready: set your own `OPERATOR_TOKEN` in `prototype/.env` (never share it; you now also need it, or a `SUBMITTER_TOKEN`, to queue jobs), then make one enrollment code per card with `python -m app.workers enroll --owner owner-gpu0` (and `owner-gpu1`). Keep the codes and the saved `.key` files private
- [ ] Decide: if a rig keeps dropping audits it was picked for (the operator health page warns after 3), should anything happen automatically, like checking its own work more often? Today it's only a warning, because a rig that went offline looks the same as one dodging checks
- [ ] Optional, when rigs are running: `python -m app.review` lists held results that need a person (side by side). It changes nothing. Decide, together with the scoring v2 question above, what your verdict on a case should do to its points
- [ ] Before you give a program the `SUBMITTER_TOKEN`: pick fixed points per job type and put them in `prototype/.env`, e.g. `POINTS_TABLE=demo:sha256=1,llm:generate=10` (whole numbers 0-1000, every job type listed). Then nobody, you included, can queue a job worth more than that. Simulated points only
- [ ] Decide: should rigs caught cheating stop being picked to check other rigs' work? Setting `AUDITOR_PICK=trusted-first` in `prototype/.env` does that (they're only used when nobody else is around). Downside: checking work pays points, so trusted rigs earn a bigger share of those. Default stays `uniform` until you choose
- [ ] Before rigs go online: plug in a USB stick and run `python -m app.key_rotation make-handover-key --out <stick>/bnh-handover.key` from `prototype/`. Put the printed `COORDINATOR_HANDOVER_PUBLIC_KEY=...` line in `prototype/.env`, restart the coordinator, then unplug the stick and put it somewhere safe. You only need it again to change the signing key. Losing it means re-pinning every rig by hand, so a second copy on another stick (kept elsewhere) is a good idea
- [ ] Decide where signed reward reports and ledger copies get posted (for example a `reports/` page on boatsnhoes.meme, or GitHub releases) and how long to wait after a period closes before sharing a copy
- [ ] Find a blockchain developer or team to build the chain and miner
- [ ] Set up a project email and a shared password manager
- [ ] Back up recovery info offline (paper, in two places)
- [ ] Set up multiple release signers (never one person)
- [ ] Budget for an independent code audit and a bug bounty

## Before launch
- [ ] Hire a crypto and gambling lawyer; choose jurisdiction
- [ ] Decide KYC/AML and age verification provider
- [x] Waitlist planned: Netlify Forms on /contribute
- [ ] Prepare moderators and rules for community channels
- [ ] Choose cold-storage method for casino funds
- [ ] Write an incident plan: who you call and what you post if something breaks
