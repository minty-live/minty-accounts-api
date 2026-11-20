"""The service refuses to start without its required configuration (fail loudly)."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


REQUIRED = {
    "SECRET_KEY": "x",
    "DATABASE_URL": "postgresql://postgres@localhost:5432/postgres",
    "XERO_CLIENT_ID": "id",
    "XERO_CLIENT_SECRET": "secret",
    "INVITE_FROM": "Minty <invites@example.test>",
}


@pytest.mark.parametrize("missing", list(REQUIRED))
def test_a_missing_required_value_stops_the_start(missing, tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in REQUIRED}
    env.update(REQUIRED)
    env[missing] = ""
    env["DJANGO_SETTINGS_MODULE"] = "config.settings"
    # Run from a folder with no .env, importing this repo's config by path.
    env["PYTHONPATH"] = str(ROOT)
    code = (
        "import dotenv; dotenv.load_dotenv = lambda *a, **k: False\n"
        "import django; django.setup()\n"
        "from django.conf import settings; settings.SECRET_KEY"
    )
    result = subprocess.run([sys.executable, "-c", code], env=env, cwd=tmp_path,
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert f"{missing} is required and is not set" in result.stderr


@pytest.mark.parametrize("app_env, expected", [
    ("development", True), ("Development ", True), ("production", False), ("", False), (None, False),
])
def test_debug_and_the_draft_banner_follow_app_env_alone(app_env, expected, tmp_path):
    """Owner, 2026-11-20: APP_ENV=development is the one switch for debug and every
    development-only feature (the draft banner here); anything else is production. The old
    DJANGO_DEBUG and LEGAL_SHOW_DRAFT_BANNER are ignored."""
    env = {k: v for k, v in os.environ.items() if k not in REQUIRED and k != "APP_ENV"}
    env.update(REQUIRED)
    if app_env is not None:
        env["APP_ENV"] = app_env
    env.update({"DJANGO_DEBUG": "1" if not expected else "0", "LEGAL_SHOW_DRAFT_BANNER": "1" if not expected else "0"})
    env["DJANGO_SETTINGS_MODULE"] = "config.settings"
    env["PYTHONPATH"] = str(ROOT)
    code = (
        "import dotenv; dotenv.load_dotenv = lambda *a, **k: False\n"
        "import django; django.setup()\n"
        "from django.conf import settings\n"
        "from hub import registry\n"
        "print(settings.DEBUG, registry.SHOW_DRAFT_BANNER)"
    )
    result = subprocess.run([sys.executable, "-c", code], env=env, cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == [str(expected), str(expected)]


def test_gunicorn_never_writes_its_access_line():
    """Its access line prints whole URLs, secrets included; the command line beats GUNICORN_CMD_ARGS."""
    from pathlib import Path

    cmd = next(line for line in Path(__file__).resolve().parents[1].joinpath("Dockerfile").read_text(
        encoding="utf-8").splitlines() if line.startswith("CMD "))
    assert "--access-logfile /dev/null" in cmd


def test_a_schema_that_is_not_a_plain_identifier_refuses_to_start(monkeypatch):
    """It becomes the connection's search_path: anything but an identifier is refused."""
    import pytest
    from minty_shared.config import django_database

    with pytest.raises(ValueError):
        django_database("postgresql://u:p@h:5432/db?schema=pettycashv3,public;drop")
    assert django_database("postgresql://u:p@h:5432/db?schema=development")[1] == "development"
