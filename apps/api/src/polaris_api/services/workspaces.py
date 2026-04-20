import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path


class WorkspaceError(Exception):
    pass


class WorkspaceConflictError(WorkspaceError):
    pass


class WorkspacePathError(WorkspaceError):
    pass


@dataclass(frozen=True)
class GitCommit:
    branch: str
    commit_hash: str | None


async def run_git(repo_path: Path, *args: str, check: bool = True) -> str:
    process = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=repo_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    if check and process.returncode != 0:
        message = stderr.decode().strip() or stdout.decode().strip()
        raise WorkspaceError(message or f"git {' '.join(args)} failed")
    return stdout.decode().strip()


def safe_workspace_path(repo_path: Path, requested_path: str) -> Path:
    if requested_path.strip() == "":
        raise WorkspacePathError("Path is required")

    relative_path = Path(requested_path)
    if relative_path.is_absolute() or ".." in relative_path.parts or ".git" in relative_path.parts:
        raise WorkspacePathError("Invalid workspace path")

    repo_root = repo_path.resolve()
    candidate = (repo_root / relative_path).resolve()
    if not candidate.is_relative_to(repo_root):
        raise WorkspacePathError("Path escapes workspace")
    return candidate


def file_revision(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def list_workspace_files(repo_path: Path) -> list[dict[str, object]]:
    repo_root = repo_path.resolve()
    if not repo_root.exists():
        return []

    entries: list[dict[str, object]] = []
    for path in sorted(repo_root.rglob("*")):
        relative = path.relative_to(repo_root)
        if ".git" in relative.parts:
            continue
        entries.append(
            {
                "path": relative.as_posix(),
                "kind": "directory" if path.is_dir() else "file",
                "size": None if path.is_dir() else path.stat().st_size,
            }
        )
    return entries


def read_workspace_file(repo_path: Path, requested_path: str) -> dict[str, str]:
    path = safe_workspace_path(repo_path, requested_path)
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(requested_path)

    try:
        content = path.read_text()
    except UnicodeDecodeError as exc:
        raise WorkspacePathError("Only text files can be read through this endpoint") from exc

    return {"path": requested_path, "content": content, "revision": file_revision(path)}


def write_workspace_file(
    repo_path: Path,
    requested_path: str,
    content: str,
    base_revision: str | None,
) -> dict[str, str]:
    path = safe_workspace_path(repo_path, requested_path)
    current_revision = file_revision(path) if path.exists() and path.is_file() else None
    if base_revision is not None and current_revision != base_revision:
        raise WorkspaceConflictError("File revision conflict")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return {"path": requested_path, "content": content, "revision": file_revision(path)}


def assert_workspace_bare(repo_path: Path) -> None:
    """Enforce the "workspace is LITERALLY empty after init" invariant.

    ╔══════════════════════════ DO NOT BREAK ═══════════════════════════╗
    ║ In-place scaffolders (`npm create vite .`, `create-react-app .`,  ║
    ║ etc.) REFUSE to run when cwd contains anything — including `.git`.║
    ║ Do NOT write ANYTHING here: no .gitignore, no README, no template ║
    ║ files, and *no `git init`* either.  All post-scaffold seeding —   ║
    ║ git init + initial commit + baseline .gitignore — happens in the  ║
    ║ `set_project_root` dynamic-tool handler once Codex has reported   ║
    ║ the real project root (which may be `/workspace` or a subdir like ║
    ║ `/workspace/my-app`).  See                                        ║
    ║   apps/worker/src/polaris_worker/runner.py::_ensure_project_git     ║
    ║   apps/api/src/polaris_api/services/gitignore_baseline.py           ║
    ╚═══════════════════════════════════════════════════════════════════╝

    Raises WorkspaceError if the invariant is violated. Called at the tail
    of `initialize_workspace` as a post-condition; callable from tests /
    future maintenance to spot drift the moment it's introduced.
    """
    if not repo_path.is_dir():
        raise WorkspaceError(f"workspace missing: {repo_path}")
    contents = sorted(p.name for p in repo_path.iterdir())
    if contents:
        raise WorkspaceError(
            "workspace invariant violated: initialize_workspace must leave "
            f"an EMPTY directory at {repo_path}; found: {contents!r}. "
            "Defer any file seeding (including `git init`) to the "
            "set_project_root handler."
        )


async def initialize_workspace(repo_path: Path) -> GitCommit:
    """Create an EMPTY workspace directory — no git init, no templates.

    The agent (Codex) drives scaffolding on the first turn.  In-place
    scaffolders (`npm create vite .`, `create-react-app .`) refuse to run
    when cwd contains anything at all — so we can't even pre-seed `.git/`.
    Git initialization + initial commit + baseline `.gitignore` all happen
    later, inside the `set_project_root` dynamic-tool handler on the
    worker, once the agent has declared where the project actually lives.

    `commit_hash` is always None here (no commits, no repo); the first
    commit is made by the handler above.  The workspace path is created
    (so docker bind-mounts can attach), but left bare.

    See `assert_workspace_bare` for the enforced post-condition and the
    big-letter explanation of why the directory must stay empty.
    """
    if repo_path.exists() and any(repo_path.iterdir()):
        raise WorkspaceError(f"Workspace path is not empty: {repo_path}")

    repo_path.parent.mkdir(parents=True, exist_ok=True)
    repo_path.mkdir(exist_ok=True)

    # Post-condition guard — fires immediately if anyone ever adds a file
    # write above (including `git init`). Keeps the scaffolder contract
    # holding without needing a separate code-review catch.
    assert_workspace_bare(repo_path)

    return GitCommit(branch="main", commit_hash=None)


async def current_commit(repo_path: Path) -> str | None:
    # Workspaces initialized post-"defer git init to set_project_root" may
    # not have a `.git` yet.  Don't shell out into "fatal: not a git
    # repository" — just return None.
    if not (repo_path / ".git").exists():
        return None
    commit = await run_git(repo_path, "rev-parse", "HEAD", check=False)
    return commit or None


async def create_snapshot(repo_path: Path, title: str) -> str:
    await run_git(repo_path, "add", ".")
    status = await run_git(repo_path, "status", "--porcelain")
    if status:
        await run_git(repo_path, "commit", "-m", title)
    commit = await current_commit(repo_path)
    if commit is None:
        raise WorkspaceError("Workspace has no commit")
    return commit

