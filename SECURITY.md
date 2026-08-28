# Security policy

Report vulnerabilities privately through GitHub Security Advisories for this
repository. Do not include secrets, customer prompts, generated assets, or
working exploit payloads in public issues.

Supported code is the current `main` branch. Security-sensitive changes must
include a regression test and must preserve these boundaries:

- email ownership is verified before an account session is issued;
- sessions, pins, chats, projects, and library assets are scoped to an owner;
- filesystem paths are derived only from validated identifiers;
- the web process never fetches caller-selected remote URLs;
- shared provider credentials are disabled by default;
- pull requests run checks but never publish or deploy artifacts.

Rotate a potentially exposed credential immediately, preserve relevant logs,
and use the deployment rollback procedure in `docs/PRODUCTION.md`.
