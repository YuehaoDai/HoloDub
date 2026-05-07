<!-- Thanks for contributing to HoloDub! Please fill out the template below. -->

## Summary

<!-- One or two sentences describing what this PR changes and why. -->

## Type of change

- [ ] Bug fix (non-breaking)
- [ ] New feature (non-breaking)
- [ ] Refactor (no behaviour change)
- [ ] Breaking change
- [ ] Documentation only
- [ ] CI / tooling

## Pipeline impact

<!-- If this touches Go backend code (internal/pipeline, internal/store, internal/models),
remember the dual-binary hot-update rule:
both holodub-api-1 and holodub-worker-1 must be rebuilt and redeployed together. -->

- [ ] Touches Go backend (api + worker must both be rebuilt)
- [ ] Touches ml-service (requires image rebuild or hf-cache reload)
- [ ] Touches UI (requires `npm run build` + api binary rebuild because of go:embed)
- [ ] Database schema change (added migration)
- [ ] Stage order or status enum changed (potentially breaking for in-flight jobs)

## Testing

- [ ] Added or updated unit tests
- [ ] Manually validated end-to-end with a smoke job
- [ ] Manually validated with a real (GPU) pipeline run

```
# Reproduction steps / commands used to validate
```

## Checklist

- [ ] Code is formatted (`gofmt`, `ruff format`, `prettier`)
- [ ] Lints pass locally (`golangci-lint run`, `ruff check`, `npm run lint`)
- [ ] No new linter warnings introduced
- [ ] Secrets / API keys / .env values are not committed
- [ ] Updated relevant docs (README, .env.example, AGENTS docs)
