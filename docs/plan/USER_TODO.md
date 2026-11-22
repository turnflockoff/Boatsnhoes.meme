# YOUR Personal To-Do List (things only you can do)
**Version:** 0.5 | **Last updated:** 2026-11-21
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
- [ ] Decide: chain path, final supply, fair launch (no premine)
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
