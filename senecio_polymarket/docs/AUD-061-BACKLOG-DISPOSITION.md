# AUD-061 open backlog disposition

No open PR was merged, rebased, closed, or imported wholesale.

| PR | Disposition | Evidence-based action |
|---|---|---|
| #1 | `UNRELATED_LEGACY` | Cloudflare Workers configuration is absent from and unrelated to the current Northflank Python service. Left open; no authority to close. |
| #2 | `UNRELATED_LEGACY` | Fly.io configuration is absent from and unrelated to the current Northflank service. Left open; no authority to close. |
| #24 | `UNIQUE_VALUE_TO_PORT` | Ported only independently justified governance ideas: explicit current authority, immutable historical policy checks, safety/path evidence. Stale branch/SHA manifests and its workflow were not imported. |
| #25 | `UNIQUE_VALUE_TO_PORT` | Ported only independently justified deterministic paper-research ideas: evidence hashes, code/config hashes, abstention reasons, reproducible read-only manifests. The H011 engine and stacked governance were not imported. |
| #36 | `RUNTIME017_ISOLATED` | Not fetched for content, modified, merged, rebased, closed, or imported. |

Issue #4 is historical deployment evidence plus an unresolved Combined/CD
constraint. AUD-061 appends current read-only observations but leaves it open:
current production SHA, deployment identity, image digest, and CD configuration
are not exposed by the public service, so supersession cannot be proven.
