"""Polaris baseline .gitignore — the safety net the agent-side `polaris
prepublish-audit` enforces on.

Written into the project root *after* Codex scaffolds, i.e. when the
`set_project_root` dynamic tool fires.  Doing it at workspace-init time
breaks scaffolders that demand an empty cwd (`npm create vite .`,
`create-react-app .`, …).  If the scaffolder already produced a
`.gitignore`, we append any baseline lines it's missing — we do not
overwrite user / scaffolder content.
"""

from pathlib import Path

# Grouped so the merge logic below can report WHICH group a missing
# line belongs to.  Order here is the order we'll append missing groups.
BASELINE_GITIGNORE_GROUPS: list[tuple[str, list[str]]] = [
    (
        "secrets",
        [
            ".env",
            ".env.*",
            "!.env.example",
            "*.pem",
            "*.key",
            "*.p12",
            "*.pfx",
            "credentials*",
            ".secrets/",
            "id_rsa",
            "id_rsa.pub",
            # polaris publish-time secrets volume injection target
            ".env.polaris.prod",
        ],
    ),
    (
        "node",
        [
            "node_modules/",
            ".next/",
            "dist/",
            "build/",
            ".cache/",
            ".turbo/",
        ],
    ),
    (
        "python",
        [
            "__pycache__/",
            "*.pyc",
            ".venv/",
            "venv/",
            ".pytest_cache/",
            ".ruff_cache/",
            ".mypy_cache/",
        ],
    ),
    (
        "editors-os",
        [".DS_Store", ".vscode/", ".idea/"],
    ),
    (
        "runtime",
        ["tmp/", "logs/", "*.log"],
    ),
    (
        "polaris",
        [".polaris-build/"],
    ),
]


def render_baseline(groups: list[tuple[str, list[str]]] | None = None) -> str:
    groups = groups or BASELINE_GITIGNORE_GROUPS
    lines = ["# Baseline polaris .gitignore (written by set_project_root)."]
    for label, patterns in groups:
        lines.append("")
        lines.append(f"# {label}")
        lines.extend(patterns)
    lines.append("")
    return "\n".join(lines) + "\n"


def ensure_baseline_gitignore(project_root: Path) -> None:
    """Create or merge a `.gitignore` at `project_root`.

    * If no file exists: write the full baseline.
    * If a file exists: append any baseline patterns it's missing under a
      trailing `# polaris baseline` section.  Existing lines are NEVER
      touched, reordered, or deduped — we only add.
    """
    gitignore = project_root / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(render_baseline())
        return

    existing = gitignore.read_text()
    existing_lines = {line.strip() for line in existing.splitlines()}
    missing: list[tuple[str, list[str]]] = []
    for label, patterns in BASELINE_GITIGNORE_GROUPS:
        absent = [p for p in patterns if p.strip() not in existing_lines]
        if absent:
            missing.append((label, absent))

    if not missing:
        return

    appended = ["", "# ── polaris baseline ─────────"]
    for label, patterns in missing:
        appended.append(f"# {label}")
        appended.extend(patterns)
    sep = "" if existing.endswith("\n") else "\n"
    gitignore.write_text(existing + sep + "\n".join(appended) + "\n")
