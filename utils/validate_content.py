#!/usr/bin/python3
"""
Simple content validation for OMI Public Course.
Checks for required Markdown files and validates image references.
Uses `problems.json` to locate problems and git diff to only check changed
problems.
"""

import json
import os
import re
import sys
import subprocess
import logging
from typing import Any, Dict, List, cast

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
LOG = logging.getLogger(__name__)


def get_changed_files(repo_root: str) -> List[str]:
    """Get list of changed files using git diff."""
    # Try to get commit range from environment variables
    env = os.environ
    commit_range = None

    if env.get('TRAVIS_COMMIT_RANGE'):
        commit_range = env['TRAVIS_COMMIT_RANGE']
    elif env.get('CIRCLE_COMPARE_URL'):
        commit_range = env['CIRCLE_COMPARE_URL'].split('/')[6]
    elif env.get('GITHUB_BASE_COMMIT'):
        commit_range = env['GITHUB_BASE_COMMIT'] + '...HEAD'
    else:
        # Default to comparing with the main branch
        commit_range = 'origin/main...HEAD'

    try:
        changes = subprocess.check_output(
            ['git', 'diff', '--name-only', '--diff-filter=AMDR', commit_range],
            cwd=repo_root,
            universal_newlines=True)
        return changes.splitlines()
    except subprocess.CalledProcessError as e:
        LOG.error(f"Failed to get git diff: {e}")
        return []


def load_problems_from_json(repo_root: str) -> List[Dict[str, Any]]:
    """Load problem paths from problems.json file."""
    problems_json_path = os.path.join(repo_root, "problems.json")

    if not os.path.exists(problems_json_path):
        raise FileNotFoundError(
            f"problems.json not found at {problems_json_path}")

    with open(problems_json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    return cast(List[Dict[str, Any]], data.get("problems", []))


def validate_markdown_files(
        problem_path: str,
        problem_title: str
) -> List[str]:
    """Check if required Markdown files exist."""
    errors = []

    # At least one statement file must exist
    statement_files = [
        'statements/es.markdown',
        'statements/en.markdown'
    ]

    statement_exists = any(
        os.path.exists(os.path.join(problem_path, f)) for f in statement_files)
    if not statement_exists:
        errors.append(
            f"Problem '{problem_title}': Missing at least one statement file "
            f"(es or en)"
        )

    return errors


def validate_image_references(
        repo_root: str,
        problem_path: str,
        problem_title: str
) -> List[str]:
    """Check if image references in Markdown files point to existing files."""
    errors = []

    # Find all Markdown files
    for root, dirs, files in os.walk(problem_path):
        for file in files:
            if file.endswith('.markdown') or file.endswith('.md'):
                markdown_file = os.path.join(root, file)
                errors.extend(_check_file_images(repo_root,
                                                 markdown_file,
                                                 problem_title))

    return errors


def _check_file_images(
        repo_root: str,
        markdown_file: str,
        problem_title: str
) -> List[str]:
    """Check image references in a single Markdown file."""
    errors = []

    try:
        with open(markdown_file, 'r', encoding='utf-8') as f:
            content = f.read()
    except UnicodeDecodeError:
        errors.append(
            f"Problem '{problem_title}': Cannot read {markdown_file} "
            f"(encoding issue)"
        )
        return errors

    # Find image references: ![alt text](image.png)
    image_pattern = r'!\[([^\]]*)\]\(([^)]+)\)'
    matches = re.findall(image_pattern, content)

    file_dir = os.path.dirname(markdown_file)

    for alt_text, image_path in matches:
        # Skip URLs and absolute paths
        if image_path.startswith('http') or image_path.startswith('/'):
            continue

        # Handle relative paths
        if image_path.startswith('./'):
            image_path = image_path[2:]

        # Resolve relative to markdown file
        full_image_path = os.path.join(file_dir, image_path)

        if not os.path.exists(full_image_path):
            errors.append(
                f"Problem '{problem_title}': Image not found: {image_path} "
                f"(in {os.path.relpath(markdown_file, repo_root)})"
            )

    return errors


def main() -> None:
    """Main validation function."""
    try:
        # Get repository root (assuming script is in utils/ directory)
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        LOG.info(f"Repository root: {repo_root}")

        # Get changed files
        changed_files = get_changed_files(repo_root)
        LOG.info(f"\nFound {len(changed_files)} changed files in git diff")

        # Load problems from problems.json
        problems = load_problems_from_json(repo_root)
        LOG.info(f"Found {len(problems)} problems in problems.json")

        if not problems:
            LOG.error("❌ No problems found in problems.json")
            sys.exit(1)

        all_errors = []
        checked_problems = 0

        LOG.info("\n🔍 Validating changed problems...")

        # Validate each problem if it has changes
        for problem in problems:
            problem_path = problem["path"]
            full_problem_path = os.path.join(repo_root, problem_path)
            problem_title = os.path.basename(problem_path)

            # Check if this problem has any changes
            if not any(f.startswith(problem_path) for f in changed_files):
                LOG.info(f"\n⏩ Skipping problem: {problem_title} (no changes)")
                continue

            checked_problems += 1
            LOG.info(f"\n📝 Checking problem: {problem_title}")
            LOG.info(f"   Path: {problem_path}")
            LOG.info(f"   Full path: {full_problem_path}")

            if not os.path.exists(full_problem_path):
                error_msg = f"Problem path does not exist: {full_problem_path}"
                LOG.error(f"   ❌ {error_msg}")
                all_errors.append(error_msg)
                continue

            # Check Markdown files
            markdown_errors = validate_markdown_files(full_problem_path,
                                                      problem_title)
            if markdown_errors:
                LOG.info("   Missing files:")
                for error in markdown_errors:
                    LOG.error(f"      ❌ {error}")
            all_errors.extend(markdown_errors)

            # Check image references
            image_errors = validate_image_references(repo_root,
                                                     full_problem_path,
                                                     problem_title)
            if image_errors:
                LOG.info("   Image issues:")
                for error in image_errors:
                    LOG.error(f"      ❌ {error}")
            all_errors.extend(image_errors)

            if not markdown_errors and not image_errors:
                LOG.info("   ✅ No issues found")

        # Report final results
        if all_errors:
            LOG.error("\n❌ Validation summary:")
            LOG.info(
                f"Found {len(all_errors)} error(s) in {checked_problems} "
                f"changed problem(s):"
            )
            for error in all_errors:
                LOG.info(f"   • {error}")
            sys.exit(1)
        else:
            LOG.info("\n✅ All validations passed!")
            LOG.info(f"   📊 Checked {checked_problems} changed problem(s)")
            sys.exit(0)

    except Exception as e:
        LOG.error(f"❌ Validation failed: {str(e)}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
