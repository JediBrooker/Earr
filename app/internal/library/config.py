from pathlib import Path
from typing import Literal

from sqlmodel import Session

from app.internal.library.naming import DEFAULT_FOLDER_TEMPLATE
from app.internal.models import OrganizeModeEnum
from app.util.cache import StringConfigCache

DEFAULT_SCAN_INTERVAL = 300
MIN_SCAN_INTERVAL = 30
DEFAULT_MATCH_THRESHOLD = 85
DEFAULT_PENDING_TTL_DAYS = 14


class LibraryMisconfigured(ValueError):
    pass


LibraryConfigKey = Literal[
    "library_enabled",
    "library_mode",
    "library_download_dir",
    "library_root_dir",
    "library_folder_template",
    "library_scan_interval",
    "library_match_threshold",
    "library_overwrite",
    "library_write_metadata",
]


class LibraryConfig(StringConfigCache[LibraryConfigKey]):
    def is_valid(self, session: Session) -> bool:
        return (
            self.get_enabled(session)
            and self.get_download_dir(session) is not None
            and self.get_root_dir(session) is not None
        )

    def raise_if_invalid(self, session: Session):
        if not self.get_download_dir(session):
            raise LibraryMisconfigured("Completed downloads folder not set")
        if not self.get_root_dir(session):
            raise LibraryMisconfigured("Library folder not set")

    def get_enabled(self, session: Session) -> bool:
        return bool(self.get_bool(session, "library_enabled") or False)

    def set_enabled(self, session: Session, enabled: bool):
        self.set_bool(session, "library_enabled", enabled)

    def get_mode(self, session: Session) -> OrganizeModeEnum:
        value = self.get(session, "library_mode")
        try:
            return OrganizeModeEnum(value)
        except ValueError:
            return OrganizeModeEnum.hardlink

    def set_mode(self, session: Session, mode: OrganizeModeEnum):
        self.set(session, "library_mode", mode.value)

    def get_download_dir(self, session: Session) -> Path | None:
        value = self.get(session, "library_download_dir")
        return Path(value) if value else None

    def set_download_dir(self, session: Session, path: str):
        self.set(session, "library_download_dir", path.rstrip("/") or "/")

    def get_root_dir(self, session: Session) -> Path | None:
        value = self.get(session, "library_root_dir")
        return Path(value) if value else None

    def set_root_dir(self, session: Session, path: str):
        self.set(session, "library_root_dir", path.rstrip("/") or "/")

    def get_folder_template(self, session: Session) -> str:
        return self.get(session, "library_folder_template", DEFAULT_FOLDER_TEMPLATE)

    def set_folder_template(self, session: Session, template: str):
        self.set(session, "library_folder_template", template.strip())

    def get_scan_interval(self, session: Session) -> int:
        return max(
            MIN_SCAN_INTERVAL,
            self.get_int(session, "library_scan_interval", DEFAULT_SCAN_INTERVAL),
        )

    def set_scan_interval(self, session: Session, seconds: int):
        self.set_int(session, "library_scan_interval", max(MIN_SCAN_INTERVAL, seconds))

    def get_match_threshold(self, session: Session) -> int:
        return min(
            100,
            max(
                0,
                self.get_int(
                    session, "library_match_threshold", DEFAULT_MATCH_THRESHOLD
                ),
            ),
        )

    def set_match_threshold(self, session: Session, threshold: int):
        self.set_int(session, "library_match_threshold", min(100, max(0, threshold)))

    def get_write_metadata(self, session: Session) -> bool:
        value = self.get_bool(session, "library_write_metadata")
        return True if value is None else value

    def set_write_metadata(self, session: Session, enabled: bool):
        self.set_bool(session, "library_write_metadata", enabled)

    def get_overwrite(self, session: Session) -> bool:
        return bool(self.get_bool(session, "library_overwrite") or False)

    def set_overwrite(self, session: Session, overwrite: bool):
        self.set_bool(session, "library_overwrite", overwrite)


library_config = LibraryConfig()
