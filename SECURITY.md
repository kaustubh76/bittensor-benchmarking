# Reporting a vulnerability

ACR handles real USDC on Arc. If you find a way to take, lock, or misprice it — or to make
the benchmark say something the estimator did not — we want to hear it privately first.

**Email:** kaushtubhagrawal45@gmail.com with the subject line `ACR security`. Include what
you found, how to reproduce it, and what you think it is worth. Encrypt if you like; a
plain report is fine.

**What to expect.** An acknowledgement within 72 hours; a fix or a mitigation plan within
14 days for anything that touches funds or the signer set; credit in the fix's commit
message if you want it. There is no bounty programme yet; when there is money on the venue
worth stealing there will be one, and this file will say so.

**In scope:** the contracts under `contracts/src/`, the seller API (`services/index_api`),
the terminal's server routes (`apps/terminal/app/api`), the MCP server, and the deploy and
operations scripts. The trust model those are held to is `docs/SECURITY.md`; the last audit
is `docs/SECURITY-AUDIT.md`.

**Out of scope:** the hackathon material under `hackathon/` (frozen, unmaintained),
third-party services we call (Circle, The Graph, World, Google Cloud), and findings that
require a compromised signer or owner key as a *precondition* — those are the trust model,
documented rather than fixable.

Please do not run automated scanners against the production hosts; the API sits on a free
tier and a scan takes the hourly press down with it.
