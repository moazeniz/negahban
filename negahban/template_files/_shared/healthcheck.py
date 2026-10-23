"""Liara healthCheck probe: healthy while the web server answers below 500."""
import sys, urllib.error, urllib.request

try:
    urllib.request.urlopen('http://127.0.0.1:{{port}}{{health_path}}', timeout=4)
except urllib.error.HTTPError as e:
    sys.exit(0 if e.code < 500 else 1)
except Exception:
    sys.exit(1)
