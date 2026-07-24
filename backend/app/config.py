import os
from pydantic_settings import BaseSettings, SettingsConfigDict


def default_data_dir() -> str:
    local_appdata = os.environ.get("LOCALAPPDATA")
    base = os.path.join(local_appdata, "nfl-edge-app") if local_appdata else os.path.join(os.getcwd(), "data")
    os.makedirs(base, exist_ok=True)
    return base


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="NFL_EDGE_", env_file=".env")

    data_dir: str = default_data_dir()
    db_path: str = ""
    host: str = "127.0.0.1"
    port: int = 8756

    def sqlite_url(self) -> str:
        path = self.db_path or os.path.join(self.data_dir, "app.db")
        return f"sqlite:///{path}"


settings = Settings()
