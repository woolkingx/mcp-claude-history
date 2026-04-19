from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AppPaths:
    root: Path

    @property
    def projects_dir(self) -> Path:
        return self.root / "projects"

    @property
    def db_path(self) -> Path:
        return self.root / "history-field-index.db"

    @property
    def log_dir(self) -> Path:
        return self.root / "log"
