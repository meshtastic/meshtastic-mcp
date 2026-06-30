---
name: meshtastic-org-knowledge
license: GPL-3.0-only
description: Find and answer questions across all Meshtastic GitHub repos — what projects exist, what their docs say, their current status (releases, open PRs/issues, recent commits), and how they relate. Use when a question spans more than one repo, or asks "which project does X", "what's the status of Y", "where is Z documented", or "what changed recently across the org".
---

# Meshtastic org knowledge

Answer org-wide questions using the `gh` CLI via bash. No GitHub MCP server needed —
`gh` is already authenticated, uses near-zero tokens (no schema overhead), and is more
reliable than the MCP server for read-only org queries.

**Prerequisite:** `gh auth status` must show `github.com` logged in. If not:
```bash
gh auth login   # or: gh auth login --with-token <<< "$GITHUB_TOKEN"
```

---

## Recipes

### Discover repos

```bash
# All active repos in the org (sorted by recent push)
gh repo list meshtastic --limit 100 --json name,description,pushedAt,defaultBranchRef \
  --jq 'sort_by(.pushedAt) | reverse | .[] | [.name, .pushedAt, .description] | @tsv'

# Search for repos matching a topic or name
gh repo list meshtastic --limit 100 --json name,description \
  --jq '.[] | select(.name | test("android|apple|firmware|python"; "i"))'
```

### Read docs

```bash
# README for a repo
gh repo view meshtastic/<repo> --json description,readme --jq '.readme'

# Arbitrary file
gh api repos/meshtastic/<repo>/contents/README.md --jq '.content' | base64 -d

# AGENTS.md / CLAUDE.md
gh api repos/meshtastic/<repo>/contents/AGENTS.md --jq '.content' | base64 -d

# Search code across the org
gh search code "<symbol-or-phrase>" --owner meshtastic --limit 20 \
  --json path,repository --jq '.[] | [.repository.name, .path] | @tsv'
```

### Repo status

```bash
# Latest release
gh release view --repo meshtastic/<repo> --json tagName,publishedAt,body

# All releases
gh release list --repo meshtastic/<repo> --limit 10

# Open PRs
gh pr list --repo meshtastic/<repo> --state open --json number,title,updatedAt,author \
  --jq 'sort_by(.updatedAt) | reverse | .[0:10]'

# Open issues
gh issue list --repo meshtastic/<repo> --state open --limit 20 \
  --json number,title,updatedAt,labels \
  --jq 'sort_by(.updatedAt) | reverse | .[]'

# Recent commits
gh api repos/meshtastic/<repo>/commits --jq '.[0:10] | .[] | [.sha[0:8], .commit.message | split("\n")[0]] | @tsv'
```

### Org-wide recent activity

```bash
# Recent commits across org (last N days)
gh search commits --owner meshtastic --sort committer-date --order desc --limit 30 \
  --json repository,sha,commit --jq '.[] | [.repository.name, .sha[0:8], .commit.message | split("\n")[0]] | @tsv'

# Recently merged PRs (closedAt = merge date for merged PRs)
gh search prs --owner meshtastic --merged --sort updated --order desc --limit 20 \
  --json repository,number,title,closedAt \
  --jq '.[] | [.closedAt[0:10], .repository.name, (.number|tostring), .title] | @tsv'

# Recently opened issues
gh search issues --owner meshtastic --state open --sort updated --order desc --limit 20 \
  --json repository,number,title,updatedAt \
  --jq '.[] | [.repository.name, (.number|tostring), .title] | @tsv'
```

---

## Cross-repo map

| Repo | Lang | Default branch | Role |
|---|---|---|---|
| **protobufs** | Protobuf | master | Wire-protocol source of truth; firmware + every client generates from it |
| **firmware** | C++ | develop | The radio — built/flashed by this repo's firmware tools |
| **Meshtastic-Android** | Kotlin | master | Android app |
| **Meshtastic-Apple** | Swift | main | iOS / macOS app |
| **web** | TypeScript | master | Web client |
| **python** | Python | master | CLI + Python API library |
| **rust** | Rust | main | Rust library |
| **meshtastic-sdk** | Kotlin (KMP) | main | Kotlin Multiplatform SDK |
| **meshtastic-mcp** | Python | master | This repo — AI tooling to drive firmware + apps |
| **framework-portduino** | C++ | master | Portduino HAL (Darwin multicast fix: PR #75) |

Default branches vary — pass `--ref <branch>` or `gh api ... ?ref=<branch>` when reading
off a non-default branch.

---

## Tips

- Pipe to `jq` for structured filtering; `--jq` inline keeps it one command.
- `gh api` gives raw REST access for anything `gh` sub-commands don't expose:
  ```bash
  gh api orgs/meshtastic/repos --paginate --jq '.[].name'
  ```
- Add `--limit 100` to list commands — default is 30.
- `gh search code` scans the default branch only; use `gh api search/code?q=...` for
  cross-branch searches.
