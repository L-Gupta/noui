from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the noui/ directory (parent of backend/)
_env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_path)


@dataclass
class Settings:
    app_name: str = "noui-backend"
    host: str = "0.0.0.0"
    port: int = 8002
    debug: bool = True

    cors_origins: list[str] = field(
        default_factory=lambda: [
            "chrome-extension://*",
            "http://localhost:3000",
            "http://localhost:8002",
        ]
    )

    data_dir: str = ""
    db_url: str = ""

    # Claude API
    anthropic_api_key: str = ""
    claude_model: str = "claude-sonnet-4-20250514"

    # Tabby API
    tabby_api_host: str = "http://localhost:8080"
    tabby_admin_token: str = ""

    def __post_init__(self) -> None:
        if not self.data_dir:
            self.data_dir = str(Path(__file__).resolve().parent / "data")
        if not self.db_url:
            self.db_url = f"sqlite+aiosqlite:///{self.data_dir}/noui.db"


settings = Settings(
    debug=os.environ.get("NOUI_DEBUG", "true").lower() == "true",
    port=int(os.environ.get("NOUI_PORT", "8002")),
    anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", ""),
    claude_model=os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514"),
    tabby_api_host=os.environ.get("TABBY_API_HOST", "http://localhost:8080"),
    tabby_admin_token=os.environ.get("TABBY_ADMIN_TOKEN", ""),
)
