# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in Model Sherpa, please report it responsibly:

- **Preferred:** Open a [GitHub Security Advisory](https://github.com/nordicnode/model-sherpa/security/advisories/new) for private disclosure.
- **Alternative:** Open a [GitHub Issue](https://github.com/nordicnode/model-sherpa/issues) and tag it with the `security` label. If the issue is sensitive, please prefix the title with `[SECURITY]` and avoid including exploit details in the public description — we'll follow up for details privately.

## Response Time

We aim to acknowledge security reports within **48 hours** and provide a substantive response within **7 days**.

## Scope

Model Sherpa is a Hermes Agent plugin that operates as middleware between the LLM and the tool registry. Security concerns within scope include:

- Bypass or failure of the privacy redaction (substring redaction of API keys, tokens, passwords)
- State file corruption or race conditions that could leak sensitive data
- Plugin code execution vulnerabilities (e.g., arbitrary code via custom hints)

Out of scope: vulnerabilities in the Hermes Agent framework itself, the host LLM, or the underlying operating system.
