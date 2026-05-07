# Contributing to HoloDub

Thanks for your interest in HoloDub! This document explains how to build
the project locally, the conventions we follow, and how to submit changes.

## Code of conduct

Be kind. Treat reviewers and contributors with respect. Discriminatory or
harassing behaviour is not tolerated.

## Project layout

```
cmd/api,worker        - Go binaries (HTTP API, background worker)
internal/             - Go control plane (config, http, pipeline, store, ...)
ml_service/           - Python FastAPI ML service (ASR / TTS / VAD / separator)
ui/                   - Vue 3 + TypeScript SPA (embedded into the api binary)
docker/               - Dockerfiles
migrations/           - SQL schema migrations
docs/                 - Operator / contributor documentation
```

The runtime is composed by Docker Compose:

| Service     | Image / source                                   |
| ----------- | ------------------------------------------------ |
| `api`       | `cmd/api` Go binary, embeds the UI bundle        |
| `worker`    | `cmd/worker` Go binary, drives the pipeline      |
| `ml-service`| Python FastAPI + PyTorch + GPU                   |
| `postgres`  | `postgres:16-alpine`                             |
| `redis`     | `redis:7-alpine`                                 |

The pipeline stages are: `media -> separate -> asr_smart -> translate -> tts_duration -> merge`,
defined in [`internal/models/models.go`](internal/models/models.go).

## Building locally

Requirements:

- Go 1.25+
- Python 3.11+
- Node.js 20+
- Docker Desktop or Docker Engine + NVIDIA Container Toolkit (for GPU)

### Smoke (no GPU, no API keys)

```bash
cp .env.example .env
docker compose up --build -d
```

### Real backends (GPU)

See `README.md` "Mode 2 — Real backends" section. You'll need a
DeepSeek/Qwen API key for translation and a GPU for ASR / TTS.

## Hot-update workflow (the dual-binary rule)

Any Go change in `internal/pipeline/`, `internal/store/`, or
`internal/models/` **must rebuild and redeploy both `api` and `worker`
binaries**. Failing to do so leaves the worker running stale code.

```powershell
$env:GOOS="linux"; $env:GOARCH="amd64"
go build -o holodub-api-linux ./cmd/api/
go build -o holodub-worker-linux ./cmd/worker/
docker cp holodub-api-linux holodub-api-1:/usr/local/bin/holodub
docker cp holodub-worker-linux holodub-worker-1:/usr/local/bin/holodub
docker compose -f docker-compose.yml restart api worker
```

UI changes additionally require `npm run build` followed by an `api`
binary rebuild because the UI is embedded via `go:embed`.

## Code style and quality

| Language | Tooling                                                    |
| -------- | ---------------------------------------------------------- |
| Go       | `gofmt`, `goimports`, `golangci-lint run`, `go test ./...` |
| Python   | `ruff check`, `ruff format`, `mypy ml_service/app`         |
| TS / Vue | `eslint`, `prettier`, `vue-tsc --noEmit`                   |

Run the CI suite locally before opening a PR:

```bash
golangci-lint run
go test -race ./...
( cd ml_service && ruff check app tests && pytest )
( cd ui && npm run lint && npm run typecheck && npm run build )
```

## Commit and branching

- The local working branch is `dev-win`; remote tracking is `origin/main`.
- We follow [Conventional Commits](https://www.conventionalcommits.org/)
  for commit messages, e.g. `feat: add SSE job status stream`,
  `fix(tts): handle empty voice profile`.
- Keep PRs focused. If your change spans multiple stages of the pipeline
  or multiple services, prefer a stack of small PRs over one giant one.

## Submitting a pull request

1. Fork the repo and create a feature branch off `main`.
2. Make your changes; add or update tests.
3. Run the lint / test commands above.
4. Open a PR against `main` and fill out the PR template.
5. Address review comments. We squash-merge by default.

## Adding a new ML adapter

When wiring in a new ASR / TTS / VAD / separator backend:

- Create the adapter under `ml_service/app/adapters/` and implement the
  same call signature as the existing siblings.
- Wire selection in `ServiceContainer` (`ml_service/app/runtime.py`)
  through the relevant `ML_*_BACKEND` environment variable.
- Update `.env.example` with the new option and any required keys.
- Add at least a unit-level smoke test under `ml_service/tests/`.

## Reporting bugs and security issues

- Functional bugs: open a GitHub issue using the bug report template.
- Security vulnerabilities: see [`SECURITY.md`](SECURITY.md). **Do not**
  disclose them in public issues.

## License

By contributing, you agree that your contributions will be licensed under
the [Apache License 2.0](LICENSE).
