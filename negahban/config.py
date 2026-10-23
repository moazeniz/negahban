"""Runtime settings, read once from the environment. Secrets are never logged; see redact.py."""
import os, secrets
from dataclasses import dataclass, field
from pathlib import Path


def _env(k, d=''):
    return os.environ.get(k, d).strip()


@dataclass
class Settings:
    liara_base: str = field(default_factory=lambda: _env('LIARA_API_BASE', 'https://api.iran.liara.ir').rstrip('/'))
    liara_dns_base: str = field(default_factory=lambda: _env('LIARA_DNS_BASE', 'https://dns-service.liara.ir/api/v1').rstrip('/'))
    liara_token: str = field(default_factory=lambda: _env('LIARA_API_TOKEN'))
    liara_team: str = field(default_factory=lambda: _env('LIARA_TEAM_ID'))
    liara_network: str = field(default_factory=lambda: _env('LIARA_NETWORK_ID'))
    box_base: str = field(default_factory=lambda: _env('BOXAPI_BASE_URL', 'https://ai.boxapi.ir/ai/v1').rstrip('/'))
    box_keys: list = field(default_factory=lambda: [k.strip() for k in _env('BOXAPI_KEYS').split(',') if k.strip()])
    model: str = field(default_factory=lambda: _env('NEGAHBAN_MODEL', 'openai/gpt-6-luna'))
    data_dir: Path = field(default_factory=lambda: Path(_env('DATA_DIR', str(Path(__file__).resolve().parent.parent / 'data'))))
    admin_password: str = field(default_factory=lambda: _env('ADMIN_PASSWORD'))
    session_secret: str = field(default_factory=lambda: _env('SESSION_SECRET') or secrets.token_hex(32))
    mcp_token: str = field(default_factory=lambda: _env('MCP_TOKEN'))
    watchdog_token: str = field(default_factory=lambda: _env('WATCHDOG_TOKEN'))
    demo_app: str = field(default_factory=lambda: _env('DEMO_APP', 'demo-shop'))
    demo_chaos_token: str = field(default_factory=lambda: _env('DEMO_CHAOS_TOKEN'))
    demo_src: Path = field(default_factory=lambda: Path(_env('DEMO_SRC', str(Path(__file__).resolve().parent.parent / 'demo-shop'))))
    self_app: str = field(default_factory=lambda: _env('SELF_APP', 'negahban-sre'))
    monitor_apps: list = field(default_factory=lambda: [a.strip() for a in _env('MONITOR_APPS', 'demo-shop,bazargard-ai').split(',') if a.strip()])
    orchestrator_interval: int = field(default_factory=lambda: int(_env('ORCH_INTERVAL', '60')))
    orchestrator_enabled: bool = field(default_factory=lambda: _env('ORCH_ENABLED', '1') == '1')
    secure_cookies: bool = field(default_factory=lambda: _env('SECURE_COOKIES', '1') == '1')
    dry_run: bool = field(default_factory=lambda: _env('LIARA_DRY_RUN', '0') == '1')

    def secrets(self):
        out = [self.liara_token, self.admin_password, self.session_secret, self.mcp_token, self.watchdog_token, self.demo_chaos_token, os.environ.get('LLM_GATE_TOKEN', ''), os.environ.get('LIARA_EXEC_RELAY_TOKEN', ''), os.environ.get('STATUS_INGEST_TOKEN', ''), os.environ.get('SCALER_ADMIN_TOKEN', ''), os.environ.get('LB_READ_TOKEN', ''), os.environ.get('LB_WRITE_TOKEN', ''), *self.box_keys]
        return [s for s in out if s and len(s) >= 6]


settings = Settings()
