#!/usr/bin/env python3
"""
GitLab to GitHub Issue Migration Tool

This tool migrates issues from GitLab to GitHub in two stages:
1. Fetch: Download all issues from GitLab (via SOCKS5 proxy) to local JSON files
2. Migrate: Upload the issues from JSON files to GitHub

Usage:
    python main.py fetch   # Fetch issues from GitLab
    python main.py migrate # Migrate issues to GitHub
"""

import argparse
import gitlab
import json
import os
import sys

from datetime import datetime
from dotenv import load_dotenv
from pathlib import Path
from github import Github

def setup_socks_proxy(host: str, port: int, user: str = "", password: str = "") -> None:
    """Configure SOCKS5 proxy for requests."""
    import socks
    import socket

    if user and password:
        socks.set_default_proxy(
            socks.SOCKS5, host, port, username=user, password=password
        )
    else:
        socks.set_default_proxy(socks.SOCKS5, host, port)

    socket.socket = socks.socksocket
    print(f"SOCKS5 proxy configured: {host}:{port}")


def fetch_gitlab_issues(
    gitlab_url: str,
    gitlab_token: str,
    project_id: str,
    data_dir: Path,
    include_closed: bool = True,
) -> None:
    """
    Fetch all issues from GitLab and save them to JSON files.

    Args:
        gitlab_url: GitLab instance URL
        gitlab_token: GitLab personal access token
        project_id: GitLab project ID
        data_dir: Directory to save fetched data
        include_closed: Whether to include closed issues
    """
    print(f"\nFetching issues from GitLab project {project_id}...")

    # Create data directory if it doesn't exist
    data_dir.mkdir(parents=True, exist_ok=True)

    # Connect to GitLab
    gl = gitlab.Gitlab(gitlab_url, private_token=gitlab_token)
    gl.auth()
    print(f"Connected to GitLab as: {gl.user.username}")

    # Get project
    project = gl.projects.get(project_id)
    print(f"Project: {project.name}")

    # Fetch issues
    state = "all" if include_closed else "opened"
    issues = project.issues.list(state=state, get_all=True)
    print(f"Found {len(issues)} issues")

    # Save issues to JSON files
    issues_data = []
    for issue in issues:
        issue_dict = {
            "iid": issue.iid,
            "title": issue.title,
            "description": issue.description or "",
            "state": issue.state,
            "created_at": issue.created_at,
            "updated_at": issue.updated_at,
            "closed_at": getattr(issue, "closed_at", None),
            "labels": issue.labels,
            "author": issue.author.get("username", "unknown"),
            "assignees": [a.get("username") for a in getattr(issue, "assignees", [])],
            "milestone": issue.milestone.get("title") if issue.milestone else None,
            "web_url": issue.web_url,
        }

        # Fetch comments/notes
        notes = issue.notes.list(get_all=True)
        issue_dict["comments"] = [
            {
                "author": note.author.get("username", "unknown"),
                "body": note.body,
                "created_at": note.created_at,
            }
            for note in notes
            if not getattr(note, "system", False)  # Skip system notes
        ]

        issues_data.append(issue_dict)
        print(f"  Fetched issue #{issue.iid}: {issue.title}")

    # Save to JSON file
    output_file = data_dir / f"gitlab_issues_{project_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(issues_data, f, indent=2, ensure_ascii=False)

    print(f"\nSuccessfully saved {len(issues_data)} issues to: {output_file}")

    # Also save a "latest" file for easy reference
    latest_file = data_dir / f"gitlab_issues_{project_id}_latest.json"
    with open(latest_file, "w", encoding="utf-8") as f:
        json.dump(issues_data, f, indent=2, ensure_ascii=False)
    print(f"Also saved to: {latest_file}")


def migrate_to_github(
    github_token: str,
    github_owner: str,
    github_repo: str,
    data_file: Path,
    preserve_labels: bool = True,
    add_migration_note: bool = True,
) -> None:
    """
    Migrate issues from JSON file to GitHub.

    Args:
        github_token: GitHub personal access token
        github_owner: GitHub repository owner
        github_repo: GitHub repository name
        data_file: Path to JSON file with issue data
        preserve_labels: Whether to create/assign labels
        add_migration_note: Whether to add a note about migration
    """
    print(f"\nMigrating issues to GitHub repository {github_owner}/{github_repo}...")

    # Load issues from JSON
    with open(data_file, "r", encoding="utf-8") as f:
        issues_data = json.load(f)
    print(f"Loaded {len(issues_data)} issues from {data_file}")

    # Connect to GitHub
    gh = Github(github_token)
    repo = gh.get_repo(f"{github_owner}/{github_repo}")
    print(f"Connected to GitHub repository: {repo.full_name}")

    # Get existing labels if preserving
    existing_labels = {}
    if preserve_labels:
        for label in repo.get_labels():
            existing_labels[label.name] = label

    # Migrate each issue
    migrated_count = 0
    for issue_data in issues_data:
        print(f"\nMigrating issue #{issue_data['iid']}: {issue_data['title']}")

        # Prepare issue body
        body = issue_data["description"]

        if add_migration_note:
            migration_note = (
                f"\n\n---\n"
                f"*Migrated from GitLab issue #{issue_data['iid']}*\n"
                f"*Original author: @{issue_data['author']}*\n"
                f"*Created at: {issue_data['created_at']}*\n"
                f"*Original URL: {issue_data['web_url']}*"
            )
            body = body + migration_note if body else migration_note.strip()

        # Create labels if needed
        labels_to_assign = []
        if preserve_labels and issue_data["labels"]:
            for label_name in issue_data["labels"]:
                if label_name not in existing_labels:
                    # Create new label
                    try:
                        new_label = repo.create_label(
                            name=label_name,
                            color="ededed",  # Default gray color
                            description="Migrated from GitLab"
                        )
                        existing_labels[label_name] = new_label
                        print(f"  Created label: {label_name}")
                    except Exception as e:
                        print(f"  Warning: Could not create label '{label_name}': {e}")
                        continue

                labels_to_assign.append(label_name)

        # Create the issue on GitHub
        try:
            gh_issue = repo.create_issue(
                title=issue_data["title"],
                body=body,
                labels=labels_to_assign,
            )
            print(f"  Created GitHub issue #{gh_issue.number}: {gh_issue.html_url}")

            # Add comments
            for comment in issue_data.get("comments", []):
                comment_body = (
                    f"*Comment by @{comment['author']} at {comment['created_at']}:*\n\n"
                    f"{comment['body']}"
                )
                gh_issue.create_comment(comment_body)
                print(f"    Added comment by {comment['author']}")

            # Close the issue if it was closed in GitLab
            if issue_data["state"] == "closed":
                gh_issue.edit(state="closed")
                print("  Closed issue (was closed in GitLab)")

            migrated_count += 1

        except Exception as e:
            print(f"  Error creating issue: {e}")
            continue

    print(f"\n{'='*60}")
    print(f"Migration complete! Migrated {migrated_count}/{len(issues_data)} issues")
    print(f"{'='*60}")


def main() -> None:
    """Main entry point."""
    # Load environment variables
    load_dotenv()

    # Parse arguments
    parser = argparse.ArgumentParser(
        description="Migrate GitLab issues to GitHub",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py fetch          # Fetch issues from GitLab
  python main.py migrate        # Migrate issues to GitHub
  python main.py migrate --file data/custom_file.json  # Migrate from specific file
        """
    )
    parser.add_argument(
        "command",
        choices=["fetch", "migrate"],
        help="Command to execute"
    )
    parser.add_argument(
        "--file",
        type=Path,
        help="JSON file to use for migration (default: latest file in data dir)"
    )

    args = parser.parse_args()

    # Get configuration from environment
    data_dir = Path(os.getenv("DATA_DIR", "data"))

    try:
        if args.command == "fetch":
            # Fetch stage configuration
            gitlab_url = os.getenv("GITLAB_URL")
            gitlab_token = os.getenv("GITLAB_TOKEN")
            gitlab_project_id = os.getenv("GITLAB_PROJECT_ID")

            # SOCKS5 proxy configuration
            proxy_host = os.getenv("SOCKS5_PROXY_HOST")
            proxy_port = os.getenv("SOCKS5_PROXY_PORT")

            # Validate required variables
            if not all([gitlab_url, gitlab_token, gitlab_project_id]):
                print("Error: Missing required GitLab configuration in .env file")
                print("Required: GITLAB_URL, GITLAB_TOKEN, GITLAB_PROJECT_ID")
                sys.exit(1)

            if not all([proxy_host, proxy_port]):
                print("Error: Missing required SOCKS5 proxy configuration in .env file")
                print("Required: SOCKS5_PROXY_HOST, SOCKS5_PROXY_PORT")
                sys.exit(1)

            # Setup SOCKS5 proxy
            setup_socks_proxy(proxy_host, int(proxy_port))

            # Fetch issues
            include_closed = os.getenv("INCLUDE_CLOSED_ISSUES", "true").lower() == "true"
            fetch_gitlab_issues(
                gitlab_url,
                gitlab_token,
                gitlab_project_id,
                data_dir,
                include_closed,
            )

        elif args.command == "migrate":
            # Migrate stage configuration
            github_token = os.getenv("GITHUB_TOKEN")
            github_owner = os.getenv("GITHUB_OWNER")
            github_repo = os.getenv("GITHUB_REPO")

            # Validate required variables
            if not all([github_token, github_owner, github_repo]):
                print("Error: Missing required GitHub configuration in .env file")
                print("Required: GITHUB_TOKEN, GITHUB_OWNER, GITHUB_REPO")
                sys.exit(1)

            # Determine which file to use
            if args.file:
                data_file = args.file
            else:
                # Use the latest file
                gitlab_project_id = os.getenv("GITLAB_PROJECT_ID", "*")
                latest_file = data_dir / f"gitlab_issues_{gitlab_project_id}_latest.json"

                if not latest_file.exists():
                    print(f"Error: No data file found at {latest_file}")
                    print("Run 'python main.py fetch' first or specify a file with --file")
                    sys.exit(1)

                data_file = latest_file

            if not data_file.exists():
                print(f"Error: Data file not found: {data_file}")
                sys.exit(1)

            # Migration options
            preserve_labels = os.getenv("PRESERVE_LABELS", "true").lower() == "true"
            add_migration_note = os.getenv("ADD_MIGRATION_NOTE", "true").lower() == "true"

            # Migrate issues
            migrate_to_github(
                github_token,
                github_owner,
                github_repo,
                data_file,
                preserve_labels,
                add_migration_note,
            )

    except KeyboardInterrupt:
        print("\n\nOperation cancelled by user")
        sys.exit(1)
    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
