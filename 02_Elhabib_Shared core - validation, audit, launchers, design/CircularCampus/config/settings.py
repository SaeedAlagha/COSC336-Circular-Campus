"""Django settings for the Circular Campus prototype (COSC 336 Phase 6).

Configuration is read from environment variables. A local ``.env`` file is
loaded when present (python-dotenv); real environment variables take priority.
SQLite is used by default for the local demonstration; PostgreSQL is selected
when ``POSTGRES_DB`` is set (design decision DR-02).
"""
from pathlib import Path
import os

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env", override=False)


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


DEBUG = env_bool("DJANGO_DEBUG", False)
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "")
if not SECRET_KEY:
    if DEBUG or os.environ.get("CAMPUS_TESTING") == "1":
        SECRET_KEY = "insecure-local-development-key-change-me"
    else:
        # The setup launchers write a random key into .env. Refuse to start
        # without one so production never runs with a published key.
        from django.core.exceptions import ImproperlyConfigured

        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY is not set. Run the setup launcher (it creates .env) "
            "or set DJANGO_DEBUG=1 for throw-away development."
        )

ALLOWED_HOSTS = [h.strip() for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]
CSRF_TRUSTED_ORIGINS = [o.strip() for o in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()]

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    # Business modules (one per Level-1 DFD process; see docs/DESIGN_MAPPING.md)
    "apps.core",          # shared infrastructure: errors, idempotency, UI shell
    "apps.accounts",      # 1.0 Accounts and access        -> D1
    "apps.catalog",       # 2.0 Assets and handover        -> D2
    "apps.exchange",      # 3.0 Requests and holds         -> D3
    "apps.workflow",      # 4.0 Authorization and transfer -> D4
    "apps.maintenance",   # 5.0 Service and disposition    -> D5
    "apps.intelligence",  # 6.0 AI advice                  -> D6
    "apps.reporting",     # 7.0 Reports, audit, notices    -> D7, D8
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "apps.core.middleware.SecurityHeadersMiddleware",
    "apps.core.middleware.DeniedAccessAuditMiddleware",
    "apps.core.middleware.BadInputMiddleware",
]

ROOT_URLCONF = "config.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context.shell",
            ],
        },
    }
]
WSGI_APPLICATION = "config.wsgi.application"

if os.environ.get("POSTGRES_DB"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ["POSTGRES_DB"],
            "USER": os.environ.get("POSTGRES_USER", ""),
            "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
            "HOST": os.environ.get("POSTGRES_HOST", "127.0.0.1"),
            "PORT": os.environ.get("POSTGRES_PORT", "5432"),
            "CONN_MAX_AGE": 0,
            "TEST": {"NAME": os.environ.get("POSTGRES_TEST_DB", "test_" + os.environ["POSTGRES_DB"])},
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": Path(os.environ.get("SQLITE_PATH", BASE_DIR / "data" / "circular_campus.sqlite3")),
            # IMMEDIATE transactions take the write lock at BEGIN, so two
            # concurrent stock commands are serialised even on SQLite.
            "OPTIONS": {"transaction_mode": "IMMEDIATE", "timeout": 20},
        }
    }

AUTH_USER_MODEL = "accounts.User"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "core:dashboard"
LOGOUT_REDIRECT_URL = "accounts:login"

LANGUAGE_CODE = "en-gb"
TIME_ZONE = os.environ.get("CAMPUS_TIME_ZONE", "Asia/Dubai")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "data" / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage" if not DEBUG else "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
WHITENOISE_USE_FINDERS = True
WHITENOISE_AUTOREFRESH = DEBUG

# Attachments are stored outside the static tree and are only streamed by an
# authorising view (FR-06). There is deliberately no MEDIA_URL route.
PROTECTED_MEDIA_ROOT = Path(os.environ.get("CAMPUS_MEDIA_ROOT", BASE_DIR / "data" / "protected_media"))
ATTACHMENT_MAX_BYTES = 10 * 1024 * 1024
DATA_UPLOAD_MAX_MEMORY_SIZE = 11 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = 60 * 60 * 8
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
if env_bool("CAMPUS_HTTPS", False):
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_SSL_REDIRECT = True
    SECURE_HSTS_SECONDS = 3600

MESSAGE_STORAGE = "django.contrib.messages.storage.session.SessionStorage"

# ---- Business configuration (versioned prototype defaults; Phase 3 TBD-04) ----
CAMPUS = {
    "HOLD_HOURS": int(os.environ.get("CAMPUS_HOLD_HOURS", "48")),
    "DEMO_MODE": env_bool("CAMPUS_DEMO_MODE", True),
}

# ---- Gemini (AI provider) — key is read server-side only (NFR-11, NFR-19) ----
GEMINI = {
    "API_KEY": os.environ.get("GEMINI_API_KEY", "").strip(),
    "MODEL": os.environ.get("GEMINI_MODEL", "gemini-3.8-flash").strip(),
    "EMBEDDING_MODEL": os.environ.get("GEMINI_EMBEDDING_MODEL", "gemini-embedding-001").strip(),
    "BASE_URL": os.environ.get("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta").rstrip("/"),
    "TIMEOUT_SECONDS": float(os.environ.get("GEMINI_TIMEOUT_SECONDS", "10")),
    "ENABLED": env_bool("GEMINI_ENABLED", True),
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "loggers": {
        "campus": {"handlers": ["console"], "level": os.environ.get("CAMPUS_LOG_LEVEL", "INFO")},
        "django.request": {"handlers": ["console"], "level": "ERROR"},
    },
}

if os.environ.get("CAMPUS_TESTING") == "1":
    # Fast hashing for the automated test suite only; never used at runtime.
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
