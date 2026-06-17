"""GitHub PR Reviewer — OpenHands Automation (v2.7 — last-marker parser).

Cron-polls a GitHub repository for open pull requests carrying the configured
trigger label. Each PR gets exactly one OpenHands conversation, and the
resulting review is posted via the **GitHub Pull Request Reviews API**
(`POST /repos/{owner}/{repo}/pulls/{n}/reviews`) with one inline thread per
finding — never as a single issue comment. The format matches
`github-code-quality[bot]`: one review per call, `comments[]` array with
`path` + `line` + `side: "RIGHT"`, each thread body shaped as
`## <🔴 Critical|🟠 Important|🟡 Suggestion> <Category>` and ending with a
```suggestion``` block for one-click apply when the fix is short and
contiguous.

This is the v2.7 release. The only change vs v2.6 is in the JSON parser:
the parser now scans ALL occurrences of the `###REVIEW_JSON###` marker
(try from the last one backwards) so that descriptive prose mentioning the
marker doesn't shadow the real JSON contract. Earlier versions matched the
FIRST occurrence, which broke on responses that referenced the marker in
their preamble.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlencode

REPO = "m0nklabs/cryptotrader"
TRIGGER_LABEL = "openhands-review"
REVIEW_TONE = "thorough"
REVIEW_STYLE_INSTRUCTIONS = ""
DEFAULT_OPENHANDS_URL = "http://localhost:8000"

DONE_DEBOUNCE = 15
TERMINAL_STATUSES = {"idle", "finished", "error", "stuck"}


def _get_env_key() -> str:
    return os.environ.get("SESSION_API_KEY") or os.environ.get("OH_SESSION_API_KEYS_0") or ""


def get_secret(name: str) -> str:
    url = os.environ.get("AGENT_SERVER_URL", "").rstrip("/")
    key = _get_env_key()
    req = urllib.request.Request(
        f"{url}/api/settings/secrets/{name}",
        headers={"X-Session-API-Key": key},
    )
    with urllib.request.urlopen(req) as r:
        return r.read().decode().strip()


def fire_callback(
    status: str = "COMPLETED",
    error: str | None = None,
    conversation_id: str | None = None,
) -> None:
    url = os.environ.get("AUTOMATION_CALLBACK_URL", "")
    if not url:
        return
    body: dict = {"status": status, "run_id": os.environ.get("AUTOMATION_RUN_ID", "")}
    if error:
        body["error"] = error
    if conversation_id:
        body["conversation_id"] = conversation_id
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {os.environ.get('AUTOMATION_CALLBACK_API_KEY', '')}",
        },
    )
    try:
        urllib.request.urlopen(req)
    except Exception as exc:
        print(f"Callback error (non-fatal): {exc}")


def _state_file_path() -> str:
    workspace_base = os.environ.get("WORKSPACE_BASE", "")
    event_payload = json.loads(os.environ.get("AUTOMATION_EVENT_PAYLOAD", "{}"))
    automation_id = event_payload.get("automation_id", "default")

    if workspace_base:
        root = Path(workspace_base).resolve().parent.parent
    else:
        root = Path.home() / ".openhands" / "workspaces"

    state_dir = root / "automation-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    return str(state_dir / f"github_pr_reviewer_label_event_{automation_id}.json")


def load_state(path: str) -> dict:
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"Warning: state file {path} unreadable ({exc}); starting fresh")
    return {
        "version": 2,
        "repo": REPO,
        "trigger_label": TRIGGER_LABEL,
        "reviews": {},
        "prs": {},
    }


def _github_request(token: str, method: str, path: str, body: dict | None = None) -> tuple[dict, dict]:
    url = f"https://api.github.com{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "openhands-pr-reviewer-automation/2.7",
    }
    data = json.dumps(body).encode() if body is not None else None
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read()
            return (json.loads(raw) if raw.strip() else {}), dict(r.headers)
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode(errors="replace")
        raise RuntimeError(f"GitHub {method} {path} → {exc.code}: {body_text}") from exc


def _github_paginate(token: str, path: str, base_params: dict | None = None) -> list[dict]:
    base_params = dict(base_params or {})
    base_params.setdefault("per_page", 100)
    page = 1
    results: list[dict] = []
    while True:
        params = dict(base_params)
        params["page"] = page
        url = f"{path}?{urlencode(params, doseq=True)}"
        data, _ = _github_request(token, "GET", url)
        if not isinstance(data, list):
            break
        results.extend(data)
        if len(data) < base_params["per_page"]:
            break
        page += 1
    return results


def _resolve_github_token() -> str:
    try:
        token = get_secret("GITHUB_PERSONAL_ACCESS_TOKEN")
        if token:
            return token
    except Exception:
        pass
    raise RuntimeError(
        "GITHUB_PERSONAL_ACCESS_TOKEN secret is not set. "
        "Go to OpenHands Settings → Secrets and add your GitHub Personal Access Token."
    )


def _verify_token_and_repo(token: str, repo: str) -> None:
    try:
        user_data, _ = _github_request(token, "GET", "/user")
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise RuntimeError("GITHUB_PERSONAL_ACCESS_TOKEN is invalid or expired.") from exc
        raise RuntimeError(f"GitHub /user check failed: {exc.code}") from exc

    print(f"Authenticated as GitHub user: {user_data.get('login', '?')}")

    try:
        _github_request(token, "GET", f"/repos/{repo}")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise RuntimeError(f"Repository '{repo}' is not accessible with the current token.") from exc
        raise RuntimeError(f"GitHub /repos/{repo} check failed: {exc.code}") from exc


def _list_open_prs(token: str, repo: str) -> list[dict]:
    return _github_paginate(
        token,
        f"/repos/{repo}/pulls",
        {"state": "open", "sort": "updated", "direction": "desc"},
    )


def _get_pr(token: str, repo: str, pr_number: int) -> dict:
    pr, _ = _github_request(token, "GET", f"/repos/{repo}/pulls/{pr_number}")
    return pr


def _get_issue_events(token: str, repo: str, pr_number: int) -> list[dict]:
    return _github_paginate(token, f"/repos/{repo}/issues/{pr_number}/events")


def _latest_trigger_label_event(token: str, repo: str, pr_number: int) -> dict | None:
    events = _get_issue_events(token, repo, pr_number)
    matching = [
        event for event in events
        if event.get("event") == "labeled"
        and (event.get("label") or {}).get("name", "").lower() == TRIGGER_LABEL.lower()
        and event.get("id") is not None
    ]
    if not matching:
        return None
    return max(matching, key=lambda event: (event.get("created_at") or "", int(event.get("id") or 0)))


# ---------------------------------------------------------------------------
# Reviews API posting
# ---------------------------------------------------------------------------

def _post_github_review(
    token: str,
    repo: str,
    pr_number: int,
    commit_id: str,
    body: str,
    event: str,
    comments: list[dict],
) -> dict:
    """Post a single Pull Request Review with inline comments."""
    payload = {
        "commit_id": commit_id,
        "body": body,
        "event": event,
        "comments": comments,
    }
    data, _ = _github_request(
        token,
        "POST",
        f"/repos/{repo}/pulls/{pr_number}/reviews",
        body=payload,
    )
    return data


def _list_pr_files(token: str, repo: str, pr_number: int) -> set[str]:
    try:
        files, _ = _github_request(
            token, "GET", f"/repos/{repo}/pulls/{pr_number}/files?per_page=100"
        )
        if isinstance(files, list):
            return {f.get("filename", "") for f in files if f.get("filename")}
    except Exception as exc:
        print(f"  Warning: could not list PR files: {exc}")
    return set()


def _filter_valid_comments(
    comments: list[dict], valid_paths: set[str]
) -> tuple[list[dict], list[dict]]:
    if not valid_paths:
        return comments, []
    valid, invalid = [], []
    for c in comments:
        if c["path"] in valid_paths:
            valid.append(c)
        else:
            invalid.append(c)
    return valid, invalid


def _list_existing_reviews(
    token: str, repo: str, pr_number: int
) -> list[dict]:
    try:
        reviews, _ = _github_request(
            token, "GET", f"/repos/{repo}/pulls/{pr_number}/reviews?per_page=100"
        )
        if isinstance(reviews, list):
            return reviews
    except Exception as exc:
        print(f"  Warning: could not list existing reviews: {exc}")
    return []


def _resolve_event(token: str, pr: dict, requested: str) -> str:
    """Coalesce REQUEST_CHANGES → COMMENT when the reviewer is the PR author.

    GitHub rejects `REQUEST_CHANGES` on your own PR with HTTP 422.
    """
    if requested != "REQUEST_CHANGES":
        return requested
    pr_author = (pr.get("user") or {}).get("login", "")
    try:
        me, _ = _github_request(token, "GET", "/user")
        me_login = me.get("login", "")
    except Exception:
        me_login = ""
    if me_login and pr_author and me_login.lower() == pr_author.lower():
        print(
            f"  Note: downgrading REQUEST_CHANGES → COMMENT because "
            f"`{me_login}` is the PR author"
        )
        return "COMMENT"
    return requested


def _fallback_post_issue_comment(token: str, repo: str, pr_number: int, body: str) -> None:
    _github_request(
        token,
        "POST",
        f"/repos/{repo}/issues/{pr_number}/comments",
        body={"body": body},
    )


# ---------------------------------------------------------------------------
# REVIEW_JSON parser (v2.7 — last-marker wins)
# ---------------------------------------------------------------------------

_REVIEW_JSON = re.compile(r"###REVIEW_JSON###", re.DOTALL)


def _extract_from(response: str, start: int) -> str | None:
    i = start
    while i < len(response) and response[i] in " \t\r\n":
        i += 1
    if i < len(response) and response[i] == "`":
        while i < len(response) and response[i] == "`":
            i += 1
        while i < len(response) and response[i] not in "\n":
            i += 1
        if i < len(response):
            i += 1
    while i < len(response) and response[i] in " \t\r\n":
        i += 1
    if i >= len(response) or response[i] != "{":
        return None
    depth = 0
    in_string = False
    escape = False
    j = i
    while j < len(response):
        c = response[j]
        if in_string:
            if escape:
                escape = False
            elif c == "\\":
                escape = True
            elif c == '"':
                in_string = False
        else:
            if c == '"':
                in_string = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return response[i : j + 1]
        j += 1
    return None


def _extract_json_after_marker(response: str) -> str | None:
    """Find the LAST `###REVIEW_JSON###` marker in the response and extract
    the JSON object that follows it. Tries each marker from the end backwards
    so that descriptive prose mentioning the marker doesn't shadow the real
    JSON contract.
    """
    if not response:
        return None
    matches = list(_REVIEW_JSON.finditer(response))
    for m in reversed(matches):
        raw = _extract_from(response, m.end())
        if raw is not None:
            try:
                json.loads(raw)
                return raw
            except json.JSONDecodeError:
                continue
    return None


def _parse_review_payload(response: str) -> dict | None:
    if not response:
        return None
    raw = _extract_json_after_marker(response)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if "comments" not in data or not isinstance(data["comments"], list):
        return None
    cleaned: list[dict] = []
    for c in data["comments"]:
        if not isinstance(c, dict):
            continue
        if not c.get("path") or not c.get("line") or not c.get("body"):
            continue
        try:
            line = int(c["line"])
        except (TypeError, ValueError):
            continue
        if line <= 0:
            continue
        cleaned.append({
            "path": str(c["path"]),
            "line": line,
            "side": str(c.get("side") or "RIGHT"),
            "body": str(c["body"]),
        })
    if not cleaned:
        return None
    return {
        "body": str(data.get("body") or "").strip(),
        "event": str(data.get("event") or "COMMENT"),
        "comments": cleaned,
    }


# ---------------------------------------------------------------------------
# Agent-server plumbing
# ---------------------------------------------------------------------------

def _oh_request(agent_url: str, api_key: str, method: str, path: str, body: dict | None = None) -> dict:
    url = f"{agent_url}{path}"
    headers = {"X-Session-API-Key": api_key, "Content-Type": "application/json"}
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode()
        raise RuntimeError(f"Agent API {method} {path} → {exc.code}: {body_text}") from exc


def _fetch_settings(agent_url: str, api_key: str) -> dict:
    req = urllib.request.Request(
        f"{agent_url}/api/settings",
        headers={"X-Session-API-Key": api_key, "X-Expose-Secrets": "plaintext"},
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def _get_agent_dict(agent_url: str, api_key: str) -> dict:
    data = _fetch_settings(agent_url, api_key)
    llm = data.get("agent_settings", {}).get("llm", {})
    return {
        "kind": "Agent",
        "llm": llm,
        "tools": [{"name": "terminal"}, {"name": "file_editor"}],
    }


def _get_mcp_config(agent_url: str, api_key: str) -> dict | None:
    try:
        data = _fetch_settings(agent_url, api_key)
        mcp_config = data.get("agent_settings", {}).get("mcp_config")
        if isinstance(mcp_config, dict) and mcp_config.get("mcpServers"):
            return mcp_config
    except Exception as exc:
        print(f"Warning: could not fetch MCP config: {exc}")
    return None


def _list_secret_names(agent_url: str, api_key: str) -> list[dict]:
    try:
        result = _oh_request(agent_url, api_key, "GET", "/api/settings/secrets")
        return result.get("secrets", [])
    except Exception as exc:
        print(f"Warning: could not list secrets: {exc}")
    return []


def _build_secrets_payload(agent_url: str, api_key: str) -> dict:
    secrets = {}
    for secret in _list_secret_names(agent_url, api_key):
        name = secret.get("name", "")
        if not name:
            continue
        lookup: dict = {
            "kind": "LookupSecret",
            "url": f"/api/settings/secrets/{name}",
        }
        if api_key:
            lookup["headers"] = {"X-Session-API-Key": api_key}
        desc = secret.get("description")
        if desc:
            lookup["description"] = desc
        secrets[name] = lookup
    return secrets


def create_conversation(agent_url: str, api_key: str, initial_message: str) -> str:
    workspace_dir = os.environ.get("WORKSPACE_BASE", "/workspace")
    payload: dict = {
        "workspace": {"working_dir": workspace_dir},
        "agent": _get_agent_dict(agent_url, api_key),
        "initial_message": {"content": [{"text": initial_message}]},
    }
    secrets = _build_secrets_payload(agent_url, api_key)
    if secrets:
        payload["secrets"] = secrets
    mcp_config = _get_mcp_config(agent_url, api_key)
    if mcp_config:
        payload["mcp_config"] = mcp_config
    result = _oh_request(agent_url, api_key, "POST", "/api/conversations", payload)
    return result["id"]


def conversation_status(agent_url: str, api_key: str, conv_id: str) -> str:
    result = _oh_request(agent_url, api_key, "GET", f"/api/conversations/{conv_id}")
    return result.get("execution_status", "unknown")


def conversation_final_response(agent_url: str, api_key: str, conv_id: str) -> str:
    result = _oh_request(agent_url, api_key, "GET", f"/api/conversations/{conv_id}/agent_final_response")
    return result.get("response", "")


_TONE_INSTRUCTIONS = {
    "thorough": (
        "Provide a comprehensive review. Cover correctness, security vulnerabilities, "
        "missing or inadequate tests, code style, maintainability, and potential edge cases. "
        "Reference specific files and line numbers where relevant."
    ),
    "concise": (
        "Provide a brief, high-signal review. Focus only on important bugs, security problems, "
        "or significant design flaws. Omit minor style feedback."
    ),
    "friendly": (
        "Provide a constructive, encouraging review. Acknowledge what is done well before "
        "raising concerns while still noting real issues."
    ),
}


def _labels(pr: dict) -> list[str]:
    return [label.get("name", "") for label in pr.get("labels", [])]


def _has_trigger_label(pr: dict) -> bool:
    return any(label.lower() == TRIGGER_LABEL.lower() for label in _labels(pr))


def _head_sha(pr: dict) -> str:
    return ((pr.get("head") or {}).get("sha") or "").strip()


def _review_key(pr_number: int, label_event_id: int | str) -> str:
    return f"{pr_number}:label:{label_event_id}"


def _with_ai_disclosure(body: str) -> str:
    disclosure = "_This comment was posted by an AI agent (OpenHands)._"
    body = (body or "").strip()
    if disclosure.lower() in body.lower():
        return body
    return f"{body}\n\n{disclosure}" if body else disclosure


# ---------------------------------------------------------------------------
# Prompt template
# ---------------------------------------------------------------------------

def _build_review_prompt(pr: dict, head_sha: str, label_event: dict) -> str:
    number = pr.get("number", "?")
    title = pr.get("title", "(no title)")
    body = (pr.get("body") or "").strip() or "(no description)"
    html_url = pr.get("html_url", "")
    author = (pr.get("user") or {}).get("login", "?")
    base_branch = (pr.get("base") or {}).get("ref", "?")
    head_branch = (pr.get("head") or {}).get("ref", "?")
    label_str = ", ".join(_labels(pr)) or "(none)"
    label_event_id = label_event.get("id", "?")
    label_event_created_at = label_event.get("created_at", "?")
    changed_files = pr.get("changed_files", "?")
    additions = pr.get("additions", "?")
    deletions = pr.get("deletions", "?")
    clone_url = f"https://github.com/{REPO}.git"
    tone = _TONE_INSTRUCTIONS.get(REVIEW_TONE, _TONE_INSTRUCTIONS["thorough"])
    extra = f"\n\nAdditional style instructions:\n{REVIEW_STYLE_INSTRUCTIONS}" if REVIEW_STYLE_INSTRUCTIONS.strip() else ""

    return (
        "/github-pr-review\n\n"
        "## How to Post the Review (CRITICAL — read first)\n\n"
        "You MUST post your review via the **GitHub Pull Request Reviews API** "
        "(`POST /repos/{owner}/{repo}/pulls/{n}/reviews`) with one inline comment "
        "per finding — never as a single issue comment. Do not call `gh pr comment`, "
        "do not call `POST /repos/{owner}/{repo}/issues/{n}/comments`, and do not "
        "produce a single Markdown blob. The automation script that wraps your "
        "conversation will call the Reviews API for you based on the JSON you "
        "return in the `###REVIEW_JSON###` block below.\n\n"
        "Use exactly one fenced code block named `###REVIEW_JSON###` at the very end "
        "of your final response. The block must parse as JSON and have this shape:\n\n"
        "```\n"
        "###REVIEW_JSON###\n"
        "{\n"
        "  \"event\": \"COMMENT\",\n"
        "  \"body\": \"Brief 1–3 sentence summary. Inline comments below.\",\n"
        "  \"comments\": [\n"
        "    {\n"
        "      \"path\": \"api/auth.py\",\n"
        "      \"line\": 87,\n"
        "      \"side\": \"RIGHT\",\n"
        "      \"body\": \"## 🟠 Important: JWT signature not verified\\n\\n"
        "Forged tokens pass because `jwt.decode()` is called without the secret.\\n\\n"
        "---\\n\\n"
        "`jwt.decode()` requires both the secret and the allowed `algorithms` list.\\n\\n"
        "Best fix in this file (`api/auth.py:87`): pass `SECRET_KEY` and `algorithms=[\\\"HS256\\\"]`.\\n\\n"
        "No new methods or dependencies needed.\\n\\n"
        "```suggestion\\n"
        "token_data = jwt.decode(token, SECRET_KEY, algorithms=[\\\"HS256\\\"])\\n"
        "```\"\n"
        "    }\n"
        "  ]\n"
        "}\n"
        "```\n\n"
        "Each `comments[i].body` starts with `## <🔴 Critical|🟠 Important|🟡 Suggestion> <Category>`, "
        "then a one-line statement of the issue, a `---` separator, a prose fix explanation, "
        "a \"Best fix in this file (`<path:line>`)\" anchor, a scope confirmation (\"No new "
        "methods or dependencies needed.\"), and ends with a `\\`\\`\\`suggestion\\`\\`\\`` block for "
        "one-click apply when the fix is ≤ 5 lines and contiguous. Never use `🟢` priority "
        "labels — if the code is fine, do not comment on it.\n\n"
        "`event` is one of `COMMENT` (default), `APPROVE`, or `REQUEST_CHANGES`.\n\n"
        "## Pull Request Information\n\n"
        f"- **Title**: {title}\n"
        f"- **Description**: {body}\n"
        f"- **Repository**: {REPO}\n"
        f"- **Base Branch**: {base_branch}\n"
        f"- **Head Branch**: {head_branch}\n"
        f"- **PR Number**: {number}\n"
        f"- **Head SHA**: {head_sha}\n"
        f"- **Trigger**: latest `{TRIGGER_LABEL}` labeled event {label_event_id} at {label_event_created_at}\n"
        f"- **Labels**: {label_str}\n"
        f"- **Changes**: +{additions} -{deletions} across {changed_files} file(s)\n"
        f"- **URL**: {html_url}\n\n"
        "## Required Workflow\n\n"
        "1. Clone the repository into a fresh working directory inside the workspace.\n"
        f"   Example: `git clone {clone_url} pr-review-{number}`.\n"
        "2. Check out the exact pull request branch by PR number, then verify HEAD matches the SHA above.\n"
        f"   Example: `git fetch origin pull/{number}/head:openhands-pr-{number}` followed by `git checkout openhands-pr-{number}`.\n"
        "3. Inspect the existing PR context before reviewing, including PR description, issue comments, "
        "review comments, changed files, and the diff.\n"
        "   Prefer `gh pr view`, `gh pr diff`, `gh pr checkout`, or GitHub REST API calls with "
        "`GITHUB_PERSONAL_ACCESS_TOKEN`; do not print secret values.\n"
        "4. Use the checked-out repository to inspect relevant files and surrounding code, not just the patch.\n"
        "5. Before producing the final response, delete only the cloned repository directory created in step 1.\n"
        f"   Example: `rm -rf pr-review-{number}`. Do not delete any other files or directories.\n"
        "6. Produce the final review in **two parts**: a short Markdown summary, then the "
        "`###REVIEW_JSON###` fenced block exactly as specified above. The summary is for "
        "humans reading the agent-server log; the JSON is the contract the automation "
        "script uses to call the Reviews API. **Important**: only the **last** "
        "`###REVIEW_JSON###` marker in your response is used, so you can reference "
        "the marker in your summary prose without ambiguity — the parser will pick "
        "the last occurrence and parse the JSON that follows it.\n\n"
        f"## Review Tone\n\n{tone}{extra}\n\n"
        "End the JSON's `body` with a clear verdict on its own line: either "
        "`✅ APPROVED` or `🔄 CHANGES REQUESTED`."
    )


# ---------------------------------------------------------------------------
# Review processing
# ---------------------------------------------------------------------------

def _process_review_request(
    github_token: str,
    agent_url: str,
    api_key: str,
    openhands_url: str,
    pr: dict,
    label_event: dict,
    reviews: dict,
) -> str | None:
    number = pr["number"]
    head_sha = _head_sha(pr)
    label_event_id = label_event["id"]
    key = _review_key(number, label_event_id)
    title = pr.get("title", "(no title)")
    html_url = pr.get("html_url", "")

    print(f"  Queuing review for PR #{number} from `{TRIGGER_LABEL}` event {label_event_id} at {head_sha[:12]}: {title}")
    prompt = _build_review_prompt(pr, head_sha, label_event)

    try:
        conv_id = create_conversation(agent_url, api_key, prompt)
    except Exception as exc:
        print(f"  Error creating conversation for PR #{number}: {exc}")
        return None

    reviews[key] = {
        "pr_number": number,
        "head_sha": head_sha,
        "trigger_label_event_id": label_event_id,
        "trigger_label_event_created_at": label_event.get("created_at"),
        "html_url": html_url,
        "status": "active",
        "conversation_id": conv_id,
        "last_activity": time.time(),
    }
    print(f"  Created review conversation {conv_id}")

    conv_url = f"{openhands_url}/conversations/{conv_id}"
    _fallback_post_issue_comment(
        github_token,
        REPO,
        number,
        _with_ai_disclosure(
            "🤖 **OpenHands is reviewing this PR.**\n\n"
            f"Trigger label: `{TRIGGER_LABEL}`\n"
            f"Label event: `{label_event_id}` at `{label_event.get('created_at', '?')}`\n"
            f"Head commit: `{head_sha}`\n"
            f"View the conversation: {conv_url}"
        ),
    )
    return conv_id


def _check_conversation_completion(
    rec: dict,
    latest_open_prs: dict[int, dict],
    github_token: str,
    agent_url: str,
    api_key: str,
) -> None:
    if (time.time() - rec.get("last_activity", 0.0)) < DONE_DEBOUNCE:
        return

    conv_id = rec["conversation_id"]
    pr_number = rec["pr_number"]
    reviewed_sha = rec.get("head_sha", "")
    current_pr = latest_open_prs.get(pr_number)

    if not current_pr:
        rec["status"] = "closed"
        print(f"  PR #{pr_number} closed/merged — skipping result post")
        return

    current_sha = _head_sha(current_pr)
    if current_sha and reviewed_sha and current_sha != reviewed_sha:
        rec["status"] = "stale"
        rec["stale_reason"] = f"head changed from {reviewed_sha} to {current_sha}"
        print(f"  PR #{pr_number} advanced to {current_sha[:12]} — suppressing stale review {conv_id}")
        return

    try:
        status = conversation_status(agent_url, api_key, conv_id)
    except Exception as exc:
        print(f"  Warning: could not get status for {conv_id}: {exc}")
        return

    print(f"  PR #{pr_number} conversation {conv_id} → status={status}")
    if status not in TERMINAL_STATUSES:
        return

    try:
        final = conversation_final_response(agent_url, api_key, conv_id)
    except Exception:
        final = ""

    if status in {"error", "stuck"}:
        body = (
            f"⚠️  **OpenHands PR Reviewer encountered a problem** at commit "
            f"`{reviewed_sha[:12]}` (status: `{status}`).\n\n"
            f"{(final or '').strip()}"
        )
        _fallback_post_issue_comment(
            github_token,
            REPO,
            pr_number,
            _with_ai_disclosure(body),
        )
    else:
        payload = _parse_review_payload(final)
        if payload is None:
            print(f"  PR #{pr_number}: agent did not emit REVIEW_JSON block in final response")
            existing = _list_existing_reviews(github_token, REPO, pr_number)
            already_posted = any(
                (r.get("user") or {}).get("login", "").lower() == "m0nk111-post"
                for r in existing
            )

            if already_posted:
                print(
                    f"  PR #{pr_number}: m0nk111-post review(s) already present "
                    f"(agent used MCP directly) — closing"
                )
            else:
                msg = (
                    "⚠️  **OpenHands completed the review for commit "
                    f"`{reviewed_sha[:12]}`** but did not produce a parseable "
                    "`###REVIEW_JSON###` block and no `m0nk111-post` review was "
                    "found on the PR. Falling back to issue comment.\n\n"
                    f"```\n{(final or '').strip()[:6000]}\n```"
                )
                _fallback_post_issue_comment(
                    github_token,
                    REPO,
                    pr_number,
                    _with_ai_disclosure(msg),
                )
                print(f"  PR #{pr_number}: fell back to issue comment")
        else:
            valid_paths = _list_pr_files(github_token, REPO, pr_number)
            valid_comments, invalid_comments = _filter_valid_comments(
                payload["comments"], valid_paths
            )
            if invalid_comments:
                print(
                    f"  PR #{pr_number}: dropped {len(invalid_comments)} comment(s) "
                    f"with non-existent paths: "
                    f"{sorted({c['path'] for c in invalid_comments})}"
                )

            current_pr = _get_pr(github_token, REPO, pr_number)
            event = _resolve_event(
                github_token, current_pr, payload["event"]
            )

            try:
                _post_github_review(
                    github_token,
                    REPO,
                    pr_number,
                    commit_id=reviewed_sha,
                    body=payload["body"] or "Automated review by OpenHands.",
                    event=event,
                    comments=valid_comments,
                )
                print(
                    f"  Posted PR Review on PR #{pr_number} at {reviewed_sha[:12]} "
                    f"with {len(valid_comments)} inline comment(s) "
                    f"(event={event})"
                )
            except Exception as exc:
                print(f"  Error posting PR Review on PR #{pr_number}: {exc}")
                invalid_note = ""
                if invalid_comments:
                    invalid_note = (
                        "\n\nThe following comments were dropped because their "
                        "file paths are not in the PR diff:\n\n"
                        + "\n".join(
                            f"- `{c['path']}:{c['line']}`" for c in invalid_comments
                        )
                    )
                _fallback_post_issue_comment(
                    github_token,
                    REPO,
                    pr_number,
                    _with_ai_disclosure(
                        "⚠️  **OpenHands could not post the inline review via the Reviews API.**\n\n"
                        f"Error: `{exc}`\n\n"
                        f"```\n{(final or '').strip()[:6000]}\n```"
                        f"{invalid_note}"
                    ),
                )

    rec["status"] = "closed"
    rec["completed_at"] = time.time()


def main() -> str | None:
    state_path = _state_file_path()
    state = load_state(state_path)
    agent_url = os.environ.get("AGENT_SERVER_URL", "").rstrip("/")
    api_key = _get_env_key()

    github_token = _resolve_github_token()
    _verify_token_and_repo(github_token, REPO)

    try:
        openhands_url = get_secret("OPENHANDS_URL").rstrip("/") or DEFAULT_OPENHANDS_URL
    except Exception:
        openhands_url = DEFAULT_OPENHANDS_URL

    reviews: dict = state.setdefault("reviews", {})
    prs_state: dict = state.setdefault("prs", {})

    open_prs = _list_open_prs(github_token, REPO)
    latest_open_prs = {pr["number"]: pr for pr in open_prs}
    print(f"Found {len(open_prs)} open PR(s) in {REPO}")

    last_conversation_id = None

    for pr in open_prs:
        number = pr["number"]
        head_sha = _head_sha(pr)
        label_present = _has_trigger_label(pr)
        prs_state[str(number)] = {
            "head_sha": head_sha,
            "label_present": label_present,
            "labels": _labels(pr),
            "last_seen": time.time(),
        }

        if not label_present:
            continue
        if not head_sha:
            print(f"  PR #{number} missing head SHA, skipping")
            continue
        label_event = _latest_trigger_label_event(github_token, REPO, number)
        if not label_event:
            print(f"  PR #{number} has label but no matching label event, skipping")
            continue
        key = _review_key(number, label_event["id"])
        existing = reviews.get(key)
        if existing:
            if existing.get("status") not in (None, "stale", "closed", "error"):
                _check_conversation_completion(
                    existing, latest_open_prs, github_token, agent_url, api_key
                )
                last_conversation_id = existing.get("conversation_id")
            else:
                last_conversation_id = existing.get("conversation_id")
            continue
        last_conversation_id = _process_review_request(
            github_token, agent_url, api_key, openhands_url, pr, label_event, reviews
        )

    with open(state_path, "w") as f:
        json.dump(state, f, indent=2)

    print("State saved to", state_path)
    return last_conversation_id


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Fatal: {exc}")
        sys.exit(1)
