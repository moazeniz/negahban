"""Settings read from Liara env vars. DATABASE_URL (secret) points at a Liara PostgreSQL; without it SQLite is used."""
import os
import secrets
from pathlib import Path
from urllib.parse import unquote, urlparse

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY') or secrets.token_urlsafe(50)
DEBUG = os.environ.get('DJANGO_DEBUG', '0') == '1'
ALLOWED_HOSTS = [h.strip() for h in os.environ.get('ALLOWED_HOSTS', '{{app_name}}.liara.run').split(',') if h.strip()] + ['localhost', '127.0.0.1']
CSRF_TRUSTED_ORIGINS = ['https://' + h for h in ALLOWED_HOSTS if h not in ('localhost', '127.0.0.1')]
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

INSTALLED_APPS = ['django.contrib.contenttypes', 'django.contrib.auth', 'django.contrib.staticfiles']
MIDDLEWARE = ['django.middleware.security.SecurityMiddleware', 'django.middleware.common.CommonMiddleware']
ROOT_URLCONF = 'config.urls'
WSGI_APPLICATION = 'config.wsgi.application'


def _db(url):
    u = urlparse(url)
    return {'ENGINE': 'django.db.backends.postgresql', 'NAME': u.path.lstrip('/'), 'USER': unquote(u.username or ''),
            'PASSWORD': unquote(u.password or ''), 'HOST': u.hostname or '', 'PORT': str(u.port or 5432), 'CONN_MAX_AGE': 60}


DATABASES = {'default': _db(os.environ['DATABASE_URL']) if os.environ.get('DATABASE_URL')
             else {'ENGINE': 'django.db.backends.sqlite3', 'NAME': BASE_DIR / 'db.sqlite3'}}
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
LANGUAGE_CODE = 'fa'
TIME_ZONE = 'Asia/Tehran'
USE_TZ = True
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
