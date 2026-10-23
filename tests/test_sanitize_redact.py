from negahban.redact import mask_envs, redact
from negahban.sanitize import clean, fence, suspicious
from pathlib import Path


def test_invisible_and_bidi_removed_but_zwnj_kept():
    s = 'a​b‮c\U000e0041d﻿e‌f'
    assert clean(s) == 'abcde‌f'


def test_ansi_and_control_chars_removed():
    assert clean('\x1b[31mred\x1b[0m\x07!') == 'red!'


def test_fence_flags_injection_from_logs():
    txt = (Path(__file__).parent / 'fixtures' / 'injection.log').read_text()
    assert suspicious(txt)
    f = fence('logs', {'lines': txt.splitlines()})
    assert f.startswith('<untrusted-data source="logs" warning=') and f.endswith('</untrusted-data>')


def test_redact_secrets():
    s = redact('Authorization: Bearer abcdefghijklmnop12345 and token=liara-test-token-abcdef and postgres://u:p4ss@h/db sk-test-key-aaaaaaaaaaaa')
    assert 'abcdefghijklmnop12345' not in s and 'liara-test-token-abcdef' not in s and 'p4ss' not in s and 'sk-test-key' not in s


def test_mask_envs_hides_sensitive_values():
    m = {e['key']: e for e in mask_envs([{'key': 'PAYMENT_GATEWAY_KEY', 'value': 'pk_live_123456'}, {'key': 'APP_VERSION', 'value': '1.0'}])}
    assert m['PAYMENT_GATEWAY_KEY']['masked'] and 'pk_live' not in m['PAYMENT_GATEWAY_KEY']['value'] and m['APP_VERSION']['value'] == '1.0'
