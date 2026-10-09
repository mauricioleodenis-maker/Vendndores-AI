# Revisión Haiku: leads-data

Findings written to /home/user/Vendndores-AI/docs/revision/haiku-reminders-worker.md (11 items, ordered by severity; no code edited).
Top issues: duplicate WhatsApp sends via stale-lock reclaim and send-before-commit (service.py:381-396, 274-277); reminders sent with no consent record (service.py:104-118, scheduler.py:234-238, verify legal basis); no reminders UI (templates/reminders/ holds only .gitkeep).
Also flagged: wrong relative time copy after quiet-hour postponement, silent localhost Redis fallback in worker.py:112, and missing tests for these paths.
