from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path

LAUNCH_EXECUTE_POLICIES = {"allow", "refuse"}
RESUME_PACKET_PLACEHOLDERS = ("{resume_path}", "{resume_path_path}")


def launch_template_uses_resume_packet(template: str) -> bool:
    return any(placeholder in template for placeholder in RESUME_PACKET_PLACEHOLDERS)


@dataclass(frozen=True)
class LaunchSpec:
    command: str
    template: str
    template_source: str
    cwd: str
    instructions: str
    packet_aware: bool
    execute_policy: str
    warning: str | None = None

    def __post_init__(self) -> None:
        if self.execute_policy not in LAUNCH_EXECUTE_POLICIES:
            allowed = ", ".join(sorted(LAUNCH_EXECUTE_POLICIES))
            raise ValueError(f"execute_policy must be one of: {allowed}")


@dataclass(frozen=True)
class AgentAdapter:
    key: str
    display_name: str
    cli_command: str
    launch_template_env: str
    default_launch_template: str
    launch_instructions_template: str
    resume_packet_target: str
    event_capture_hook_name: str | None = None

    def resolve_launch_template(self) -> tuple[str, str]:
        template = os.getenv(self.launch_template_env)
        if template:
            return template, "env"
        return self.default_launch_template, "default"

    def render_launch_spec(self, repo_root: Path, resume_path: Path) -> LaunchSpec:
        template, source = self.resolve_launch_template()
        packet_aware = launch_template_uses_resume_packet(template)
        if source == "default" and not packet_aware:
            placeholders = " or ".join(RESUME_PACKET_PLACEHOLDERS)
            raise SystemExit(
                f"Built-in launch template for {self.key} must include {placeholders}"
            )
        values = self._template_values(repo_root, resume_path)
        try:
            command = template.format(**values)
        except KeyError as exc:
            raise SystemExit(
                f"Launch template for {self.key} references unknown placeholder: {exc.args[0]}"
            ) from exc
        warning: str | None = None
        execute_policy = "allow"
        if packet_aware:
            instructions = self.launch_instructions_template.format(**values)
        else:
            execute_policy = "refuse"
            placeholders = " or ".join(RESUME_PACKET_PLACEHOLDERS)
            warning = (
                f"{self.display_name} launch template does not pass the resume packet. "
                f"`launch --execute` will refuse until the template includes {placeholders}."
            )
            instructions = (
                f"{warning} Resume packet: {resume_path}."
            )
        return LaunchSpec(
            command=command,
            template=template,
            template_source=source,
            cwd=str(repo_root),
            instructions=instructions,
            packet_aware=packet_aware,
            execute_policy=execute_policy,
            warning=warning,
        )

    def _template_values(self, repo_root: Path, resume_path: Path) -> dict[str, str]:
        return {
            "agent": self.key,
            "agent_name": self.display_name,
            "agent_cli": shlex.quote(self.cli_command),
            "repo_root": shlex.quote(str(repo_root)),
            "repo_root_path": str(repo_root),
            "resume_path": shlex.quote(str(resume_path)),
            "resume_path_path": str(resume_path),
            "resume_packet_target": self.resume_packet_target,
        }


class ClaudeCodeAdapter(AgentAdapter):
    def __init__(self) -> None:
        super().__init__(
            key="claude",
            display_name="Claude Code",
            cli_command="claude",
            launch_template_env="AGENT_RELAY_CLAUDE_LAUNCH_TEMPLATE",
            default_launch_template='cd {repo_root} && {agent_cli} -p "$(cat {resume_path})"',
            launch_instructions_template=(
                "Start {agent_name} in {repo_root_path} with the resume packet as its prompt."
            ),
            resume_packet_target="claude",
            event_capture_hook_name=None,
        )


class CodexAdapter(AgentAdapter):
    def __init__(self) -> None:
        super().__init__(
            key="codex",
            display_name="Codex",
            cli_command="codex",
            launch_template_env="AGENT_RELAY_CODEX_LAUNCH_TEMPLATE",
            default_launch_template='cd {repo_root} && {agent_cli} "$(cat {resume_path})"',
            launch_instructions_template=(
                "Start {agent_name} in {repo_root_path} with the resume packet as its prompt."
            ),
            resume_packet_target="codex",
            event_capture_hook_name=None,
        )


AGENT_REGISTRY: dict[str, AgentAdapter] = {
    adapter.key: adapter
    for adapter in (
        ClaudeCodeAdapter(),
        CodexAdapter(),
    )
}

AGENT_NAMES = tuple(AGENT_REGISTRY)


def get_agent_adapter(agent: str) -> AgentAdapter:
    try:
        return AGENT_REGISTRY[agent]
    except KeyError as exc:
        raise SystemExit(f"Unsupported agent adapter: {agent}") from exc


def get_agent_display_name(agent: str) -> str:
    return get_agent_adapter(agent).display_name
