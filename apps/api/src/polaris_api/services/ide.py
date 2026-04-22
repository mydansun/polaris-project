from dataclasses import dataclass
import socket
from urllib.parse import quote
from uuid import UUID


@dataclass(frozen=True)
class IdeSession:
    ide_url: str | None
    ide_status: str


def render_ide_session(
    template: str,
    *,
    project_id: UUID,
    workspace_id: UUID,
    workspace_path: str,
) -> IdeSession:
    if template.strip() == "":
        return IdeSession(ide_url=None, ide_status="not_configured")

    replacements = {
        "{projectId}": quote(str(project_id), safe=""),
        "{workspaceId}": quote(str(workspace_id), safe=""),
        "{workspacePath}": quote(workspace_path, safe=""),
    }
    rendered = template.strip()
    for token, value in replacements.items():
        rendered = rendered.replace(token, value)

    return IdeSession(ide_url=rendered, ide_status="configured")


def workspace_hash(workspace_id: UUID) -> str:
    """24-char hex identifier used for predictable container names and subdomains."""
    return str(workspace_id).replace("-", "")[:24]


def render_public_ide_url(template: str, *, project_id: UUID, workspace_id: UUID) -> str:
    rendered = template.strip()
    replacements = {
        "{workspaceHash}": workspace_hash(workspace_id),
        "{projectId}": quote(str(project_id), safe=""),
        "{workspaceId}": quote(str(workspace_id), safe=""),
    }
    for token, value in replacements.items():
        rendered = rendered.replace(token, value)
    return rendered


def is_tcp_port_available(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((host, port)) != 0
