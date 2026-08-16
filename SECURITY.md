# Security Policy

PortfoliFLOW is software for regulated institutional investors. Security
reports are taken seriously and handled by the maintainer personally.

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Report privately by e-mail to
**ProcessReengineer@happycomputercollective.org** with the subject line
`[SECURITY] PortfoliFLOW`. Include:

- a description of the issue and its impact,
- the affected area/module and, where known, the file or route,
- reproduction steps or a proof of concept,
- the migration revision and `BUILD_SHA` shown in the application footer,
- whether you would like to be credited.

You will receive an acknowledgement within **5 working days**. The project
is single-maintainer, so a fix may take longer than in a staffed project;
you will be kept informed of progress and asked to agree a disclosure date
before anything is published.

## Scope

In scope: this repository's code, migrations, configuration templates and
documentation. Of particular interest are tenant-isolation bypasses (row-level
security, `tenant_context`), authentication and session handling, credential
vault handling, the tool-trust gating of the AI assistant, and any path by
which uploaded portfolio data could leave its tenant.

Out of scope: vulnerabilities in third-party dependencies that are already
public (please report those upstream and, if you like, tell us too), and
issues that require an already-compromised operator account or host.

## Supported versions

Until the first tagged release, only the `main` branch is supported. Fixes
land on `main`; there are no backports.

## Coordinated disclosure

Once a fix is on `main`, a short advisory is published as a GitHub Security
Advisory on the repository, crediting the reporter unless they prefer
otherwise. Please give the maintainer the chance to release a fix before
public disclosure.
