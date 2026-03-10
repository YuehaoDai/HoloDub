# Release Runbook

## Pre-release checklist

- run `go test ./...`
- run `python .\\tests\\quality\\run_regression.py ...` on approved samples
- validate `docker compose config`
- validate staging stack using `docker-compose.staging.yml`
- back up PostgreSQL and `data/`

## Deploy

1. Build tagged images.
2. Apply database migrations.
3. Deploy to staging.
4. Run smoke checks and quality regression.
5. Promote the same image digest to production.

## Rollback

1. Stop new job intake.
2. Roll back to the previous image digest.
3. Restore the previous `.env` and model manifest if required.
4. If schema changed incompatibly, restore database backup.
5. Re-run smoke checks.

## Required artifacts

- build log
- image digest list
- regression report
- backup confirmation
- rollback target version
