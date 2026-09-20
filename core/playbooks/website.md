# Playbook: website

You are building a production-quality, visually striking website. Aim for the kind of site an agency would
put in its portfolio: a clear story, confident typography, rich graphics, tasteful motion and interaction —
never a generic template.

## Research (planning phase)
- Search for the business type, 3–5 strong competitor or reference sites, current design trends for this
  niche, and what customers expect (menus, booking, ordering, hours, location, reviews, FAQs…).
- Use fetch_url on the best references and note what works (structure, tone, features, visuals).
- Save concise notes to research/notes.md (sources + takeaways).

## The plan (submit_plan) must contain
1. **Summary** — what we are building and for whom, in 2–3 sentences.
2. **Goals & audience** — primary actions a visitor should take.
3. **Requirements checklist** — every feature (e.g. menu with categories & prices, online ordering or
   reservation form, opening hours, map/location, gallery, reviews, newsletter, contact, SEO basics,
   accessibility, mobile-first). Mark anything you assume with (assumed).
4. **Sitemap & sections** — page by page, section by section, with the content each needs.
5. **Design direction** — mood/keywords; colour palette with hex codes and roles; font pairing
   (Google Fonts); imagery & illustration style; layout ideas; **signature interactions** (e.g. hero with
   a 3D/particle or animated SVG scene, scroll-triggered reveals and parallax, magnetic buttons, hover
   micro-interactions, animated menu cards, a sticky order bar); motion principles (durations, easing).
6. **Assets** — generated SVG graphics/icons you will draw, image placeholders with descriptions
   (the user can swap in real photos later), copy you will write.
7. **Tech** — see stack below; file structure.
8. **File map** — every file (index.html, css/styles.css, js/main.js, assets/…) with one line on what it holds.
9. **Content** — the real headings, body copy, menu items/prices, testimonials and CTA text you will use.
10. **Build steps** — the ordered list you pass as `steps` (6–12), each naming its files and its acceptance
   criteria (e.g. "hero: full-viewport, GSAP intro, CTA scrolls to #order; passes contrast at 4.5:1").
9. **Open questions** — anything only the user can answer (name, real prices, address…), with the
   placeholder you will use meanwhile.

## Stack (no build tools needed)
- `index.html` (+ extra pages if the sitemap needs them), `css/styles.css`, `js/main.js`, `assets/`.
- Modern CSS: custom properties for the palette, fluid type with clamp(), grid/flex, `prefers-reduced-motion`.
- Motion: GSAP + ScrollTrigger from `https://cdn.jsdelivr.net/npm/gsap@3/dist/` ; 3D: Three.js from
  `https://cdn.jsdelivr.net/npm/three@0.160/build/three.module.js` (only where it adds real wow).
- Icons/illustrations: hand-written inline SVG. Fonts: Google Fonts `<link>`.
- Forms work client-side (validation + a friendly confirmation); note where a backend would plug in.
- Accessible: semantic landmarks, alt text, focus styles, 4.5:1 contrast, keyboard-usable menus.

## Build (executing phase)
- Work through the steps in order; `complete_step` after each.
- If delegate_coding is available, hand each substantial build step to it (give it the plan's design
  direction, the files and the exact outcome); review what it produced, then continue.
- Write real, specific copy for the business — no lorem ipsum.
- After the hero and after the full page are done, call `preview_site` with `review=true`, read the
  critique and console errors, and fix them. Repeat until the polish score is ≥ 8 or 3 review rounds.
- Finish by `open_in_browser("index.html")`, write REPORT.md (what was built, how to edit, what needs real
  content), then `finish` with a short spoken-style summary.
