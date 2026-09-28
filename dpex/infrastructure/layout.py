from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional, Sequence, Tuple


@dataclass(frozen=True)
class RunLayout:
    root: Path

    @property
    def workspace(self) -> Path:
        return self.root / "workspace"

    @property
    def artifacts(self) -> Path:
        return self.root / "artifacts"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def summaries(self) -> Path:
        return self.root / "summaries"

    def trigger_dir(self, project: str, bug: str, trigger: int | str) -> Path:
        return self.artifacts / project / f"bug_{bug}" / "triggers" / f"trigger_{trigger}"

    def workspace_dir(self, project: str, bug: str) -> Path:
        return self.workspace / f"{project}_{bug}b"

    def stage_log_dir(self, stage: str, project: str, bug: str,
                      trigger: int | str | None = None) -> Path:
        path = self.logs / stage / project / f"bug_{bug}"
        return path / f"trigger_{trigger}" if trigger is not None else path

    def ensure(self) -> None:
        for path in (self.workspace, self.artifacts, self.logs, self.summaries):
            path.mkdir(parents=True, exist_ok=True)

    def discover_triggers(self, projects: Sequence[str], bugs: Optional[set[str]] = None,
                          trigger: Optional[str] = None) -> Iterator[Tuple[str, str, str, Path]]:
        bases = (
            sorted(self.artifacts.glob("*"))
            if not projects or "ALL" in projects
            else [self.artifacts / project for project in projects]
        )
        for base in bases:
            if not base.is_dir():
                continue
            for bug_dir in sorted(base.glob("bug_*")):
                bug = bug_dir.name.removeprefix("bug_")
                if bugs and bug not in bugs:
                    continue
                trigger_dirs = [
                    path
                    for path in (bug_dir / "triggers").glob("trigger_*")
                    if path.name.removeprefix("trigger_").isdigit()
                ]
                for trigger_dir in sorted(
                    trigger_dirs,
                    key=lambda path: int(path.name.removeprefix("trigger_")),
                ):
                    number = trigger_dir.name.removeprefix("trigger_")
                    if trigger and number != trigger:
                        continue
                    yield base.name, bug, number, trigger_dir
