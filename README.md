# Gitlab to Github Issues

This project downloads a set of issues from a Gitlab repositories and migrates it to Github.

## How to Run

Install `uv`.

Example Commands:

```bash
  python main.py fetch                      # Fetch issues from GitLab
  python main.py migrate                    # Migrate all issues to GitHub
  python main.py migrate --closed-only      # Test migration on closed issues only
  python main.py migrate --open-only        # Migrate only open issues
  python main.py migrate --file data/custom_file.json  # Migrate from specific file
```

## Open Issues

1. Issues comments are migrated in reverse order (oldest comments are commented first).
1. This repository does not capture images and other attachments.
