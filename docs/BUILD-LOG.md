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
