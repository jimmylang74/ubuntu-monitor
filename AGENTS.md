# Repository instructions for coding agents

## Commit messages

Every commit message MUST include the list of files changed in that commit, grouped as `Added` and `Modified` (and `Deleted` when applicable), under a `Files:` heading.

This is automated by the `prepare-commit-msg` hook in `scripts/git/`, enabled via local git config:

```bash
git config core.hooksPath scripts/git
```

Do not remove or bypass this hook. When creating commits, stage the intended files and use a normal `git commit`; the hook appends the file list automatically. If you write the message manually, still include the same `Files:` section.
