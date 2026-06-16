# agent-canvas-automation — v2

This directory holds the **standalone OpenHands automation** that the
agent-canvas `github-pr-review` automation runs on cron inside the
`m0nklabs/cryptotrader` repo. It is the bit of glue that:

1. **Polls** `m0nklabs/cryptotrader` for open PRs carrying the
   `openhands-review` label (configurable via `TRIGGER_LABEL`).
2. **Forks a fresh OpenHands conversation** per `(PR, label_event_id)` and
   feeds it the prompt template (see `_build_review_prompt` in `main.py`).
3. **Waits** for the conversation to reach a terminal state
   (`finished` / `error` / `stuck` / `idle`).
4. **Parses the agent's final response** for a `###REVIEW_JSON###` block
   (see `v2.7/main.py`).
5. **Posts the result via the GitHub Pull Request Reviews API** —
   `POST /repos/{owner}/{repo}/pulls/{n}/reviews` with one inline thread
   per finding, in the same shape as `github-code-quality[bot]`. **Never**
   `POST /repos/{owner}/{repo}/issues/{n}/comments` (issue-comments API).

Versions are tracked as tarballs uploaded via
`POST /api/automation/v1/uploads` on the OpenHands Automations backend
(usually `http://localhost:18001/api/automation/v1/uploads` locally) and
attached to the automation by `PATCH
/api/automation/v1/{automation_id}` with
`{"tarball_path": "oh-internal://uploads/<id>"}`.

This directory is the source-of-truth copy of those tarballs so that the
runtime config is reproducible from a clean checkout.

## v1 vs v2 — what changed and why

### The bug

The v1 tarball had two independent problems:

1. **The prompt template told the agent not to use the Reviews API.**
   From the v1 `_build_review_prompt`:

   > "You are an AI code reviewer. Review the GitHub pull request below and
   > write a **single review comment**. Do not modify files, push commits,
   > approve via the GitHub API, or request changes via the **review API**;
   > only produce the final comment text."

   The agent followed the instruction and produced one Markdown blob.

2. **The result-poster used the issue-comments API.** From v1
   `_post_github_comment`:

   ```python
   def _post_github_comment(token, repo, pr_number, body):
       _github_request(token, "POST",
                       f"/repos/{repo}/issues/{pr_number}/comments",
                       body={"body": body})
   ```

   The agent's blob was posted as a single issue comment on the PR — no
   inline threads, no `## 🟠 Important: ...` headings, no
   ` ```suggestion ` blocks, no per-finding thread anchoring. From the
   human's perspective the agent was pretending to review.

This is exactly the bug the upstream `OpenHands/extensions` PR #339
("fix(pr-review): post reviews via Pull Request Reviews API, not issue
comments") was supposed to fix, but **the agent-canvas automation does not
use the upstream `plugins/pr-review` plugin at all** — it has its own
tarball that was uploaded long before PR #339 landed, and that tarball
was never updated.

### The v2 fix

v2.1 through v2.6, then v2.7 (current), replace the prompt template and
the result-poster. Concretely:

| Concern | v1 | v2.7 |
|---|---|---|
| Prompt tells the agent to use the Reviews API? | No — explicitly forbids it | Yes — mandates `POST /repos/{owner}/{repo}/pulls/{n}/reviews` with `comments[]` |
| Prompt contract with the wrapper script? | None — the script posts the agent's prose verbatim as an issue comment | `###REVIEW_JSON###` block at the end of the response with `{event, body, comments[]}` |
| Result-poster API | `POST /repos/.../issues/{n}/comments` | `POST /repos/.../pulls/{n}/reviews` with `comments[]` |
| Path validation | n/a | Inline comments with `path` not in the PR diff are dropped with a log line (GitHub returns 422 for unknown paths) |
| Author-equals-reviewer | n/a | `REQUEST_CHANGES` is auto-downgraded to `COMMENT` when the bot user is the PR author (GitHub returns 422 otherwise) |
| Re-review guard | n/a | Already-closed `(PR, label_event_id)` pairs are skipped on the next cron tick instead of starting a new conversation |
| MCP-direct detection | n/a | If the agent posts the review via the GitHub MCP instead of the JSON contract, the script queries GitHub for existing `m0nk111-post` reviews and closes the state without double-posting |
| `###REVIEW_JSON###` parser | n/a | Brace-counting parser that handles both fenced ```json` and raw inline JSON, with a "last-marker wins" rule so descriptive prose mentioning the marker doesn't shadow the real JSON contract |

### Real-world demonstration

| PR | Outcome | Reference |
|---|---|---|
| PR 379 (old, before v1 was uploaded) | Used the new format because v1 wasn't yet the prompt | https://github.com/m0nklabs/cryptotrader/pull/379 |
| PR 380, 381 (with v1 tarball) | Old format (single issue-comment blob) | https://github.com/m0nklabs/cryptotrader/issues/4718914883 (the bad blob) |
| PR 380, 381, 387 (with v2.5/2.6) | New format with inline review threads | https://github.com/m0nklabs/cryptotrader/pull/381#pullrequestreview-4507050239 |
| PR 400 (with v2.7) | New format — but **2 reviews with identical content** landed on the PR (agent's MCP-direct post + script's post from the parsed JSON). The v2.7 script's MCP-detection only ran in the "no JSON" path. | https://github.com/m0nklabs/cryptotrader/pull/400 |
| PR 402 (clean e2e test, with v2.8) | New format, **exactly 1 review**. The v2.8 duplicate-review guard runs in BOTH the "no JSON" and "have JSON" paths. | https://github.com/m0nklabs/cryptotrader/pull/402 |

## How to apply this locally

```bash
# 1. Pack the v2.8 main.py into a gzipped tarball
cd plugins/pr-review/agent-canvas-automation/v2.8
tar -czf /tmp/pr-reviewer-v2.8.tar.gz main.py

# 2. Upload via the Automations API (port 18001 locally)
curl -s -X POST \
  -H "X-Session-API-Key: $OPENHANDS_AUTOMATION_API_KEY" \
  -H "Content-Type: application/gzip" \
  --data-binary @/tmp/pr-reviewer-v2.8.tar.gz \
  "http://localhost:18001/api/automation/v1/uploads?name=pr-reviewer-v2.8-reviews-api&description=..."

# 3. PATCH the automation to point at the new tarball
curl -s -X PATCH \
  -H "X-Session-API-Key: $OPENHANDS_AUTOMATION_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"tarball_path": "oh-internal://uploads/<id-from-step-2>"}' \
  "http://localhost:18001/api/automation/v1/9581078a7fce41578fb104acfd4e718a"
```

## Versions

| Version | What changed | When |
|---|---|---|
| v2.0 | Initial v2 (prompt + Reviews API) | 2026-06-16 |
| v2.1 | First re-pack (no code change) | 2026-06-16 |
| v2.2 | Re-review guard for closed (PR, label_event_id) pairs | 2026-06-16 |
| v2.3 | Skipped (experimental) | 2026-06-16 |
| v2.4 | Skipped (experimental) | 2026-06-16 |
| v2.5 | Path validation, REQUEST_CHANGES→COMMENT for PR author, MCP-direct detection in "no JSON" path | 2026-06-16 |
| v2.6 | Re-pack (no code change) | 2026-06-16 |
| v2.7 | Last-marker parser fix (handles agent prose that mentions `###REVIEW_JSON###`) | 2026-06-16 |
| **v2.8** | **Duplicate-review guard in BOTH the "no JSON" and "have JSON" paths** — the v2.7 only had it in the "no JSON" path, so a run where the agent posted via MCP AND the script also posted from the parsed JSON would produce two reviews with identical content (as seen on PR 400). | 2026-06-16 |

The new prompt template also requires the agent to use the
`/github-pr-review` trigger (matched against the `github-pr-review` skill
in the `OpenHands/extensions` registry) so the upstream plugin's
priority-label and ` ```suggestion ` block conventions are followed.

## Why this isn't upstreamed

`OpenHands/extensions` has the `plugins/pr-review` plugin and the
`github-pr-review` skill (with the new format). The v1 tarball predates
those and was never updated. The right long-term fix is to retire the
v1 tarball entirely and let agent-canvas run `plugins/pr-review`'s
action directly — see `OpenHands/extensions` issue tracker for the
ongoing effort. Until that lands, this `agent-canvas-automation/v2.7/`
tarball is the working solution.
