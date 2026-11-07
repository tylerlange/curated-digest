"""`curated-digest init` and `curated-digest check`: create and validate an
instance outside the repo.

Credential templates are not duplicated: init splits the repo's .env.example
on its `# --- <what>: .../<file>.env ---` headers, so the one file a reader
sees in the repo is the same text the operator fills in. Every credential file
is written 0600 and never overwritten.
"""
import os
import re
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import config, task as taskmod

ENV_EXAMPLE = config.REPO / ".env.example"
TEMPLATES = config.REPO / "templates" / "tasks"
DEFAULT_VOICE = config.REPO / "prompts" / "voice.md"
_BLOCK = re.compile(r"^# --- (?P<title>.+?): \S*/(?P<file>[\w.-]+\.env) ---$")
PLACEHOLDER_VALUE = "replace-me"
REWRITE = "REWRITE:"


@dataclass
class Report:
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def env_blocks(text: str) -> dict:
    """{filename: block text} for every credential block in .env.example."""
    blocks, current = {}, None
    for line in text.splitlines():
        if line.startswith("# --- "):
            m = _BLOCK.match(line)
            current = m.group("file") if m else None
            if current:
                blocks[current] = [f"# {m.group('title')}. Keep this file mode 0600."]
            continue
        if current:
            blocks[current].append(line)
    return {k: "\n".join(v).rstrip() + "\n" for k, v in blocks.items()}


def templates() -> list:
    return sorted(p.stem for p in TEMPLATES.glob("*.yaml"))


def _inside_repo(path: Path) -> bool:
    real, repo = os.path.realpath(path), os.path.realpath(config.REPO)
    return real == repo or real.startswith(repo + os.sep)


def init(task_template=None, task_id=None, out=print) -> int:
    home = config.config_home()
    if _inside_repo(home):
        out(f"error: config home {home} is inside the repository. Instance files "
            "(credentials, your tasks) must live outside it. Unset "
            "CURATED_DIGEST_HOME or point it elsewhere.")
        return 2
    if task_template:
        if task_template not in templates():
            out(f"error: no task template {task_template!r}. Available: "
                f"{', '.join(templates())}.")
            return 2
        tid = task_id or task_template
        if not taskmod.ID_RE.match(tid):
            out(f"error: task id {tid!r} must be lowercase letters, digits, '.', '_' or '-'.")
            return 2
    tasks = config.tasks_dir()
    for d in (home, tasks):
        d.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o700)

    def put(dest: Path, text: str, mode: int):
        if dest.exists():
            out(f"kept     {dest} (exists, not overwritten)")
            return
        # Create with the final mode so a secret file is never briefly readable.
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "w") as f:
            f.write(text)
        out(f"created  {dest}")

    for name, body in env_blocks(ENV_EXAMPLE.read_text()).items():
        put(config.env_file(name), body, 0o600)
    put(home / "voice.md", DEFAULT_VOICE.read_text(), 0o644)

    if task_template:
        text = (TEMPLATES / f"{task_template}.yaml").read_text()
        text = re.sub(r"^id: .*$", f"id: {tid}", text, count=1, flags=re.M)
        put(tasks / f"{tid}.yaml", text, 0o644)

    out(f"\nNext: fill in the 0600 *.env files in {home} (in an editor, never in a\n"
        f"chat), rewrite every {REWRITE} line in {tasks}/*.yaml, then run\n"
        "  bin/curated-digest check\n"
        "  bin/curated-digest task run <id> --dry-run")
    return 0


def _env_values(path: Path) -> dict:
    vals = {}
    for line in path.read_text().splitlines():
        if line.strip().startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        vals[k.strip()] = v.strip().strip('"').strip("'")
    return vals


def _check_env(r: Report, name: str, override: str, keys: tuple, why: str):
    path = config.env_file(name, override)
    if all(os.environ.get(k) for k in keys):
        return
    if not path.exists():
        r.errors.append(f"{path} is missing ({why}). Run `bin/curated-digest init`, "
                        f"then set {', '.join(keys)} in it.")
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        r.errors.append(f"{path} is mode {oct(mode)}; credentials must not be "
                        f"readable by others. Run `chmod 600 {path}`.")
    vals = _env_values(path)
    for k in keys:
        if not vals.get(k) or PLACEHOLDER_VALUE in vals.get(k, ""):
            r.errors.append(f"{k} is not set in {path} ({why}). Edit the file; "
                            "do not paste the value anywhere else.")


def check() -> Report:
    r = Report()
    home, tasks_dir = config.config_home(), config.tasks_dir()
    if not home.is_dir():
        r.errors.append(f"config home {home} does not exist. Run `bin/curated-digest init`.")
        return r
    try:
        cfg = config.load()
        cfg["rate_limit"]["signatures"], cfg["retry"]["max_attempts"]
    except (OSError, yaml.YAMLError, KeyError, TypeError) as e:
        r.errors.append(f"{config.config_path()} is unusable ({e}). Restore it from "
                        "the repo or fix CURATED_DIGEST_CONFIG.")
    if not tasks_dir.is_dir():
        r.errors.append(f"tasks directory {tasks_dir} does not exist. Run "
                        "`bin/curated-digest init --task rss-generic`.")
        return r
    tasks, broken = taskmod.load_all(tasks_dir)
    r.errors += [f"task file problem: {e}" for e in broken]
    if not tasks and not broken:
        r.warnings.append(f"no tasks in {tasks_dir}; nothing will ever run. Add one with "
                          f"`bin/curated-digest init --task TEMPLATE` ({', '.join(templates())}).")
    for t in tasks:
        left = [ln for ln in Path(t.path).read_text().splitlines()
                if REWRITE in ln and not ln.lstrip().startswith("#")]
        if left:
            r.warnings.append(f"{t.path}: {len(left)} {REWRITE} line(s) left from the "
                              "template; the model will judge against placeholder criteria.")
    if any(t.channel == "telegram" for t in tasks):
        _check_env(r, "telegram.env", "CURATED_DIGEST_TELEGRAM_ENV",
                   ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"), "Telegram delivery")
    if any(s.type == "reddit" for t in tasks for s in t.sources):
        _check_env(r, "reddit.env", "CURATED_DIGEST_REDDIT_ENV",
                   ("REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET"), "Reddit source")
    if any(t.model.startswith("gemini:") for t in tasks):
        _check_env(r, "gemini.env", "CURATED_DIGEST_GEMINI_KEY_FILE",
                   ("GEMINI_API_KEY",), "Gemini provider")
    if any(t.model.startswith("claude:") for t in tasks) and not shutil.which("claude"):
        r.errors.append("a task uses a claude: model but the `claude` CLI is not on PATH.")
    wd = config.env_file("curated-digest.env", "CURATED_DIGEST_ENV")
    ping = os.environ.get("CURATED_DIGEST_HC_PING_URL") or (
        _env_values(wd).get("CURATED_DIGEST_HC_PING_URL") if wd.exists() else "")
    if not ping or PLACEHOLDER_VALUE in ping:
        r.warnings.append(f"no watchdog URL in {wd}: a dead pipeline will look exactly "
                          "like a quiet one. Set CURATED_DIGEST_HC_PING_URL to a "
                          "dead-man's-switch URL.")
    return r


def require_tasks_dir():
    """Startup guard for the verbs that read tasks: a missing directory is a
    setup problem to fix, not an empty schedule to report as healthy."""
    d = config.tasks_dir()
    if not d.is_dir():
        raise SystemExit(f"curated-digest: tasks directory {d} does not exist. Run "
                         "`bin/curated-digest init --task TEMPLATE`, or set "
                         "CURATED_DIGEST_TASKS_DIR. `bin/curated-digest check` "
                         "validates the rest.")
    return d
