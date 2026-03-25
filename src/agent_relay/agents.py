from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AgentProfile:
    key: str
    display_name: str
    cli_command: str
    launch_template_env: str
    default_launch_template: str
    launch_instructions_template: str

    def resolve_launch_template(self) -> tuple[str, str]:
        template = os.getenv(self.launch_template_env)
        if template:
            return template, "env"
        return self.default_launch_template, "default"


AGENT_PROFILES: dict[str, AgentProfile] = {
    "claude": AgentProfile(
        key="claude",
        display_name="Claude Code",
        cli_command="claude",
        launch_template_env="AGENT_RELAY_CLAUDE_LAUNCH_TEMPLATE",
        default_launch_template="cd {repo_root} && {agent_cli}",
        launch_instructions_template=(
            "Start {agent_name} in {repo_root_path} and use {resume_path_path} "
            "as the first resume packet you provide."
        ),
    ),
    "codex": AgentProfile(
        key="codex",
        display_name="Codex",
        cli_command="codex",
        launch_template_env="AGENT_RELAY_CODEX_LAUNCH_TEMPLATE",
        default_launch_template="cd {repo_root} && {agent_cli}",
        launch_instructions_template=(
            "Start {agent_name} in {repo_root_path} and use {resume_path_path} "
            "as the opening prompt for the handoff."
        ),
    ),
}

AGENT_NAMES = tuple(AGENT_PROFILES)


def get_agent_profile(agent: str) -> AgentProfile:
    try:
        return AGENT_PROFILES[agent]
    except KeyError as exc:
        raise SystemExit(f"Unsupported agent profile: {agent}") from exc


def render_launch_command(profile: AgentProfile, repo_root: Path, resume_path: Path) -> tuple[str, str, str]:
    template, source = profile.resolve_launch_template()
    values = _template_values(profile, repo_root, resume_path)
    try:
        command = template.format(**values)
    except KeyError as exc:
        raise SystemExit(
            f"Launch template for {profile.key} references unknown placeholder: {exc.args[0]}"
        ) from exc
    return command, template, source


def render_launch_instructions(profile: AgentProfile, repo_root: Path, resume_path: Path) -> str:
    return profile.launch_instructions_template.format(**_template_values(profile, repo_root, resume_path))


def _template_values(profile: AgentProfile, repo_root: Path, resume_path: Path) -> dict[str, str]:
    return {
        "agent": profile.key,
        "agent_name": profile.display_name,
        "agent_cli": shlex.quote(profile.cli_command),
        "repo_root": shlex.quote(str(repo_root)),
        "repo_root_path": str(repo_root),
        "resume_path": shlex.quote(str(resume_path)),
        "resume_path_path": str(resume_path),
    }
