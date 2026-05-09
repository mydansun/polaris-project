"""Unit tests for the platform-side publish-stack detection.

``_detect_stack`` is the fallback used by ``auto_scaffold_if_missing``
when the user clicks Publish without having run the workspace CLI
first.  It picks the recommended stack from files at the repo root.
Vite-shaped SPAs are distinguished from plain Node servers by peeking
at ``package.json`` dependencies for a ``vite`` key.
"""

from __future__ import annotations

import json
from pathlib import Path

from polaris_api.services.publish import _detect_stack, auto_scaffold_if_missing


def _write_pkg(dir_: Path, deps: dict | None = None, dev_deps: dict | None = None) -> None:
    (dir_ / "package.json").write_text(
        json.dumps(
            {
                "name": "x",
                "dependencies": deps or {},
                "devDependencies": dev_deps or {},
            }
        )
    )


# ── detection ─────────────────────────────────────────────────────────────


def test_vite_in_dependencies_is_spa(tmp_path: Path):
    _write_pkg(tmp_path, deps={"vite": "^5"})
    assert _detect_stack(tmp_path) == "spa"


def test_vite_in_devdependencies_is_spa(tmp_path: Path):
    _write_pkg(tmp_path, dev_deps={"vite": "^5"})
    assert _detect_stack(tmp_path) == "spa"


def test_express_without_vite_is_node(tmp_path: Path):
    _write_pkg(tmp_path, deps={"express": "^4"})
    assert _detect_stack(tmp_path) == "node"


def test_prisma_schema_promotes_node_to_node_prisma(tmp_path: Path):
    """package.json + prisma/schema.prisma → node-prisma (the stack
    that copies prisma/ before install + runs `prisma generate`)."""
    _write_pkg(tmp_path, deps={"@prisma/client": "^5", "next": "^14"})
    (tmp_path / "prisma").mkdir()
    (tmp_path / "prisma" / "schema.prisma").write_text("// stub")
    assert _detect_stack(tmp_path) == "node-prisma"


def test_node_without_prisma_schema_stays_node(tmp_path: Path):
    """Regression guard: node detection unchanged when
    prisma/schema.prisma is absent — even an empty prisma/ dir."""
    _write_pkg(tmp_path, deps={"express": "^4"})
    (tmp_path / "prisma").mkdir()
    assert _detect_stack(tmp_path) == "node"


def test_vite_beats_prisma(tmp_path: Path):
    """A Vite-shaped repo with stray prisma/schema.prisma still routes
    to spa — vite check fires first, and vite + prisma is rare enough
    that the user can override via `--stack=node-prisma` if they need."""
    _write_pkg(tmp_path, deps={"vite": "^5"})
    (tmp_path / "prisma").mkdir()
    (tmp_path / "prisma" / "schema.prisma").write_text("// stub")
    assert _detect_stack(tmp_path) == "spa"


def test_package_json_beats_index_html(tmp_path: Path):
    # Vite projects have BOTH package.json and index.html; must not
    # fall through to the static branch.
    _write_pkg(tmp_path, deps={"vite": "^5"})
    (tmp_path / "index.html").write_text("<!doctype html>")
    assert _detect_stack(tmp_path) == "spa"


def test_malformed_package_json_falls_back_to_node(tmp_path: Path):
    (tmp_path / "package.json").write_text("{ not json")
    assert _detect_stack(tmp_path) == "node"


def test_requirements_is_python(tmp_path: Path):
    (tmp_path / "requirements.txt").write_text("fastapi\n")
    assert _detect_stack(tmp_path) == "python"


def test_html_only_is_static(tmp_path: Path):
    (tmp_path / "index.html").write_text("<!doctype html>")
    assert _detect_stack(tmp_path) == "static"


def test_nothing_is_custom(tmp_path: Path):
    assert _detect_stack(tmp_path) == "custom"


# ── auto_scaffold integration ────────────────────────────────────────────


def test_scaffold_writes_spa_template_for_vite_repo(tmp_path: Path):
    """End-to-end-ish: a Vite repo triggers the new spa template, the
    Dockerfile is multi-stage with the SPA try_files rewrite, and the
    build token has been rendered into the real command."""
    _write_pkg(tmp_path, deps={"vite": "^5"})
    templates_root = (
        Path(__file__).resolve().parents[3] / "infra" / "publish-templates"
    )
    log: list[str] = []
    auto_scaffold_if_missing(tmp_path, templates_root, log)

    df = (tmp_path / "Dockerfile").read_text()
    assert "node:22-alpine AS builder" in df
    assert "nginx:1.27-alpine" in df
    assert "try_files" in df
    # Token replacement rendered the actual build command.
    assert "__POLARIS_BUILD_CMD__" not in df
    assert "npm run build" in df

    pyaml = (tmp_path / "polaris.yaml").read_text()
    assert "stack: spa" in pyaml
    assert "port: 80" in pyaml

    # Log contains the recognisable reason so users see why we picked spa.
    assert any("stack=spa" in line for line in log)
    assert any("vite" in line for line in log)


def test_scaffold_writes_node_prisma_template(tmp_path: Path):
    """Prisma project scaffolds the node-prisma template: prisma copied
    before install, explicit `prisma generate`, and the start command
    runs migrations before launching."""
    _write_pkg(tmp_path, deps={"@prisma/client": "^5", "next": "^14"})
    (tmp_path / "prisma").mkdir()
    (tmp_path / "prisma" / "schema.prisma").write_text("// stub")
    templates_root = (
        Path(__file__).resolve().parents[3] / "infra" / "publish-templates"
    )
    log: list[str] = []
    auto_scaffold_if_missing(tmp_path, templates_root, log)

    df = (tmp_path / "Dockerfile").read_text()
    # Prisma must be copied before install (postinstall hooks find schema).
    install_run = df.index("RUN set -eux")
    copy_prisma = df.index("COPY prisma ./prisma")
    assert copy_prisma < install_run, "prisma/ must be COPY'd before install"
    # Explicit generate, defense for projects without postinstall hook.
    assert "RUN npx prisma generate" in df
    # No leftover placeholders.
    for token in ("__POLARIS_BUILD_CMD__", "__POLARIS_PORT__",
                  "__POLARIS_START_CMD__", "__POLARIS_START_CMD_JSON__",
                  "__POLARIS_SERVICE__"):
        assert token not in df

    pyaml = (tmp_path / "polaris.yaml").read_text()
    assert "stack: node-prisma" in pyaml
    assert "port: 3000" in pyaml

    # CMD must use shell-form encoding because the default start contains `&&`.
    assert 'CMD ["sh", "-c"' in df
    assert "prisma migrate deploy" in df
    assert "npm start" in df


# ── render_cmd_array (Dockerfile CMD encoding) ───────────────────────────


def test_render_cmd_array_empty_is_no_op():
    from polaris_api.services.publish import _render_cmd_array
    assert _render_cmd_array("") == '["true"]'


def test_render_cmd_array_plain_command_is_exec_form():
    from polaris_api.services.publish import _render_cmd_array
    assert _render_cmd_array("npm start") == '["npm", "start"]'
    assert _render_cmd_array("next start -p 3000") == \
        '["next", "start", "-p", "3000"]'


def test_render_cmd_array_quoted_args_survive():
    """Pre-existing bug: ``json.dumps(cmd.split())`` left literal
    quotes in the output array.  shlex split fixes it."""
    from polaris_api.services.publish import _render_cmd_array
    assert _render_cmd_array("echo 'hello world'") == '["echo", "hello world"]'


def test_render_cmd_array_shell_metachar_wraps_with_sh_c():
    """Pre-existing bug: ``"a && b".split()`` produced
    ``["a", "&&", "b"]`` and exec-form CMD would pass ``&&`` as a literal
    arg, failing at container boot.  Wrap with sh -c instead."""
    from polaris_api.services.publish import _render_cmd_array
    assert _render_cmd_array("a && b") == '["sh", "-c", "a && b"]'
    assert _render_cmd_array("npx prisma migrate deploy && npm start") == \
        '["sh", "-c", "npx prisma migrate deploy && npm start"]'
    assert _render_cmd_array("node app.js | tee /var/log/app.log") == \
        '["sh", "-c", "node app.js | tee /var/log/app.log"]'
