# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| latest  | Yes       |
| < latest | No — please upgrade |

## Reporting a Vulnerability

If you discover a security vulnerability in NeverDry, **please do not open a public issue.**

Instead, report it privately:

1. **GitHub Security Advisories** (preferred): Go to the [Security tab](https://github.com/never-dry/NeverDry/security/advisories) and click **"Report a vulnerability"**
2. **Email**: Send details to the repository owner via the email listed on the [GitHub profile](https://github.com/drake69)

### What to include

- Description of the vulnerability
- Steps to reproduce
- Affected version(s)
- Potential impact
- Suggested fix (if any)

### Response timeline

| Step | Timeline |
|------|----------|
| Acknowledgment | Within 48 hours |
| Initial assessment | Within 7 days |
| Patch release | Within 30 days (critical: within 7 days) |
| Public disclosure | After patch is released |

## Scope

The following are **in scope**:

- Code in `custom_components/never_dry/`
- GitHub Actions workflows (`.github/workflows/`)
- Configuration handling (config_flow, options_flow)
- Service call handling (irrigate, stop, reset)
- Sensor state processing

The following are **out of scope**:

- Home Assistant core vulnerabilities (report to [HA security](https://www.home-assistant.io/security/))
- Third-party hardware/firmware issues
- Social engineering attacks
- Denial of service via excessive automation triggers (this is an HA-level concern)

## Security Measures

NeverDry employs the following security practices:

- **Zero runtime dependencies** — no third-party supply chain risk
- **Static analysis** — Bandit, CodeQL, and custom forbidden pattern guards run on every PR
- **Input validation** — all config flow inputs are validated with voluptuous schemas and runtime bounds:
  - Max 50 zones per instance (`MAX_ZONES`)
  - Zone names max 64 characters (`MAX_ZONE_NAME_LENGTH`)
  - Numeric parameters bounded (e.g., alpha 0.05–1.0, t_base -5–20, efficiency 0.1–1.0)
  - Entity selectors domain-constrained (sensor, switch)
- **No dangerous functions** — `eval()`, `exec()`, `subprocess`, `pickle`, `__import__()` are forbidden and CI-blocked
- **Service rate limiting** — irrigation services throttled to min 10-second intervals; emergency stop is never throttled
- **Ruff linting** — enforced in CI with bandit security rules (S) enabled
- **Dependabot** — automatic updates for GitHub Actions versions
- **Minimal permissions** — workflows use least-privilege RBAC

## Acknowledgments

We appreciate responsible disclosure. Security researchers who report valid vulnerabilities will be credited in the release notes (unless they prefer anonymity).
