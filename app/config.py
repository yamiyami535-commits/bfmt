from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "rpa-control-hub"
    debug: bool = False

    jwt_secret_key: str = "dev-only-change-in-production"
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 20

    admin_username: str = "admin"
    admin_password: str = "admin"

    user_username: str | None = None
    user_password: str | None = None

    mysql_host: str | None = None
    mysql_port: int = 3306
    mysql_user: str | None = None
    mysql_password: str | None = None
    mysql_db: str | None = None

    qc_recovery_enabled: bool = False
    qc_recovery_interval_seconds: int = 60
    qc_recovery_batch_size: int = 50

    # Optional remote service restart callback before triggering bot.
    # Example:
    # http://{runner_name}:8787/api/restart-service?service=Automation%20Anywhere%20Bot%20Agent
    runner_restart_url_template: str | None = None
    runner_restart_winrm_host_template: str | None = None
    runner_restart_winrm_username: str | None = None
    runner_restart_winrm_password: str | None = None
    runner_restart_winrm_transport: str = "ntlm"
    runner_restart_winrm_command: str = "Restart-Service -Name {service} -Force"

    # Control Room device polling (for dashboard "live" merge)
    cr_poll_enabled: bool = False
    cr_poll_interval_seconds: int = 15
    cr_base_scheme: str = "http"
    cr_devices_list_path: str = "/v2/devices/list"
    cr_devices_page_length: int = 500

    # BEO v3 activity list (In progress–style data). Same auth as devices.
    cr_activity_list_path: str = "/v3/activity/list"
    cr_activity_page_length: int = 300
    # Comma-separated status values counted as "active" (Control Room enums; adjust if your CR differs).
    cr_activity_status_active: str = "RUNNING,RUN_PAUSED,DEPLOYED,STARTED,IN_PROGRESS,EXECUTING"
    cr_activity_status_queued: str = "QUEUED"
    cr_activity_status_pending: str = "PENDING_EXECUTION,PENDING,DEPLOYING"


@lru_cache
def get_settings() -> Settings:
    return Settings()
