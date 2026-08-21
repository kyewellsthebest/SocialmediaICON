# dashboard (Phase 2 — not started)

Next.js review queue. Scope, when it gets built:

- list `GET /review/queue`, play each clip from its signed URL
- edit title / caption / hashtags → `PATCH /review/{id}`
- approve / reject → `POST /review/{id}/approve|reject`
- download approved clips for manual posting

Acceptance: run a source from the UI and export 3 approved clips without
touching the terminal.

Do not start this until Phase 1 passes its go/no-go — clips a human would
actually post, straight out of `scripts/run_pipeline.py`.
