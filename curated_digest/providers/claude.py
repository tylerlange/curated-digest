"""Local Claude via `claude -p`. D21: start here, accepting that curated-digest competes
with the operator's own sessions for the same subscription usage window."""
import json
import subprocess
import time

from . import register
from .base import Completion, ProviderError, RateLimited

# R20/R21. Every one of these flags is load-bearing:
#   --allowed-tools ""       nothing is permitted
#   --disallowed-tools ...   named denial, so a future default-on tool cannot
#                            quietly widen the blast radius
#   --permission-mode manual no auto-approval path exists to be exploited
#   --strict-mcp-config      ignores every MCP server configured on this box
#   --system-prompt          REPLACES the default prompt, so a relevance check
#                            does not pay for CLAUDE.md and full auto memory on
#                            every invocation (R21)
#   --settings autoMemory    off, same reason
_DENY = ("Bash,Read,Write,Edit,NotebookEdit,Glob,Grep,WebFetch,WebSearch,"
         "Task,TodoWrite,SlashCommand,KillShell,BashOutput")


class ClaudeProvider:
    name = "claude"

    def complete(self, prompt, model, *, system=None, max_tokens=2048, timeout=180):
        cmd = [
            "claude", "-p", "--output-format", "json",
            "--model", model,
            "--allowed-tools", "",
            "--disallowed-tools", _DENY,
            "--permission-mode", "manual",
            "--strict-mcp-config",
            "--settings", json.dumps({"autoMemory": False}),
            "--exclude-dynamic-system-prompt-sections",
        ]
        if system:
            cmd += ["--system-prompt", system]

        # --allowed-tools and friends are variadic and will swallow a following
        # positional argument. The prompt always goes in on stdin.
        try:
            p = subprocess.run(cmd, input=prompt, capture_output=True,
                               text=True, timeout=timeout)
        except subprocess.TimeoutExpired as e:
            raise ProviderError(f"claude timed out after {timeout}s") from e

        blob = f"{p.stdout}\n{p.stderr}"
        if p.returncode != 0:
            from .. import ratelimit
            v = ratelimit.classify(blob)
            if v.is_rate_limit:
                raise RateLimited(blob.strip()[-500:], v.kind,
                                  v.resume_after or int(time.time() + 3600))
            raise ProviderError(f"claude exited {p.returncode}: {blob.strip()[-500:]}")

        try:
            out = json.loads(p.stdout)
        except json.JSONDecodeError as e:
            raise ProviderError(f"claude returned non-JSON: {p.stdout[:300]}") from e

        if out.get("is_error"):
            raise ProviderError(str(out.get("result"))[:500])
        return Completion(text=out.get("result") or "", model=model,
                          cost_usd=float(out.get("total_cost_usd") or 0.0))


register("claude", ClaudeProvider)
