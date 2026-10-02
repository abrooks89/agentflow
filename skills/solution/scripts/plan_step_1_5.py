"""Step 1.5 helper: inject slug and depends_on into plan frontmatter."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Set

import yaml


def apply_step_1_5(plan_path: Path, slug_validation_against: Set[str]) -> None:
    """
    Inject slug and depends_on into plan frontmatter.

    Args:
        plan_path: Path to the plan file
        slug_validation_against: Set of valid slugs (from disk scan). Empty set allows future epics.
    """
    text = plan_path.read_text(encoding="utf-8")

    # Extract filename and derive slug
    filename = plan_path.name
    # Remove date prefix (YYYY-MM-DD-) and .md suffix
    slug = re.sub(r"^\d{4}-\d{2}-\d{2}-", "", filename)
    slug = re.sub(r"\.md$", "", slug)
    # Remove -design suffix if present
    slug = re.sub(r"-design$", "", slug)

    # Parse frontmatter
    frontmatter_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not frontmatter_match:
        raise ValueError(f"No YAML frontmatter found in {plan_path}")

    frontmatter_text = frontmatter_match.group(1)
    rest_of_file = text[frontmatter_match.end() :]

    # Load and update frontmatter
    frontmatter = yaml.safe_load(frontmatter_text)
    if frontmatter is None:
        frontmatter = {}

    # Add slug
    frontmatter["slug"] = slug

    # Ask user for depends_on
    response = input(
        "Does this plan depend on any other epics? List slugs of specs/plans/statuses "
        "that must complete before this one. Reply with comma-separated slugs or 'none'.\n"
        "(Pre-detected from doc body: []) "
    )

    depends_on = []
    if response.strip().lower() != "none":
        # Parse comma-separated slugs
        for s in response.split(","):
            s = s.strip()
            if not s:
                continue

            # Validate slug and re-prompt if user chooses to correct
            while True:
                if s not in slug_validation_against and slug_validation_against:
                    # Only validate if slug_validation_against is non-empty
                    user_choice = input(
                        f"Slug '{s}' not found on disk. Continue anyway "
                        f"(it's a future epic) or correct? [continue|correct] "
                    )
                    if user_choice.strip().lower() == "correct":
                        # Re-prompt for corrected slug
                        s = input("Enter the correct slug: ").strip()
                        # Loop back to validate the new slug
                        continue
                # Either slug is valid or user chose to continue with unknown slug
                break

            depends_on.append(s)

    frontmatter["depends_on"] = depends_on

    # Serialize back to YAML
    new_frontmatter = yaml.dump(
        frontmatter, default_flow_style=False, sort_keys=False
    )
    new_text = f"---\n{new_frontmatter}---\n{rest_of_file}"

    plan_path.write_text(new_text, encoding="utf-8")
