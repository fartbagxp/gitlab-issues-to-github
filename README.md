# Gitlab to Github Issues

This project downloads a set of issues from a Gitlab repositories and migrates it to Github using a simple python script.

This repository exists because I couldn't find [a practical guide from Github](https://docs.github.com/en/migrations/using-ghe-migrator/about-ghe-migrator) to pull Gitlab issues into Github as a mirror after a code repository migration using `git --mirror`.

## How to Run

Install [uv](https://github.com/astral-sh/uv?tab=readme-ov-file#installation).

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
