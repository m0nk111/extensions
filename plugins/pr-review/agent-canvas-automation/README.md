# agent-canvas-automation

This directory is the **source-of-truth copy** of the OpenHands Automations
tarball that powers a `github-pr-review`-style cron automation on a single
agent-canvas deployment.

## What this is for

The upstream `OpenHands/extensions` repo has a `plugins/pr-review` plugin
that produces good, inline-review-threads-style PR reviews (see
[`skills/github-pr-review/SKILL.md`](../skills/github-pr-review/SKILL.md)).
**Agent-canvas does not currently use that plugin directly.** Instead, it
runs a custom OpenHands automation (`9581078a-7fce-4157-8fb1-04acfd4e718a`)
that was set up before the upstream plugin reached a usable state, and the
custom automation lives in an uploaded tarball — a single `main.py` plus
the OpenHands Automations framework.

This directory tracks that tarball's source so that:

- The runtime is reproducible from a clean checkout.
- The history of the v1 → v2 fix is recorded.
- A future PR that retires this automation in favour of the upstream
  plugin has a clear baseline to compare against.

## Layout

| Path | Purpose |
|---|---|
| `v2.7/main.py` | The current source-of-truth. The runtime tarball is `tar -czf` of this file. |
| `v2/README.md`  | The v1 → v2.7 changelog, with the bug summary and the demonstration table. |

## Why a fork, not upstream

`OpenHands/extensions` is the public extensions registry. The
agent-canvas-automation is **specific to one user's agent-canvas
deployment** (a single OpenHands Automations instance on one host, with
one cron job and one automation ID). It is not general-purpose plugin
code — it's the wiring that talks to that specific deployment.

So:

- The "what should the agent prompt look like" lives in
  `OpenHands/extensions` (skills/github-pr-review/SKILL.md, the
  `###REVIEW_JSON###` contract, the priority labels) — that is general
  knowledge.
- The "how to post the result, given the agent's output, on this
  specific deployment, with these specific secrets" lives here, on the
  fork, because it depends on the deployment.
- When the upstream `plugins/pr-review` plugin gets a deployment story
  that doesn't need a custom tarball, this whole directory can be
  deleted and the agent-canvas automation can be re-pointed at the
  upstream plugin's `action.yml`.

## See also

- The "v1" tarball, still in agent-canvas but no longer recommended:
  `oh-internal://uploads/80f203b3-0678-404a-9b60-401782a0e4b9` —
  described in `v2/README.md` so anyone can understand why it was
  replaced.
- The Automations API endpoints used to upload and patch tarballs:
  - `POST /api/automation/v1/uploads` (returns `id`, `tarball_path`)
  - `PATCH /api/automation/v1/{automation_id}` with `{"tarball_path": "..."}`
- The `m0nk111/extensions` `.pth` shim that points the agent-server at
  this fork as the public-skills source: see the dotfiles-managed
  `openhands_fork_extensions.{py,pth}` in the agent-server's site-packages.
