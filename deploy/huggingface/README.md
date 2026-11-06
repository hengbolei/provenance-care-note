---
title: Provenance Care Note
emoji: 🩺
colorFrom: green
colorTo: gray
sdk: docker
app_port: 8000
pinned: false
license: mit
short_description: Citation-checked care review agent on synthetic data
---

# Provenance Care Note: public demo

A provenance-first care record with a clinical review agent. Every claim the agent drafts cites
an exact span of the record and passes deterministic verification before a clinician decides.

**Open the demo in its own tab** (signing in does not work inside the embedded view on this
page): use the "Open in new tab" link, or the direct `*.hf.space` address of this Space.

- Sign in as `clinician` with password `clinician-demo-2026`, then click **Prepare review**.
- Synthetic data only. Everyone shares this demo and it resets every 30 minutes, so never enter
  real information. This is a research prototype, not a medical device.
- The agent replays recorded drafts, so the demo makes no model calls.

Source, evaluation results and documentation:
[github.com/hengbolei/provenance-care-note](https://github.com/hengbolei/provenance-care-note)
