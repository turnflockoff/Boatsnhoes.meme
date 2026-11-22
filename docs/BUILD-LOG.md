# Build log: proof of work and what you learned

## 2026-11-21: Merged site, graphics, contributor page, Netlify

**Shipped**
- Merged the $BNH meme-coin brand (v0.4 plan) with the GitHub GPU-compute repo into one site.
- Original graphics: hoe-mast boat with a $BNH sail, GPU-chip jib, and GPU-fan portholes (`assets/boat.svg`), logo mark/favicon (`assets/mark.svg`), 1200×630 share card (`assets/og-image.png`).
- `/contribute`: a 10-second in-browser GPU check (WebGPU + WebGL), copy-ready Linux hardware commands, and a tester sign-up form (Netlify Forms, honeypot spam trap).
- Tested at phone (390 px), tablet (768 px) and desktop (1440 px) with the real fonts and the `_headers` security policy applied. Found and fixed 2 bugs: sideways scrolling on phones, and the "N" in the headline drifting off-center.
- Netlify project `boatsnhoes-dik6` created, form detection on. Internal folders (`/docs`, `/prototype`, `/prompts`) blocked from the public site.

**What you learned**
1. **Static site + forms without a server.** Netlify scans your HTML at deploy time. Any `<form data-netlify="true">` becomes a working form, and submissions show up in the Netlify dashboard. No backend code is needed.
2. **The `_headers` file works on Netlify and Cloudflare.** Both read the same format. Your Content-Security-Policy only lets the page load fonts from Google and scripts from your own domain. That is why the GPU checker lives in `assets/contribute.js` instead of a third-party script.
3. **Social share cards.** The `og:image` meta tag sets the picture people see when they paste your link in Facebook, X or Discord. It must be a full `https://` URL to a PNG or JPG, ideally 1200×630 px. SVG images don't show up.
4. **Browsers can only half-see your GPU.** WebGL and WebGPU report a GPU name, but browsers blur it for privacy. That is why the real check is the Linux `vulkaninfo` command.
5. **Rotated text can break phones.** A rotated full-width block pokes past the screen edge and lets the page scroll sideways. The fix is to shrink each block to its text width (`width: fit-content`).

## 2026-11-21 (later): Homepage back to orange/black

- You picked version A (the orbital design) from a side-by-side of all 4 homepages in the repo history and your upload.
- Restored it from git history instead of rebuilding. Changed only: footer links (they pointed at folders the site hides), a link to the GPU contributor page, an orange share image, and a phone-only icon overlap.
- **What you learned:** every version of a file lives in git history. `git show <commit>:index.html` brings back any past version without redoing the work.

## 2026-11-21 (evening): Matching pages + durable GPU job queue

**Shipped**
- /contribute, /thanks and 404 now use the same orange/black look as the homepage. The orange boat logo is also the browser-tab icon.
- GPU worker backend, Phase 1: jobs live in SQLite and survive restarts. Leases time out and requeue, retries are capped, and every step goes to an audit log. Workers refuse jobs whose signature doesn't check out.
- Tests went from 4 to 13, all passing. I also ran a real end-to-end test: coordinator, two workers, a coordinator restart mid-run, and a worker using the wrong key.

**What you learned**
1. **A lease is a timed lock.** A worker "borrows" a job for 60 seconds. If the rig crashes or goes offline, the job returns to the queue for another rig. The coordinator never waits forever on a dead machine.
2. **Idempotent means safe to repeat.** If a rig's Wi-Fi drops and it sends the same result twice, it's paid once and gets the same receipt both times.
3. **Signatures protect the miners.** Each job is signed with a secret key. A worker checks the signature before running anything, so a fake coordinator can't make your GPU run its code.
4. **Test the bad days, not just the good one.** Most of the new tests cover failures: crashes, timeouts, cheaters, duplicates, and 20 workers grabbing jobs at the same moment.

## 2026-11-22: GPU worker Phase 2 (job types + pinned AI models)

**Shipped**
- A plug-in system for job types. Each type knows how to check its input, run, and grade its results.
- The first real AI job type, `llm:generate`: short text generation on a small model through llama.cpp, the software that will drive the RX 470s.
- A model registry that records where each model comes from, its license and its size. A model can't be used until its exact fingerprint (sha256) is pinned from a real download. Nothing downloads unless you add `--yes-download`.
- 32 tests pass, up from 13. An hourly scheduled build session now continues this work and saves to your drafts branch.

**What you learned**
1. **A fingerprint (hash) proves a file is exactly the one you meant.** Change one byte of a 400 MB model and the sha256 changes completely, so a worker can detect a swapped or corrupted model before running it.
2. **Not every check proves the same thing.** For the SHA job the coordinator redoes the math, which is exact proof. For AI text it can only check that the answer looks right, which is "schema-only". Real proof needs another rig to re-run the same job and compare (Phase 4).
3. **Licenses matter before launch.** The model we picked (Qwen2.5-0.5B) is Apache-2.0, which allows commercial use. That's recorded right next to it in the registry.

## 2026-11-22 (later): GPU worker Phase 4, first slice (catching cheaters)

**Shipped**
- **Hidden re-runs.** When an AI result can only be checked for shape, the coordinator now quietly hands the same job to a *different* rig (25% of the time by default). If both answers match, both are marked confirmed. If they don't, both are held. A rig can never re-check its own work.
- **Hidden test jobs ("challenges").** You can slip in a job where you already know the answer. To the rig it looks exactly like normal work, and a wrong answer even looks accepted, so a cheater can't tell they were caught. You see the failure in a private trust report.
- **Points split by status:** confirmed, pending, unverified, held. The public view hides which jobs were tests.
- The private views (challenges, audits, trust report) are locked behind an operator password (`OPERATOR_TOKEN`). If you don't set one, they're switched off.
- Tests went from 32 to 44, all passing. Still software only: no real GPU has run any of this.

**What you learned**
1. **You can't prove a GPU did the work, but you can make cheating expensive.** If 1 in 4 answers gets re-checked by someone else, a rig that fakes answers gets caught fast. That's spot-checking, the same idea as a random bag check.
2. **A test only works if it's invisible.** My first version gave re-check jobs a longer deadline than normal jobs. That's a tell: a cheater could fake normal jobs and only work honestly on the long ones. I fixed it so re-checks copy the original's time window.
3. **"Close enough" needs a rule written down.** Two honest rigs can print the same AI answer with different spacing. The rule now: ignore spacing and line breaks, but any different word or capital letter counts as a mismatch. We'll adjust it once two real RX 470s produce answers.
4. **Agreement isn't proof.** If one person runs two rigs, they can agree on a fake answer. That's why per-worker identity (logins/keys) is one of the next jobs.
