# Security Policy

## Reporting a Vulnerability

The BamDude team takes security seriously. We appreciate your efforts to responsibly disclose your findings.

### How to Report

**Please DO NOT report security vulnerabilities through public GitHub issues.**

Use GitHub's private vulnerability reporting feature:
1. Go to the [Security tab](https://github.com/kainpl/bamdude/security)
2. Click "Report a vulnerability"
3. Fill out the form with details

### What to Include

Please include the following information in your report:

- **Description** of the vulnerability
- **Steps to reproduce** the issue
- **Affected versions** of BamDude
- **Potential impact** of the vulnerability
- **Any suggested fixes** (if you have them)

### What to Expect

- **Acknowledgment**: We will acknowledge receipt of your report within 48 hours
- **Assessment**: We will investigate and validate the issue within 7 days
- **Updates**: We will keep you informed of our progress
- **Resolution**: We aim to release a fix within 30 days for critical issues
- **Credit**: We will credit you in our release notes (unless you prefer to remain anonymous)

## Supported Versions

Fixes ship in the next release, not as backports.

| Version | Supported |
| ------- | --------- |
| Latest stable (`vX.Y.Z`, Docker `:latest`) | :white_check_mark: |
| Current beta (`vX.Y.ZbN`, Docker `:dev`)   | :white_check_mark: — fixed in the next beta or the stable it becomes |
| Anything older                              | :x: — upgrade first (see [UPDATING.md](UPDATING.md)) |

## Security Considerations

### Network Security

BamDude communicates with your printers over your local network using:

- **MQTT over TLS** (port 8883) - Encrypted printer communication
- **FTPS** (port 990) - Encrypted file transfers

### Recommendations

1. **Run on trusted network**: BamDude should only be accessible on your local network
2. **Use reverse proxy**: If exposing to the internet, use a reverse proxy with HTTPS
3. **Keep updated**: Always run the latest version for security patches
4. **Secure API keys**: Treat API keys like passwords; don't share them publicly
5. **Developer Mode**: Use your printer's Developer Mode access code; don't share it

### Known Security Features

- Authentication is always on: JWT with rotating refresh tokens, optional 2FA (TOTP, email OTP, backup codes) and OIDC single sign-on
- Every API endpoint is permission-checked; API keys are scoped and can never reach admin-only operations
- Login rate limiting per user and per IP
- No default credentials — the first start creates the admin account
- Local-only by default (no cloud dependency)
- TLS encryption for printer communication; strict Content-Security-Policy and security headers on the web UI

## Scope

The following are **in scope** for security reports:

- Authentication/authorization bypasses
- Remote code execution
- SQL injection
- Cross-site scripting (XSS)
- Cross-site request forgery (CSRF)
- Sensitive data exposure
- Insecure direct object references

The following are **out of scope**:

- Issues in dependencies (report to the upstream project)
- Social engineering attacks
- Physical attacks
- Denial of service (DoS) attacks
- Issues requiring physical access to the server

## Acknowledgments

We thank the following individuals for responsibly disclosing security issues:

*No security issues have been reported yet.*

---

Thank you for helping keep BamDude and its users safe!
