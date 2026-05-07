# Database migrations

The control plane currently uses GORM's `AutoMigrate` (called from
`cmd/api`/`cmd/worker` when `AUTO_MIGRATE_ON_START=true`). The SQL files
in this folder document the **logical** migration history so that:

- production operators can review schema changes per release;
- a future switch to a versioned migration tool ([golang-migrate], [goose])
  has a starting point;
- `migrate ... down` style rollbacks have a paper trail even though
  AutoMigrate cannot perform them.

## File naming

```
NNN_short_description.sql       -- forward migration
NNN_short_description.down.sql  -- (optional) rollback
```

`NNN` is a zero-padded sequence number, never reused. Out-of-order numbers
are reserved for hot-fix branches and merged in chronological order during
release.

## Numbering history

| #   | File                                  | Notes                                             |
| --- | ------------------------------------- | ------------------------------------------------- |
| 000 | `000_initial.sql`                     | Baseline schema as of v0.1 (auto-generated reference; AutoMigrate already creates these tables). |
| 001 | reserved                              |                                                   |
| 002 | reserved                              |                                                   |
| 003 | `003_segment_review.sql`              | Adds `segment_suggestions` + `jobs.translation_summary`. |

> **Heads-up:** `AutoMigrate` does NOT honour these files; it derives the
> schema from the GORM struct tags in `internal/models`. The files exist
> for documentation and future tooling.

## Adopting a versioned migration tool

When we move off AutoMigrate, the recommended path is:

1. Add `github.com/pressly/goose/v3` as a dev dependency.
2. Generate a baseline file `migrations/20260507000000_baseline.sql` by
   dumping the current schema (`pg_dump --schema-only`).
3. Replace `s.db.AutoMigrate(...)` in `internal/store/store.go` with a
   `goose.Up` call that uses an embedded migration FS.
4. Disable `AUTO_MIGRATE_ON_START` in production, run `goose up` from a
   one-shot Job (CI or a dedicated sidecar).

Tracked under issue: _Phase 2 follow-up — adopt golang-migrate/goose._

[golang-migrate]: https://github.com/golang-migrate/migrate
[goose]: https://github.com/pressly/goose
