"""Config and paths. One place that knows where state lives.

The repo is the engine. The operator's instance lives outside it:

  config home  $CURATED_DIGEST_HOME, else $XDG_CONFIG_HOME/curated-digest,
               else ~/.config/curated-digest. Holds tasks/, the *.env
               credential files (0600) and an optional voice.md.
  state        $CURATED_DIGEST_STATE_DIR, else $XDG_STATE_HOME/curated-digest,
               else ~/.local/state/curated-digest. Ledger, store, pause flag.

Every path is read from the environment on each call, so tests and the
runner can point a process at a scratch instance without reloading modules.
"""
import os
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent


def _xdg(var: str, fallback: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / fallback)


def config_home() -> Path:
    if os.environ.get("CURATED_DIGEST_HOME"):
        return Path(os.environ["CURATED_DIGEST_HOME"]).expanduser()
    return _xdg("XDG_CONFIG_HOME", ".config") / "curated-digest"


# Each credential file and the variable that relocates it.
ENV_OVERRIDES = {
    "telegram.env": "CURATED_DIGEST_TELEGRAM_ENV",
    "gemini.env": "CURATED_DIGEST_GEMINI_KEY_FILE",
    "reddit.env": "CURATED_DIGEST_REDDIT_ENV",
    "curated-digest.env": "CURATED_DIGEST_ENV",
}


def env_file(name: str, override_var: str = "") -> Path:
    """A credential file: its override variable, else <config home>/<name>."""
    v = os.environ.get(override_var or ENV_OVERRIDES.get(name, ""))
    return Path(v).expanduser() if v else config_home() / name


PLACEHOLDER = "replace-me"


def is_placeholder(value) -> bool:
    """A template value `init` wrote and nobody filled in. Treated as unset so
    a placeholder is never sent to a provider as if it were a credential."""
    return not value or PLACEHOLDER in value


def tasks_dir() -> Path:
    v = os.environ.get("CURATED_DIGEST_TASKS_DIR")
    return Path(v).expanduser() if v else config_home() / "tasks"


def state_dir() -> Path:
    v = os.environ.get("CURATED_DIGEST_STATE_DIR")
    d = Path(v).expanduser() if v else _xdg("XDG_STATE_HOME", ".local/state") / "curated-digest"
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path() -> Path:
    return state_dir() / "curated_digest.db"


def pause_flag() -> Path:
    return state_dir() / "PAUSED"


def sidecar_path() -> Path:
    """Optional rate-limit reset times, written by a statusline sidecar such as
    tools/build-loop/statusline-sidecar. Absent means 'parse the error text'."""
    return state_dir() / "ratelimit.json"


def config_path() -> Path:
    return Path(os.environ.get("CURATED_DIGEST_CONFIG", REPO / "config" / "harness.yaml"))


def load() -> dict:
    with open(config_path()) as fh:
        return yaml.safe_load(fh)
