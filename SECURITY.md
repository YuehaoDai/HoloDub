# Security Policy

We take the security of HoloDub seriously. Thank you for helping make it
safer for everyone.

## Supported versions

HoloDub is in active development. Security fixes are applied to the latest
`main` branch. We do not yet maintain long-term support branches.

| Version          | Supported          |
| ---------------- | ------------------ |
| `main` / latest  | :white_check_mark: |
| older releases   | :x:                |

## Reporting a vulnerability

**Please do not open public GitHub issues for security problems.**

Use one of the following private channels:

1. **GitHub Security Advisory** (preferred):
   <https://github.com/YuehaoDai/HoloDub/security/advisories/new>
2. Email the maintainer at the address listed in the project README.

Please include:

- A description of the vulnerability and the affected version / commit.
- Step-by-step reproduction (Docker Compose config, requests, sample input).
- Impact assessment (what an attacker could achieve).
- Optional: suggested fix or mitigation.

## What to expect

- We will acknowledge your report within **3 business days**.
- We aim to provide an initial assessment (severity, affected components)
  within **7 days**.
- For confirmed vulnerabilities, we will work with you on a coordinated
  disclosure timeline (typically up to 90 days, shorter if exploitation
  in the wild is observed).
- We are happy to credit reporters in release notes unless you prefer to
  remain anonymous.

## Scope

In-scope components:

- Go control plane (`cmd/`, `internal/`)
- Python ML service (`ml_service/`)
- Web UI (`ui/`) and the embedded static bundle
- Docker images shipped from this repository

Out of scope (please report upstream instead):

- Vulnerabilities in third-party dependencies (PyTorch, IndexTTS2,
  Faster-Whisper, Pyannote, FFmpeg, GORM, etc.).
- Default Docker Compose credentials (`postgres:postgres`) — these are
  intentionally weak and **must be changed for any deployment that is
  reachable beyond `localhost`**. See the production deployment section
  in `README.md` and `.env.production.example`.

## Hardening tips

- Always set a strong `API_AUTH_TOKEN` for any deployment exposed beyond
  `localhost`. The middleware refuses requests without a token in
  production environments (`APP_ENV=production`).
- Restrict the `8080` port at the firewall or behind a reverse proxy
  (Caddy / Nginx) with TLS.
- Avoid storing model checkpoints from untrusted sources under
  `DATA_ROOT`; the data plane treats them as trusted input.
