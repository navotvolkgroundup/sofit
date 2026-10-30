"""The publish scripts run from the ws-scraper venv, where sofit isn't
installed - load sofit/shows.py (stdlib only) straight from the repo."""
import importlib.util
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "src" / "sofit" / "shows.py"
_spec = importlib.util.spec_from_file_location("sofit_shows", _SRC)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

browser_profile, home, show_config, slug = (
    _mod.browser_profile, _mod.home, _mod.show_config, _mod.slug)
