import pathlib

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.internal.auth.login_types import LoginTypeEnum


class DBSettings(BaseModel):
    sqlite_path: str = "db.sqlite"
    """Relative path to the sqlite database given the config directory. If absolute, it ignores the config dir location."""
    use_postgres: bool = False
    """Use Postgres instead of SQLite. The postgres_* settings only apply when this is on."""
    postgres_host: str = "localhost"
    """Postgres host name."""
    postgres_port: int = 5432
    """Postgres port."""
    postgres_db: str = "earr"
    """Postgres database name."""
    postgres_user: str = "earr"
    """Postgres user."""
    postgres_password: str = "password"
    """Postgres password."""
    postgres_ssl_mode: str = "prefer"
    """Postgres sslmode, e.g. disable, prefer or require."""


class ApplicationSettings(BaseModel):
    debug: bool = False
    """Development mode: detailed error pages, no caching of static files, and missing frontend scripts are downloaded at startup."""
    openapi_enabled: bool = False
    """Serves the interactive API docs at /docs and the schema at /openapi.json."""
    config_dir: str = "/config"
    """Where the database and other state are kept."""
    port: int = 8000
    """Port the Docker image listens on."""
    version: str = "local"
    """Version shown in the UI. Set by the image build."""
    log_level: str = "INFO"
    """Log level, e.g. DEBUG, INFO or WARNING."""
    base_url: str = ""
    """Path prefix when served from a sub path behind a reverse proxy, e.g. /earr."""
    disable_censor: bool = False
    """Log usernames in full instead of partly masked."""
    forwarded_allow_ips: str = "127.0.0.1"
    """Trusted proxy IPs for forwarded headers. Used by Uvicorn's --forwarded-allow-ips."""

    default_region: str = "us"
    """Default region used in the search"""

    force_login_type: str = ""
    """Forces the login type used. If set, the login type cannot be changed in the UI."""

    init_root_username: str = ""
    """Creates this root admin on first start instead of showing the setup page. Needs EARR_APP__INIT_ROOT_PASSWORD too."""
    init_root_password: str = ""
    """Password for the root admin created from EARR_APP__INIT_ROOT_USERNAME."""

    def get_force_login_type(self) -> LoginTypeEnum | None:
        if self.force_login_type.strip():
            try:
                login_type = LoginTypeEnum(self.force_login_type.strip().lower())
                if login_type == LoginTypeEnum.api_key:
                    raise ValueError(
                        "API key login type is not supported for forced login type."
                    )
                return login_type
            except ValueError:
                raise ValueError(f"Invalid force login type: {self.force_login_type}")
        return None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(  # pyright: ignore[reportUnannotatedClassAttribute]
        env_prefix="EARR_",
        env_nested_delimiter="__",
        nested_model_default_partial_update=True,
        env_file=(".env.local", ".env"),
    )

    db: DBSettings = DBSettings()
    app: ApplicationSettings = ApplicationSettings()

    def get_sqlite_path(self):
        if self.db.sqlite_path.startswith("/"):
            return self.db.sqlite_path
        return str(pathlib.Path(self.app.config_dir) / self.db.sqlite_path)
