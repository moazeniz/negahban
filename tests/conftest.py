import os, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.pop('LIARA_DRY_RUN', None)  # tests rely on mocked transports, never on dry-run
_tmp = tempfile.mkdtemp(prefix='ngh-test-')
os.environ.update({'DATA_DIR': _tmp, 'ADMIN_PASSWORD': 'test-admin-pw', 'SESSION_SECRET': 's' * 64, 'MCP_TOKEN': 'mcp-test-token-123',
                   'WATCHDOG_TOKEN': 'wd-test-token-123', 'LIARA_API_TOKEN': 'liara-test-token-abcdef', 'LIARA_TEAM_ID': 'team1',
                   'BOXAPI_KEYS': 'sk-test-key-aaaaaaaaaaaa,sk-test-key-bbbbbbbbbbbb', 'ORCH_ENABLED': '0', 'SECURE_COOKIES': '0',
                   'DEMO_SRC': str(ROOT / 'demo-shop')})

import pytest  # noqa: E402
from negahban import store  # noqa: E402

store.init()


@pytest.fixture
def fresh_db():
    store.x('delete from proposals'); store.x('delete from policies'); store.x('delete from audit'); store.x('delete from kv'); store.x('delete from incidents')
    yield
