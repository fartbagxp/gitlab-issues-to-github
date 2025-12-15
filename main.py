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
import hashlib
import json
import mimetypes
import os
import re
import sys

from datetime import datetime
from dotenv import load_dotenv
from pathlib import Path
from github import Github
from urllib.parse import urljoin, urlparse

def create_proxied_session(host: str, port: int, user: str = "", password: str = "", ssl_verify=True):
    """Create a requests session configured to use SOCKS5 proxy with remote DNS.

    Args:
        host: SOCKS5 proxy host
        port: SOCKS5 proxy port
        user: Optional username for proxy authentication
        password: Optional password for proxy authentication
        ssl_verify: SSL verification - True, False, or path to CA bundle

    Returns:
        A configured requests.Session object

    Note:
        Uses socks5h:// scheme which performs DNS resolution through the proxy
        (remote DNS) instead of locally. This is necessary when accessing hosts
        that are only resolvable on the remote network.
    """
    import requests

    # Build proxy URL with remote DNS (socks5h://)
    # The 'h' suffix tells the SOCKS client to resolve hostnames remotely
    if user and password:
        proxy_url = f"socks5h://{user}:{password}@{host}:{port}"
    else:
        proxy_url = f"socks5h://{host}:{port}"

    # Create session with proxy configuration
    session = requests.Session()
    session.proxies = {
        'http': proxy_url,
        'https': proxy_url,
    }

    # Set SSL verification
    session.verify = ssl_verify

    print(f"SOCKS5 proxy configured: {host}:{port} (with remote DNS)")
    return session


def extract_image_urls(text: str, base_url: str) -> list[tuple[str, str]]:
    """Extract image URLs from markdown text.

    Args:
        text: Markdown text to search
        base_url: Base URL for resolving relative URLs (should be the issue/project URL)

    Returns:
        List of tuples (original_markdown, absolute_url)
    """
    if not text:
        return []

    def resolve_url(url: str, base: str) -> str:
        """Resolve a URL relative to a base URL.

        Special handling for GitLab upload paths that start with /uploads/
        which should be relative to the project, not the domain root.
        """
        # If URL starts with /uploads/, it's relative to the project root
        if url.startswith('/uploads/'):
            # Append to the base URL instead of replacing
            return base.rstrip('/') + url
        # Otherwise use standard urljoin
        return urljoin(base, url)

    images = []

    # Match markdown images: ![alt](url)
    markdown_pattern = r'!\[([^\]]*)\]\(([^)]+)\)'
    for match in re.finditer(markdown_pattern, text):
        # alt_text = match.group(1)
        url = match.group(2)
        # Convert relative URLs to absolute using the base URL
        absolute_url = resolve_url(url, base_url)
        original_markdown = match.group(0)
        images.append((original_markdown, absolute_url))

    # Match HTML images: <img src="url">
    html_pattern = r'<img[^>]+src=["\']([^"\']+)["\'][^>]*>'
    for match in re.finditer(html_pattern, text):
        url = match.group(1)
        absolute_url = resolve_url(url, base_url)
        original_markdown = match.group(0)
        images.append((original_markdown, absolute_url))

    # Also match attachment links that might be images: [filename](url)
    # This catches regular markdown links to images
    link_pattern = r'\[([^\]]+)\]\(([^)]+\.(?:png|jpg|jpeg|gif|svg|webp|bmp))\)'
    for match in re.finditer(link_pattern, text, re.IGNORECASE):
        url = match.group(2)
        absolute_url = resolve_url(url, base_url)
        original_markdown = match.group(0)
        # Only add if not already captured
        if (original_markdown, absolute_url) not in images:
            images.append((original_markdown, absolute_url))

    return images


def download_image(url: str, save_dir: Path, session=None, gitlab_token: str = None) -> tuple[str, Path]:
    """Download an image from a URL.

    Args:
        url: Image URL to download
        save_dir: Directory to save the image
        session: Optional requests session (for proxy/auth)
        gitlab_token: Optional GitLab private token for authentication

    Returns:
        Tuple of (original_url, local_file_path)
    """
    import requests

    # Create a hash of the URL to use as filename
    url_hash = hashlib.md5(url.encode()).hexdigest()

    # Try to get extension from URL
    parsed = urlparse(url)
    path = parsed.path
    ext = os.path.splitext(path)[1]

    # Prepare headers with authentication if token is provided
    headers = {}
    if gitlab_token:
        headers['PRIVATE-TOKEN'] = gitlab_token

    # Download the image
    if session:
        response = session.get(url, headers=headers, timeout=30)
    else:
        response = requests.get(url, headers=headers, timeout=30)

    response.raise_for_status()

    # Check content type to verify we got what we expected
    content_type = response.headers.get('content-type', '').lower()
    content_length = len(response.content)

    # Detect if we got an HTML error page instead of an image
    if 'text/html' in content_type:
        # We got an HTML page, likely an error or login page
        error_preview = response.text[:500] if len(response.text) > 500 else response.text
        raise Exception(
            f"Received HTML instead of file (likely authentication error or 404).\n"
            f"Content-Type: {content_type}\n"
            f"Size: {content_length} bytes\n"
            f"Preview: {error_preview}"
        )

    # Try to determine extension from content-type if not in URL
    if not ext or ext not in ['.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.bmp', '.pdf', '.txt', '.zip', '.tar', '.gz']:
        ext = mimetypes.guess_extension(content_type) or '.bin'
        print(f"      Content-Type: {content_type}, using extension: {ext}")

    # Warn if file seems suspiciously small
    if content_length < 100:
        print(f"      Warning: File is very small ({content_length} bytes), may be corrupted")

    # Save the file
    filename = f"{url_hash}{ext}"
    filepath = save_dir / filename

    with open(filepath, 'wb') as f:
        f.write(response.content)

    print(f"      Downloaded {content_length} bytes, Content-Type: {content_type}")

    return url, filepath


def fetch_gitlab_issues(
    gitlab_url: str,
    gitlab_token: str,
    project_id: str,
    data_dir: Path,
    session=None,
    ssl_verify=True,
    include_closed: bool = True,
    migrate_images: bool = True,
) -> None:
    """
    Fetch all issues from GitLab and save them to JSON files.

    Args:
        gitlab_url: GitLab instance URL
        gitlab_token: GitLab personal access token
        project_id: GitLab project ID
        data_dir: Directory to save fetched data
        session: Optional requests.Session object (for proxy support)
        ssl_verify: SSL verification - True, False, or path to CA bundle
        include_closed: Whether to include closed issues
        migrate_images: Whether to download images referenced in issues
    """
    print(f"\nFetching issues from GitLab project {project_id}...")

    # Create data directory if it doesn't exist
    data_dir.mkdir(parents=True, exist_ok=True)

    # Create images directory if migrating images
    images_dir = data_dir / "images"
    if migrate_images:
        images_dir.mkdir(parents=True, exist_ok=True)

    # Connect to GitLab
    gl = gitlab.Gitlab(gitlab_url, private_token=gitlab_token, session=session, ssl_verify=ssl_verify)
    gl.auth()
    print(f"Connected to GitLab as: {gl.user.username}")

    # Get project
    project = gl.projects.get(project_id)
    print(f"Project: {project.name}")

    # Get the authenticated session from gitlab client for downloading files
    # This session has cookies and proper authentication for file downloads
    authenticated_session = gl.session

    # Get project web URL for resolving relative image URLs
    # GitLab uploads are typically at: https://gitlab.com/group/project/uploads/...
    project_web_url = project.web_url
    print(f"Project URL: {project_web_url}")

    # Fetch issues
    state = "all" if include_closed else "opened"
    issues = project.issues.list(state=state, get_all=True)
    print(f"Found {len(issues)} issues")

    # Save issues to JSON files
    issues_data = []
    total_images_downloaded = 0

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
            "images": {},  # Map of original URL to local file path
        }

        # Download images and attachments from description
        if migrate_images and issue_dict["description"]:
            # Extract images (both image tags and attachment links)
            image_urls = extract_image_urls(issue_dict["description"], project_web_url)

            # Also extract any other attachment links (non-image files)
            attachment_pattern = r'\[([^\]]+)\]\(([^)]+\.[a-zA-Z0-9]+)\)'
            for match in re.finditer(attachment_pattern, issue_dict["description"]):
                url = match.group(2)
                # Use same resolve_url logic for consistency
                if url.startswith('/uploads/'):
                    absolute_url = project_web_url.rstrip('/') + url
                else:
                    absolute_url = urljoin(project_web_url, url)
                # Skip if already in images
                if not any(absolute_url == img_url for _, img_url in image_urls):
                    # Check if it looks like an upload path
                    if '/uploads/' in url or url.startswith('/uploads/'):
                        image_urls.append((match.group(0), absolute_url))

            if image_urls:
                print(f"    Found {len(image_urls)} attachment(s) in description")

            for original_md, img_url in image_urls:
                try:
                    print(f"    Downloading: {img_url}")
                    _, local_path = download_image(img_url, images_dir, authenticated_session, gitlab_token)
                    issue_dict["images"][img_url] = str(local_path.relative_to(data_dir))
                    total_images_downloaded += 1
                    print(f"    Saved to: {local_path.name}")
                except Exception as e:
                    print(f"    Warning: Failed to download {img_url}: {e}")

        # Fetch comments/notes
        notes = issue.notes.list(get_all=True)
        issue_dict["comments"] = []

        for note in notes:
            if getattr(note, "system", False):  # Skip system notes
                continue

            comment = {
                "author": note.author.get("username", "unknown"),
                "body": note.body,
                "created_at": note.created_at,
                "images": {},  # Map of original URL to local file path
                "attachments": {},  # Map of original URL to local file path
            }

            # Download images and attachments from comment
            if migrate_images and note.body:
                # Extract images (both image tags and attachment links)
                image_urls = extract_image_urls(note.body, project_web_url)

                # Also extract any other attachment links (non-image files)
                attachment_pattern = r'\[([^\]]+)\]\(([^)]+\.[a-zA-Z0-9]+)\)'
                for match in re.finditer(attachment_pattern, note.body):
                    url = match.group(2)
                    # Use same resolve_url logic for consistency
                    if url.startswith('/uploads/'):
                        absolute_url = project_web_url.rstrip('/') + url
                    else:
                        absolute_url = urljoin(project_web_url, url)
                    # Skip if already in images
                    if not any(absolute_url == img_url for _, img_url in image_urls):
                        # Check if it looks like an upload path
                        if '/uploads/' in url or url.startswith('/uploads/'):
                            image_urls.append((match.group(0), absolute_url))

                if image_urls:
                    print(f"    Found {len(image_urls)} attachment(s) in comment by {comment['author']}")

                for original_md, img_url in image_urls:
                    try:
                        print(f"      Downloading: {img_url}")
                        _, local_path = download_image(img_url, images_dir, authenticated_session, gitlab_token)
                        # Store in images dict (used for both images and attachments)
                        comment["images"][img_url] = str(local_path.relative_to(data_dir))
                        total_images_downloaded += 1
                        print(f"      Saved to: {local_path.name}")
                    except Exception as e:
                        print(f"      Warning: Failed to download {img_url}: {e}")

            issue_dict["comments"].append(comment)

        issues_data.append(issue_dict)
        image_count = len(issue_dict["images"]) + sum(len(c.get("images", {})) for c in issue_dict["comments"])
        if image_count > 0:
            print(f"  Fetched issue #{issue.iid}: {issue.title} ({image_count} images)")
        else:
            print(f"  Fetched issue #{issue.iid}: {issue.title}")

    # Save to JSON file
    output_file = data_dir / f"gitlab_issues_{project_id}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(issues_data, f, indent=2, ensure_ascii=False)

    print(f"\nSuccessfully saved {len(issues_data)} issues to: {output_file}")
    if migrate_images and total_images_downloaded > 0:
        print(f"Downloaded {total_images_downloaded} images to: {images_dir}")

    # Also save a "latest" file for easy reference
    latest_file = data_dir / f"gitlab_issues_{project_id}_latest.json"
    with open(latest_file, "w", encoding="utf-8") as f:
        json.dump(issues_data, f, indent=2, ensure_ascii=False)
    print(f"Also saved to: {latest_file}")


def upload_image_to_github(repo, image_path: Path, github_path: str) -> str:
    """Upload an image to a GitHub repository.

    Args:
        repo: PyGithub Repository object
        image_path: Local path to the image file
        github_path: Path in the repository where the image should be stored

    Returns:
        URL to the uploaded image
    """
    with open(image_path, "rb") as f:
        content = f.read()

    # Check if file already exists
    try:
        existing = repo.get_contents(github_path)
        # File exists, update it
        repo.update_file(
            path=github_path,
            message=f"Update migrated image: {image_path.name}",
            content=content,
            sha=existing.sha
        )
    except Exception:
        # File doesn't exist, create it
        repo.create_file(
            path=github_path,
            message=f"Add migrated image: {image_path.name}",
            content=content
        )

    # Return the raw URL to the image
    return f"https://raw.githubusercontent.com/{repo.full_name}/main/{github_path}"


def migrate_to_github(
    github_token: str,
    github_owner: str,
    github_repo: str,
    data_file: Path,
    preserve_labels: bool = True,
    add_migration_note: bool = True,
    migrate_images: bool = True,
    github_images_path: str = "migrated-images",
    closed_only: bool = False,
    open_only: bool = False,
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
        migrate_images: Whether to upload and migrate images
        github_images_path: Path in GitHub repo to store images
        closed_only: Only migrate closed issues
        open_only: Only migrate open issues
    """
    print(f"\nMigrating issues to GitHub repository {github_owner}/{github_repo}...")

    # Load issues from JSON
    with open(data_file, "r", encoding="utf-8") as f:
        issues_data = json.load(f)

    # Filter issues if requested
    original_count = len(issues_data)
    if closed_only:
        issues_data = [issue for issue in issues_data if issue["state"] == "closed"]
        print(f"Filtered to {len(issues_data)} closed issues (out of {original_count} total)")
    elif open_only:
        issues_data = [issue for issue in issues_data if issue["state"] == "opened"]
        print(f"Filtered to {len(issues_data)} open issues (out of {original_count} total)")
    else:
        print(f"Loaded {len(issues_data)} issues from {data_file}")

    # Connect to GitHub
    gh = Github(github_token)
    repo = gh.get_repo(f"{github_owner}/{github_repo}")
    print(f"Connected to GitHub repository: {repo.full_name}")

    # Get data directory (parent of data_file)
    data_dir = data_file.parent

    # Get existing labels if preserving
    existing_labels = {}
    if preserve_labels:
        for label in repo.get_labels():
            existing_labels[label.name] = label

    # Migrate each issue
    migrated_count = 0
    total_images_uploaded = 0

    for issue_data in issues_data:
        print(f"\nMigrating issue #{issue_data['iid']}: {issue_data['title']}")

        # Prepare issue body
        body = issue_data["description"]

        # Upload images and replace URLs in description
        if migrate_images and issue_data.get("images"):
            for original_url, local_path_str in issue_data["images"].items():
                local_path = data_dir / local_path_str
                if not local_path.exists():
                    print(f"  Warning: Image file not found: {local_path}")
                    continue

                try:
                    # Upload to GitHub
                    github_path = f"{github_images_path}/{local_path.name}"
                    github_url = upload_image_to_github(repo, local_path, github_path)

                    # Replace URL in body
                    body = body.replace(original_url, github_url)
                    total_images_uploaded += 1
                    print(f"  Uploaded image: {local_path.name}")
                except Exception as e:
                    print(f"  Warning: Failed to upload image {local_path.name}: {e}")

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
                comment_body = comment['body']

                # Upload images and replace URLs in comment
                if migrate_images and comment.get("images"):
                    for original_url, local_path_str in comment["images"].items():
                        local_path = data_dir / local_path_str
                        if not local_path.exists():
                            print(f"    Warning: Image file not found: {local_path}")
                            continue

                        try:
                            # Upload to GitHub
                            github_path = f"{github_images_path}/{local_path.name}"
                            github_url = upload_image_to_github(repo, local_path, github_path)

                            # Replace URL in comment body
                            comment_body = comment_body.replace(original_url, github_url)
                            total_images_uploaded += 1
                            print(f"    Uploaded image: {local_path.name}")
                        except Exception as e:
                            print(f"    Warning: Failed to upload image {local_path.name}: {e}")

                # Add author attribution
                comment_body = (
                    f"*Comment by @{comment['author']} at {comment['created_at']}:*\n\n"
                    f"{comment_body}"
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
    if migrate_images and total_images_uploaded > 0:
        print(f"Uploaded {total_images_uploaded} images to {github_images_path}/")
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
  python main.py fetch                      # Fetch issues from GitLab
  python main.py migrate                    # Migrate all issues to GitHub
  python main.py migrate --closed-only      # Test migration on closed issues only
  python main.py migrate --open-only        # Migrate only open issues
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
    parser.add_argument(
        "--closed-only",
        action="store_true",
        help="Only migrate closed issues (useful for testing)"
    )
    parser.add_argument(
        "--open-only",
        action="store_true",
        help="Only migrate open issues"
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
            proxy_user = os.getenv("SOCKS5_PROXY_USER", "")
            proxy_pass = os.getenv("SOCKS5_PROXY_PASS", "")

            # Validate required variables
            if not all([gitlab_url, gitlab_token, gitlab_project_id]):
                print("Error: Missing required GitLab configuration in .env file")
                print("Required: GITLAB_URL, GITLAB_TOKEN, GITLAB_PROJECT_ID")
                sys.exit(1)

            if not all([proxy_host, proxy_port]):
                print("Error: Missing required SOCKS5 proxy configuration in .env file")
                print("Required: SOCKS5_PROXY_HOST, SOCKS5_PROXY_PORT")
                sys.exit(1)

            # SSL verification configuration
            ssl_verify_str = os.getenv("GITLAB_SSL_VERIFY", "true").lower()
            if ssl_verify_str == "false":
                ssl_verify = False
                print("Warning: SSL certificate verification is disabled")
            elif ssl_verify_str == "true":
                ssl_verify = True
            else:
                # Treat as path to CA bundle
                ssl_verify = ssl_verify_str
                print(f"Using custom CA bundle: {ssl_verify}")

            # Create proxied session with SSL settings
            proxied_session = create_proxied_session(
                proxy_host, int(proxy_port), proxy_user, proxy_pass, ssl_verify
            )

            # Fetch issues
            include_closed = os.getenv("INCLUDE_CLOSED_ISSUES", "true").lower() == "true"
            migrate_images = os.getenv("MIGRATE_IMAGES", "true").lower() == "true"

            fetch_gitlab_issues(
                gitlab_url,
                gitlab_token,
                gitlab_project_id,
                data_dir,
                session=proxied_session,
                ssl_verify=ssl_verify,
                include_closed=include_closed,
                migrate_images=migrate_images,
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
            migrate_images = os.getenv("MIGRATE_IMAGES", "true").lower() == "true"
            github_images_path = os.getenv("GITHUB_IMAGES_PATH", "migrated-images")

            # Migrate issues
            migrate_to_github(
                github_token,
                github_owner,
                github_repo,
                data_file,
                preserve_labels,
                add_migration_note,
                migrate_images,
                github_images_path,
                closed_only=args.closed_only,
                open_only=args.open_only,
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
