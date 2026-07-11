# babysit-prs

Shepherd your own open pull requests so you stop hand-watching CI.

Babysitting a single PR is a pile of low-value context switches: re-running a flaky required check, keeping the branch current when the base moved, noticing a reviewer left a comment, and remembering to merge once it is green. None of that needs your attention until something is actually stuck. `babysit-prs` does the mechanical parts and notifies you on macOS only when a PR's state changes in a way you care about.

It is a small, dependency-free Python script designed to run every few minutes from `launchd` (macOS) or `cron`.

## What it does

For each of your recently-active open PRs (optionally limited to specific owners), it:

- **Re-runs failed required checks** once per head commit (`gh run rerun --failed` on the exact Actions runs backing the failed required checks), then notifies you only if they are still red after that retry. This is the flaky-test retry.
- **Updates the branch** (`gh pr update-branch`) when the base requires up-to-date branches and the PR is cleanly behind, so CI re-runs against the latest base without your involvement.
- **Notifies you** when a PR needs a human: merge conflicts, changes requested, a new human review comment (including inline review-thread replies; bots and the Copilot reviewer are excluded), a failed branch update, a required check still failing after the retry, or a non-draft PR that is green and ready to merge.

### Guardrails

The auto-actions are deliberately conservative:

- Only **required** checks are ever re-run. If the required set cannot be read (you lack admin on the repo, or it uses rulesets you cannot see), CI auto-actions are skipped for that PR rather than guessed at.
- A branch is only updated when the base is **strict** (requires up-to-date branches) and the PR is cleanly **behind**. Merge conflicts are never auto-resolved; they are surfaced for you.
- Everything ambiguous is left for you.

### Quiet by default

A per-PR state signature means you are pinged only when a PR's notable state actually changes, not every run. Each notification is one PR: the title is `owner/repo#N`, the subtitle names the reasons, and the body is the PR URL.

## Requirements

- macOS (for notifications). [`terminal-notifier`](https://github.com/julienXX/terminal-notifier) makes notifications clickable (they open the PR); without it, delivery falls back to `osascript` with the URL in the body.
- The [`gh` CLI](https://cli.github.com/), authenticated with `repo` and `workflow` scopes (`workflow` is needed to re-run checks).
- Python 3.11 or newer (standard library only, no `pip install` required to run).

```bash
brew install gh terminal-notifier
gh auth login
```

## Install

```bash
git clone https://github.com/zkoppert/babysit-prs.git ~/repos/babysit-prs
```

Run it once by hand to see what it would do, without changing anything:

```bash
python3 ~/repos/babysit-prs/babysit_prs.py --dry-run --verbose
```

## Usage

```text
babysit_prs.py [--owner OWNER] [--active-days N] [--allowed-repo OWNER/REPO]
               [--skip-repo OWNER/REPO] [--dry-run] [--no-notify]
               [--state-file PATH] [--verbose]
```

| Flag | Default | Meaning |
| --- | --- | --- |
| `--owner OWNER` | all | Limit to PRs in this org/user owner. Repeatable. |
| `--active-days N` | `14` | Only watch PRs updated in the last N days (0 = no limit). |
| `--allowed-repo OWNER/REPO` | all | Restrict to specific repos. Repeatable. |
| `--skip-repo OWNER/REPO` | none | Never act on a repo (for example a fixture). Repeatable. |
| `--dry-run` | off | Preview only: no re-runs, branch updates, or notifications. |
| `--no-notify` | off | Act on checks and branches, but send no notifications. |
| `--state-file PATH` | `~/Library/Logs/babysit-prs-state.json` | Per-PR de-dup state. |
| `--verbose` | off | Debug logging. |

Examples:

```bash
# Watch every open PR you authored, updated in the last two weeks.
python3 babysit_prs.py

# Limit to two orgs and skip a fixture repo.
python3 babysit_prs.py --owner my-org --owner my-other-org --skip-repo my-org/fixture
```

## Run it on a schedule

### macOS (launchd)

Copy the wrapper and plist templates from [`examples/`](examples/), edit the paths (and any `--owner` flags) for your machine, then load the job:

```bash
cp examples/babysit-prs ~/.local/bin/babysit-prs && chmod +x ~/.local/bin/babysit-prs
cp examples/com.example.babysit-prs.plist ~/Library/LaunchAgents/com.example.babysit-prs.plist
# edit both files to replace the placeholder paths
launchctl load -w ~/Library/LaunchAgents/com.example.babysit-prs.plist
```

The example runs every 15 minutes. Logs go to `~/Library/Logs/babysit-prs.log`.

### cron

Invoke the wrapper (see [`examples/babysit-prs`](examples/babysit-prs)) rather than a bare `python3`, so the job uses your Python 3.11+ interpreter and finds `gh` on `PATH`:

```cron
*/15 * * * * $HOME/.local/bin/babysit-prs --owner my-org
```

## How de-duping works

State lives in a small JSON file mapping each PR URL to `{rerun_head, update_head, last_activity, notified_sig}`:

- `rerun_head` / `update_head`: the head commit a re-run or branch update was last attempted for, so each is tried once per commit.
- `last_activity`: the newest human review or comment timestamp seen, for new-comment detection.
- `notified_sig`: the persistent alert signature last notified, so an unchanged state stays quiet and an alert that transiently disappears and reappears on the same commit is not re-notified.

The state file is written atomically, and a non-blocking lock means overlapping runs skip cleanly instead of racing.

## Limitations and notes

- Notifications are macOS only. The scheduling and GitHub work are portable; the notifier is not yet.
- Required checks are matched by context name. In the rare case where a required check is bound to a specific GitHub App and an unrelated failing check shares its name, the tool may re-run the wrong workflow. When the required set cannot be read at all, CI actions are skipped rather than guessed.
- The scan is bounded by `--active-days` (default 14) and capped at 1000 results, which is far more than an active window normally contains.

## Using it with GitHub Copilot CLI

If you use the [GitHub Copilot CLI](https://github.com/github/copilot-cli), `SKILL.md` lets you drive this by asking, for example, "babysit my PRs". Symlink the repo into your skills directory:

```bash
ln -s ~/repos/babysit-prs ~/.copilot/skills/babysit-prs
```

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). Issues and pull requests are welcome.

## License

[MIT](LICENSE)
