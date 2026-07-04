import os
from dataclasses import dataclass


@dataclass
class SupabaseConfig:
    url: str = ""
    anon_key: str = ""

    @classmethod
    def from_env(cls):
        return cls(
            url=os.getenv("SUPABASE_URL", "").strip(),
            anon_key=os.getenv("SUPABASE_ANON_KEY", "").strip(),
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.url and self.anon_key)


class SupabaseSyncService:
    """Configuration and status boundary for future Supabase sync work."""

    def __init__(self, config: SupabaseConfig = None):
        self.config = config or SupabaseConfig.from_env()

    def status_label(self) -> str:
        if self.config.is_configured:
            return "Supabase ready"
        return "Supabase not configured"

    def can_sync(self) -> bool:
        return self.config.is_configured
