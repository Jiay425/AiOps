from __future__ import annotations

import asyncio
import contextvars
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from datetime import datetime
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..schemas import now_iso


TEXT_SUFFIXES = {
    ".java", ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".kt", ".kts", ".xml", ".yml",
    ".yaml", ".json", ".toml", ".md", ".properties", ".sql", ".sh", ".ps1",
}
IGNORED_PARTS = {".git", ".idea", ".venv", "node_modules", "target", "build", "dist", "__pycache__"}


@dataclass
class ToolBudget:
    max_calls: int
    used_calls: int = 0
    per_tool: dict[str, int] = field(default_factory=dict)

    def consume(self, tool: str, repeat_limit: int = 8) -> None:
        if self.used_calls >= self.max_calls:
            raise PermissionError(f"Tool call budget exhausted ({self.used_calls}/{self.max_calls})")
        count = self.per_tool.get(tool, 0)
        if count >= repeat_limit:
            raise PermissionError(f"Tool repeat limit exceeded for {tool}: {count}/{repeat_limit}")
        self.used_calls += 1
        self.per_tool[tool] = count + 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SecurityDecision:
    allowed: bool
    reason: str
    risk: str
    requires_approval: bool = False


class SecurityPolicy:
    READ_TOOLS = {"list_files", "search_code", "read_file", "git_diff", "git_status", "detect_tests",
                  "repo.create_snapshot", "repo.search_text", "repo.list_files", "repo.read_file_snippet",
                  "repo.git_diff", "repo.git_log", "repo.find_tests", "task.background_status", "knowledge.search",
                  "ops.query_prometheus", "ops.search_logs", "ops.query_trace"}
    EXECUTE_TOOLS = {"run_tests", "compile", "repo.maven", "repo.maven_background"}
    MUTATION_TOOLS = {"sandbox_patch", "apply_patch"}

    BLOCKED_PATTERNS = ("rm -rf", "rm -r", "sudo", "chmod", "chown", "DROP TABLE", "DELETE FROM",
                        "TRUNCATE", "> /dev/", "dd if=", "mkfs", ":(){ :|:& };:", "wget", "curl.*-o", "eval", "$(",
                        "/etc/passwd", "/etc/shadow", ".ssh/", ".env")
    ALLOWED_COMMANDS = {"mvn", "git", "java", "javac", "ls", "cat", "head", "tail", "grep",
                        "find", "diff", "wc", "echo", "mkdir"}

    def authorize(self, tool: str, *, approved: bool = False, command: str = "") -> SecurityDecision:
        if tool in self.READ_TOOLS:
            return SecurityDecision(True, "Read-only repository operation", "LOW")
        if tool in self.EXECUTE_TOOLS:
            allowed = not command or self.is_command_allowed(command)
            return SecurityDecision(allowed, "Allowlisted build/test operation" if allowed else "Command denied by policy", "MEDIUM")
        if tool in {"sandbox_patch", "repo.exact_replace", "artifact.generate_review_report"}:
            return SecurityDecision(True, "Mutation is isolated in a sandbox", "MEDIUM")
        if tool == "apply_patch":
            return SecurityDecision(approved, "Human approval is required for source mutation", "HIGH", not approved)
        return SecurityDecision(False, f"Unknown or denied tool: {tool}", "HIGH")

    def is_command_allowed(self, command: str) -> bool:
        normalized = (command or "").strip().lower()
        if not normalized or any(pattern.lower() in normalized for pattern in self.BLOCKED_PATTERNS):
            return False
        return normalized.split()[0] in self.ALLOWED_COMMANDS

    @staticmethod
    def is_write_allowed(repository: str | Path, relative_path: str, *, source_only: bool = False) -> bool:
        if not repository or not relative_path or Path(relative_path).is_absolute():
            return False
        root = Path(repository).resolve()
        target = (root / relative_path).resolve()
        try:
            target.relative_to(root / "src" if source_only else root)
        except ValueError:
            return False
        return not any(part.lower() in {".git", ".ssh"} for part in target.parts) and target.name.lower() != ".env"

    def governance_summary(self) -> dict[str, Any]:
        return {"policyVersion": "codeops-agent-permission-v1",
                "layers": ["read scope", "write scope", "command allowlist", "blocked dangerous patterns",
                           "patch guard", "snapshot rollback", "human approval"],
                "allowedCommands": sorted(self.ALLOWED_COMMANDS), "blockedPatterns": list(self.BLOCKED_PATTERNS),
                "writeScope": "repository src/** only",
                "blockedWriteTargets": [".env", ".ssh/**", "pom.xml without approval", "scripts/config secrets"],
                "patchGuardEnabled": True, "rollbackEnabled": True,
                "defaultApprovalRule": "HIGH/CRITICAL risk patch or guardrail reasons require human approval"}


@dataclass(frozen=True)
class EngineeringToolDefinition:
    tool_name: str
    description: str
    category: str
    risk_level: str
    access_level: str
    source_type: str
    enabled: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"toolName": self.tool_name, "description": self.description, "category": self.category,
                "riskLevel": self.risk_level, "accessLevel": self.access_level,
                "sourceType": self.source_type, "enabled": self.enabled}


class ToolRuntime:
    """Task-bound, bounded and secret-sanitized equivalent of ToolRuntimeService."""

    _scope: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("codeops_tool_scope", default=None)

    def __init__(self):
        self._recent: list[dict[str, Any]] = []

    def bind(self, task: dict[str, Any], *, trace_id: str = "", execution_id: str = "", agent_or_skill: str = ""):
        return self._scope.set({"task": task, "taskId": task.get("taskId", ""), "traceId": trace_id,
                                "executionId": execution_id, "agentOrSkill": agent_or_skill})

    def clear(self, token=None) -> None:
        if token is None:
            self._scope.set(None)
        else:
            self._scope.reset(token)

    def current_task(self) -> dict[str, Any] | None:
        """Return the task bound to the current tool invocation, if any."""
        task = (self._scope.get() or {}).get("task")
        return task if isinstance(task, dict) else None

    def begin(self, definition: EngineeringToolDefinition, request_summary: str,
              metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        scope = self._scope.get() or {}
        return {"toolCallId": str(uuid.uuid4()), "taskId": scope.get("taskId", ""),
                "traceId": scope.get("traceId", ""), "executionId": scope.get("executionId", ""),
                "agentOrSkill": scope.get("agentOrSkill", ""), "toolName": definition.tool_name,
                "logicalToolName": definition.description, "category": definition.category,
                "accessLevel": definition.access_level, "sourceType": definition.source_type,
                "status": "RUNNING", "success": False, "requestSummary": self._sanitize_text(request_summary),
                "responseSummary": "", "errorType": "", "errorMessage": "", "costMillis": 0,
                "startTime": datetime.now().isoformat(), "endTime": None,
                "metadata": self._sanitize(metadata or {}), "_started": time.monotonic()}

    def finish(self, record: dict[str, Any], status: str, response: str = "", error: Exception | None = None) -> dict[str, Any]:
        record.update({"status": status, "success": status == "SUCCESS",
                       "responseSummary": self._sanitize_text(response),
                       "errorType": type(error).__name__ if error else "",
                       "errorMessage": self._sanitize_text(str(error)) if error else "",
                       "costMillis": int((time.monotonic() - record.pop("_started", time.monotonic())) * 1000),
                       "endTime": datetime.now().isoformat()})
        clean = dict(record)
        self._recent.insert(0, clean)
        del self._recent[500:]
        scope = self._scope.get() or {}
        task = scope.get("task")
        if isinstance(task, dict):
            context = task.setdefault("context", {})
            trace = context.setdefault("toolRuntimeTrace", [])
            trace.append(clean)
            del trace[:-200]
        return clean

    def list_recent(self, limit: int = 20) -> list[dict[str, Any]]:
        return self._recent[:max(1, min(limit, 500))]

    @classmethod
    def _sanitize_text(cls, value: str) -> str:
        text = re.sub(r"(?i)(api[_-]?key|token|secret|password|authorization)\s*[:=]\s*\S+", r"\1=***", str(value or ""))
        text = re.sub(r"sk-[A-Za-z0-9_-]{12,}", "sk-***", text)
        return text if len(text) <= 1200 else text[:1200] + "...truncated..."

    @classmethod
    def _sanitize(cls, value: Any, key: str = "") -> Any:
        if any(part in key.lower() for part in ("key", "token", "secret", "password", "authorization")):
            return "***"
        if isinstance(value, dict):
            return {str(k): cls._sanitize(v, str(k)) for k, v in value.items()}
        if isinstance(value, list):
            return [cls._sanitize(item, key) for item in value]
        return cls._sanitize_text(value) if isinstance(value, str) else value


class RepositoryToolkit:
    def __init__(self, root: str | Path, budget: ToolBudget | None = None):
        self.root = Path(root or ".").resolve()
        if not self.root.is_dir():
            raise ValueError(f"Repository does not exist: {self.root}")
        self.budget = budget or ToolBudget(20)

    def list_files(self, limit: int = 1000) -> list[str]:
        self.budget.consume("list_files")
        result: list[str] = []
        for path in self.root.rglob("*"):
            if len(result) >= limit:
                break
            if path.is_file() and not any(part in IGNORED_PARTS for part in path.parts):
                result.append(path.relative_to(self.root).as_posix())
        return sorted(result)

    def create_snapshot(self, limit: int = 5000) -> dict[str, str]:
        self.budget.consume("repo.create_snapshot")
        snapshot: dict[str, str] = {}
        for path in self._text_files():
            if len(snapshot) >= limit:
                break
            try:
                snapshot[path.relative_to(self.root).as_posix()] = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
        return snapshot

    def list_files_by_pattern(self, pattern: str = "**/*", limit: int = 1000) -> list[str]:
        self.budget.consume("repo.list_files")
        values = []
        for path in self.root.rglob("*"):
            relative = path.relative_to(self.root).as_posix()
            if path.is_file() and not any(part in IGNORED_PARTS for part in path.parts) and fnmatch.fnmatch(relative, pattern):
                values.append(relative)
                if len(values) >= limit:
                    break
        return sorted(values)

    def search(self, terms: list[str], *, limit: int = 100, context_lines: int = 2) -> list[dict[str, Any]]:
        self.budget.consume("search_code")
        normalized = [term.lower() for term in terms if term and len(term.strip()) >= 2]
        matches: list[dict[str, Any]] = []
        for path in self._text_files():
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for index, line in enumerate(lines):
                hits = [term for term in normalized if term in line.lower()]
                if not hits:
                    continue
                start, end = max(0, index - context_lines), min(len(lines), index + context_lines + 1)
                matches.append({
                    "file": path.relative_to(self.root).as_posix(), "line": index + 1, "hits": hits,
                    "snippet": "\n".join(f"{i + 1}: {lines[i]}" for i in range(start, end)),
                })
                if len(matches) >= limit:
                    return matches
        return matches

    def read(self, relative_path: str, *, start_line: int = 1, end_line: int = 400) -> dict[str, Any]:
        self.budget.consume("read_file")
        path = self.safe_path(relative_path)
        if path.suffix.lower() not in TEXT_SUFFIXES:
            raise PermissionError(f"Unsupported source type: {path.suffix}")
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, start_line)
        end = min(len(lines), max(start, end_line))
        return {"file": relative_path, "startLine": start, "endLine": end,
                "content": "\n".join(f"{i + 1}: {lines[i]}" for i in range(start - 1, end))}

    def git_diff(self, change_ref: str = "") -> dict[str, Any]:
        self.budget.consume("git_diff")
        args = ["git", "diff", "--no-ext-diff", "--unified=3"]
        if change_ref:
            args.append(change_ref)
        completed = self._run(args, timeout=30)
        return {"exitCode": completed.returncode, "diff": completed.stdout[-50000:], "error": completed.stderr[-4000:]}

    def git_status(self) -> list[str]:
        self.budget.consume("git_status")
        completed = self._run(["git", "status", "--short"], timeout=15)
        return completed.stdout.splitlines()

    def git_log(self, limit: int = 20) -> list[dict[str, str]]:
        self.budget.consume("repo.git_log")
        completed = self._run(["git", "log", f"-{max(1, min(limit, 100))}", "--pretty=format:%H%x1f%an%x1f%aI%x1f%s"], 30)
        if completed.returncode != 0:
            return []
        result = []
        for line in completed.stdout.splitlines():
            parts = line.split("\x1f", 3)
            if len(parts) == 4:
                result.append(dict(zip(("commit", "author", "time", "subject"), parts)))
        return result

    def find_tests(self, changed_files: list[str], limit: int = 100) -> list[str]:
        self.budget.consume("repo.find_tests")
        stems = {Path(item).stem.lower().removeprefix("test_").removesuffix("test") for item in changed_files}
        candidates = []
        for path in self.root.rglob("*"):
            if not path.is_file() or any(part in IGNORED_PARTS for part in path.parts):
                continue
            relative, name = path.relative_to(self.root).as_posix(), path.name.lower()
            if ("test" in name or "test" in {part.lower() for part in path.parts}) and any(stem and stem in name for stem in stems):
                candidates.append(relative)
                if len(candidates) >= limit:
                    break
        return sorted(candidates)

    def safe_path(self, relative_path: str) -> Path:
        if not relative_path or Path(relative_path).is_absolute():
            raise PermissionError("Only a non-empty repository-relative path is allowed")
        resolved = (self.root / relative_path).resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise PermissionError(f"Path escapes repository: {relative_path}") from exc
        if not resolved.is_file():
            raise FileNotFoundError(relative_path)
        return resolved

    def _text_files(self):
        for path in self.root.rglob("*"):
            if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES and not any(part in IGNORED_PARTS for part in path.parts):
                try:
                    if path.stat().st_size <= 2_000_000:
                        yield path
                except OSError:
                    continue

    def _run(self, args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
        return subprocess.run(args, cwd=self.root, capture_output=True, text=True, timeout=timeout, shell=False)


@dataclass
class FilePatch:
    path: str
    old: str
    new: str


@dataclass
class PatchProposal:
    summary: str
    patches: list[FilePatch]
    tests: list[str] = field(default_factory=list)
    rationale: str = ""

    @classmethod
    def from_llm(cls, content: str) -> "PatchProposal":
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
        candidate = fenced.group(1) if fenced else content[content.find("{"):content.rfind("}") + 1]
        data = json.loads(candidate)
        patches = [FilePatch(path=str(item["path"]), old=str(item["old"]), new=str(item["new"]))
                   for item in data.get("patches", [])]
        return cls(summary=str(data.get("summary", "")), patches=patches,
                   tests=[str(item) for item in data.get("tests", [])], rationale=str(data.get("rationale", "")))

    def to_dict(self) -> dict[str, Any]:
        return {"summary": self.summary, "patches": [asdict(item) for item in self.patches],
                "tests": self.tests, "rationale": self.rationale}


class PatchScopeGuard:
    def validate(self, repository: str | Path, proposal: PatchProposal,
                 repair_scope: dict[str, Any] | None = None) -> dict[str, Any]:
        scope = repair_scope or {}
        scope_type = str(scope.get("scopeType", "FULL_FILE")).upper()
        touched = list(dict.fromkeys(item.path.replace("\\", "/") for item in proposal.patches))
        target_files = [str(item).replace("\\", "/") for item in scope.get("targetFiles", [])]
        target_methods = [str(item) for item in scope.get("targetMethods", [])]
        violations: list[str] = []
        changed_methods: list[str] = []
        if scope_type == "NO_CODE_FIX" and touched:
            violations.append("NO_CODE_FIX_PATCH: NO_CODE_FIX scope prohibits any patch")
        root = Path(repository).resolve()
        for patch in proposal.patches:
            if not SecurityPolicy.is_write_allowed(root, patch.path):
                violations.append(f"TOUCHED_FILE_OUT_OF_SCOPE: {patch.path} escapes repository or is protected")
                continue
            normalized = patch.path.replace("\\", "/")
            if target_files and not any(normalized == item or normalized.endswith("/" + item) or item.endswith("/" + normalized)
                                        for item in target_files):
                violations.append(f"TOUCHED_FILE_OUT_OF_SCOPE: {normalized} not in repairScope.targetFiles {target_files}")
            changed_methods.extend(self._changed_methods(normalized, patch.old, patch.new))
        if scope_type in {"STRICT_SINGLE_METHOD", "MULTI_METHOD"} and proposal.patches and not changed_methods:
            violations.append("DETECTION_FAILED: Cannot detect changed methods from patch")
        if scope_type == "STRICT_SINGLE_METHOD" and len({item for item in changed_methods if not item.endswith(".<STRUCTURE>")}) > 1:
            violations.append("METHOD_OUT_OF_SCOPE: STRICT_SINGLE_METHOD patch changes multiple methods")
        if scope_type in {"STRICT_SINGLE_METHOD", "MULTI_METHOD"} and target_methods:
            for method in changed_methods:
                bare = method.rsplit(".", 1)[-1]
                if bare != "<STRUCTURE>" and not any(method == item or method.endswith("." + item) or item.endswith("." + bare)
                                                      for item in target_methods):
                    violations.append(f"METHOD_OUT_OF_SCOPE: {method} not in {target_methods}")
        failure = "" if not violations else violations[0].split(":", 1)[0]
        return {"passed": not violations, "failureType": failure, "touchedFiles": touched,
                "changedMethods": list(dict.fromkeys(changed_methods)), "violations": violations,
                "repairScope": scope}

    @staticmethod
    def _changed_methods(path: str, old: str, new: str) -> list[str]:
        class_name = Path(path).stem
        patterns = [
            r"(?m)^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(",
            r"(?m)^\s*(?:public|protected|private|static|final|synchronized|\s)+[\w<>\[\], ?]+\s+([A-Za-z_]\w*)\s*\([^;]*\)\s*(?:throws[^\{]+)?\{",
            r"(?m)^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(",
        ]
        old_names, new_names = set(), set()
        for pattern in patterns:
            old_names.update(re.findall(pattern, old))
            new_names.update(re.findall(pattern, new))
        changed = sorted(old_names ^ new_names)
        if not changed and old != new:
            common = old_names & new_names
            changed = sorted(common) if len(common) == 1 else ["<STRUCTURE>"]
        return [f"{class_name}.{name}" for name in changed]


class PatchValidation:
    def validate(self, repository: str | Path, unified_diff: str) -> dict[str, Any]:
        diff = (unified_diff or "").replace("\r\n", "\n").replace("\r", "\n")
        errors, warnings, touched = [], [], []
        old_header = new_header = hunk = False
        dev_null = False
        for line in diff.splitlines():
            if line.startswith("--- "):
                old_header = True
                path = self._path(line[4:])
            elif line.startswith("+++ "):
                new_header = True
                path = self._path(line[4:])
            else:
                path = ""
            if path == "/dev/null":
                dev_null = True
            elif path and path not in touched:
                touched.append(path)
            hunk = hunk or line.startswith("@@")
        if not diff.strip():
            errors.append("patch is empty")
        if not old_header or not new_header:
            errors.append("unified diff must contain both --- and +++ file headers")
        if not hunk:
            warnings.append("patch does not contain a standard @@ hunk header")
        if not touched:
            errors.append("patch does not contain any touched file path")
        if dev_null and not all(item.replace("\\", "/").startswith("src/test/") for item in touched):
            errors.append("patch references /dev/null; creating or deleting files is not allowed in incident fix proposal")
        root, existing, missing = Path(repository).resolve(), [], []
        for item in touched:
            target = (root / item).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                errors.append(f"patch path escapes repository: {item}")
                continue
            (existing if target.is_file() else missing).append(item)
        if touched and not existing and not (dev_null and all(item.startswith("src/test/") for item in touched)):
            errors.append("patch does not reference an existing repository file")
        if missing:
            warnings.append("patch references files not found in repository: " + ", ".join(missing))
        return {"patchPresent": bool(diff.strip()), "valid": not errors, "repositoryPath": str(root),
                "touchedFiles": touched, "existingTouchedFiles": existing, "missingTouchedFiles": missing,
                "errors": errors, "warnings": warnings}

    @staticmethod
    def _path(raw: str) -> str:
        path = raw.strip().split(maxsplit=1)[0].strip('"').replace("\\", "/")
        return path[2:] if path.startswith(("a/", "b/")) else path


class PatchDiffAnalysis:
    def analyze(self, diff: str, validation: dict[str, Any], guard: dict[str, Any]) -> dict[str, Any]:
        files = list(dict.fromkeys([*validation.get("touchedFiles", []), *guard.get("touchedFiles", [])]))
        methods = guard.get("changedMethods", [])
        lower = [item.lower().replace("\\", "/") for item in files]
        sensitive = [file for file, name in zip(files, lower) if self._sensitive(name)]
        additions = sum(1 for line in diff.splitlines() if line.startswith("+") and not line.startswith("+++"))
        deletions = sum(1 for line in diff.splitlines() if line.startswith("-") and not line.startswith("---"))
        score = max(0, min(100, 100 - max(0, len(files) - 2) * 10 - max(0, len(methods) - 3) * 8
                           - max(0, additions + deletions - 60) // 3 - len(sensitive) * 35))
        warnings = []
        if not guard.get("passed", True):
            warnings.append("PatchScopeGuard did not pass; patch is not scope aligned.")
        if sensitive:
            warnings.append("Patch touches sensitive file(s): " + ", ".join(sensitive))
        if len(files) > 5:
            warnings.append("Patch touches more than 5 files; review minimality.")
        if len(methods) > 6:
            warnings.append("Patch changes more than 6 methods; review scope.")
        if additions + deletions > 120:
            warnings.append("Patch changes more than 120 lines; review minimality.")
        return {"touchedFiles": files, "changedMethods": methods,
                "productionFileCount": sum(item.startswith("src/main/") for item in lower),
                "testFileCount": sum(item.startswith("src/test/") or "test" in Path(item).name for item in lower),
                "configFileCount": sum(self._config(item) for item in lower),
                "scriptFileCount": sum(Path(item).suffix in {".sh", ".ps1", ".bat", ".cmd"} for item in lower),
                "sensitiveFileCount": len(sensitive), "hunkCount": sum(line.startswith("@@") for line in diff.splitlines()),
                "additions": additions, "deletions": deletions, "staticSafetyPassed": not sensitive,
                "scopeAligned": guard.get("passed", True), "testsChanged": any("test" in item for item in lower),
                "minimalChangeScore": score, "requiresHumanApproval": bool(sensitive) or score < 60,
                "sensitiveFiles": sensitive, "qualityWarnings": warnings}

    @staticmethod
    def _config(path: str) -> bool:
        return Path(path).suffix in {".yml", ".yaml", ".properties", ".xml"} or "/config/" in path

    @classmethod
    def _sensitive(cls, path: str) -> bool:
        return ".env" in path or Path(path).name in {"pom.xml", "build.gradle", "settings.gradle"} or cls._config(path) or Path(path).suffix in {".sh", ".ps1", ".bat", ".cmd"}


@dataclass
class SandboxResult:
    success: bool
    sandbox: str
    changed_files: list[str]
    diff: str
    errors: list[str]
    checksums: dict[str, str]
    mode: str = "COPY_SANDBOX"
    branch_name: str = ""
    command: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PatchSandbox:
    def __init__(self, repository: str | Path, base_dir: str | Path | None = None,
                 prefer_git_worktree: bool = False, timeout_ms: int = 30000, task_id: str = "incident"):
        self.repository = Path(repository).resolve()
        if not self.repository.is_dir():
            raise ValueError(f"Repository does not exist: {self.repository}")
        self.base_dir = Path(base_dir).resolve() if base_dir else None
        self.prefer_git_worktree = prefer_git_worktree
        self.timeout_ms = max(1, timeout_ms)
        self.task_id = re.sub(r"[^a-z0-9._-]+", "-", (task_id or "incident").lower())

    def apply(self, proposal: PatchProposal) -> SandboxResult:
        sandbox = Path(tempfile.mkdtemp(prefix="codeops-", dir=self.base_dir))
        workspace = sandbox / "workspace"
        mode, branch_name, command = "COPY_SANDBOX", "", []
        if self.prefer_git_worktree and (self.repository / ".git").exists():
            branch_name = f"codeops/{self.task_id}"
            command = ["git", "worktree", "add", "-B", branch_name, str(workspace), "HEAD"]
            try:
                completed = subprocess.run(command, cwd=self.repository, capture_output=True, text=True,
                                           timeout=self.timeout_ms / 1000, shell=False)
                if completed.returncode == 0:
                    mode = "GIT_WORKTREE"
                else:
                    branch_name, command = "", []
            except (OSError, subprocess.TimeoutExpired):
                branch_name, command = "", []
        if mode == "COPY_SANDBOX":
            if workspace.exists():
                shutil.rmtree(workspace)
            shutil.copytree(self.repository, workspace,
                            ignore=shutil.ignore_patterns(".git", "target", "build", ".gradle", ".idea",
                                                          "node_modules", ".venv", "data"))
        manifest = {"enabled": True, "isolated": True, "mode": mode,
                    "originalRepositoryPath": str(self.repository), "sandboxRepositoryPath": str(workspace),
                    "branchName": branch_name, "command": command, "taskId": self.task_id,
                    "purpose": "CodeOps patch sandbox. Patches, compile gates and tests run here before any human decision."}
        (workspace / "CODEOPS_SANDBOX_MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                                                  encoding="utf-8")
        errors: list[str] = []
        changed: list[str] = []
        checksums: dict[str, str] = {}
        for patch in proposal.patches:
            try:
                target = self._safe_target(workspace, patch.path)
                original = target.read_text(encoding="utf-8")
                if not patch.old:
                    raise ValueError("Exact-replace patch requires non-empty old text")
                count = original.count(patch.old)
                if count != 1:
                    raise ValueError(f"Expected exactly one match, found {count}")
                updated = original.replace(patch.old, patch.new, 1)
                target.write_text(updated, encoding="utf-8", newline="")
                changed.append(patch.path)
                checksums[patch.path] = hashlib.sha256(updated.encode()).hexdigest()
            except Exception as exc:
                errors.append(f"{patch.path}: {exc}")
        diff = self._unified_diff(workspace, changed)
        return SandboxResult(not errors and bool(changed), str(workspace), changed, diff, errors, checksums,
                             mode, branch_name, command)

    def apply_to_repository(self, proposal: PatchProposal, expected_checksums: dict[str, str]) -> list[str]:
        changed: list[str] = []
        for patch in proposal.patches:
            target = self._safe_target(self.repository, patch.path)
            original = target.read_text(encoding="utf-8")
            if original.count(patch.old) != 1:
                raise RuntimeError(f"Repository changed since proposal; exact match failed: {patch.path}")
            updated = original.replace(patch.old, patch.new, 1)
            digest = hashlib.sha256(updated.encode()).hexdigest()
            expected = expected_checksums.get(patch.path)
            if expected and digest != expected:
                raise RuntimeError(f"Sandbox checksum mismatch: {patch.path}")
            target.write_text(updated, encoding="utf-8", newline="")
            changed.append(patch.path)
        return changed

    @staticmethod
    def cleanup(path: str) -> None:
        workspace = Path(path).resolve()
        sandbox = workspace.parent
        if workspace.name != "workspace" or not sandbox.name.startswith("codeops-"):
            raise PermissionError(f"Refusing to clean unexpected sandbox path: {workspace}")
        manifest_path = workspace / "CODEOPS_SANDBOX_MANIFEST.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
        original = Path(manifest.get("originalRepositoryPath", ""))
        if manifest.get("mode") == "GIT_WORKTREE" and original.is_dir():
            subprocess.run(["git", "worktree", "remove", "--force", str(workspace)], cwd=original,
                           capture_output=True, text=True, timeout=30, shell=False)
        if sandbox.exists():
            shutil.rmtree(sandbox)

    @staticmethod
    def _safe_target(root: Path, relative: str) -> Path:
        if Path(relative).is_absolute() or not relative:
            raise PermissionError("Patch path must be repository-relative")
        target = (root / relative).resolve()
        try:
            target.relative_to(root.resolve())
        except ValueError as exc:
            raise PermissionError(f"Patch path escapes repository: {relative}") from exc
        if not target.is_file() or target.suffix.lower() not in TEXT_SUFFIXES:
            raise PermissionError(f"Patch target is not an allowed source file: {relative}")
        return target

    def _unified_diff(self, workspace: Path, changed: list[str]) -> str:
        import difflib

        result: list[str] = []
        for relative in changed:
            before = (self.repository / relative).read_text(encoding="utf-8").splitlines(keepends=True)
            after = (workspace / relative).read_text(encoding="utf-8").splitlines(keepends=True)
            result.extend(difflib.unified_diff(before, after, fromfile=f"a/{relative}", tofile=f"b/{relative}"))
        return "".join(result)


@dataclass
class TestResult:
    command: list[str]
    status: str
    exit_code: int | None
    output: str
    duration_ms: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TestRunner:
    ALLOWED_EXECUTABLES = {"python", "python.exe", "mvn", "mvn.cmd", "npm", "npm.cmd", "gradle", "gradlew", "gradlew.bat"}

    def detect(self, root: str | Path) -> list[str] | None:
        path = Path(root)
        if (path / "pyproject.toml").exists() or (path / "pytest.ini").exists():
            return [os.environ.get("PYTHON", "python"), "-m", "pytest", "-q"]
        if (path / "mvnw.cmd").exists():
            return [str(path / "mvnw.cmd"), "-q", "test"]
        if (path / "mvnw").exists():
            return [str(path / "mvnw"), "-q", "test"]
        if (path / "pom.xml").exists():
            return ["mvn", "-q", "test"]
        if (path / "gradlew.bat").exists():
            return [str(path / "gradlew.bat"), "test"]
        if (path / "package.json").exists():
            return ["npm", "test", "--", "--runInBand"]
        return None

    async def run(self, root: str | Path, command: list[str] | None = None, timeout_seconds: int = 120) -> TestResult:
        root_path = Path(root).resolve()
        selected = command or self.detect(root_path)
        if not selected:
            return TestResult([], "SKIPPED", None, "No supported test runner detected", 0)
        executable = Path(selected[0]).name.lower()
        if executable not in self.ALLOWED_EXECUTABLES:
            raise PermissionError(f"Test executable is not allowlisted: {executable}")
        loop = asyncio.get_running_loop()
        started = loop.time()
        process = await asyncio.create_subprocess_exec(
            *selected, cwd=root_path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        try:
            output, _ = await asyncio.wait_for(process.communicate(), timeout=timeout_seconds)
            status = "PASSED" if process.returncode == 0 else "FAILED"
            exit_code = process.returncode
        except TimeoutError:
            process.kill()
            output, _ = await process.communicate()
            status, exit_code = "TIMEOUT", None
        duration = int((loop.time() - started) * 1000)
        return TestResult(selected, status, exit_code, output.decode(errors="replace")[-20000:], duration)


class EngineeringToolGateway:
    """Named tool registry matching the Java gateway contract and recording every invocation."""

    DEFINITIONS = (
        EngineeringToolDefinition("repo.create_snapshot", "Create repository source snapshot", "repository", "READ_ONLY", "READ_ONLY", "LOCAL_REPOSITORY"),
        EngineeringToolDefinition("repo.search_text", "Search code text in the target repository", "repository", "READ_ONLY", "READ_ONLY", "LOCAL_REPOSITORY"),
        EngineeringToolDefinition("repo.list_files", "List repository files by pattern", "repository", "READ_ONLY", "READ_ONLY", "LOCAL_REPOSITORY"),
        EngineeringToolDefinition("repo.read_file_snippet", "Read a bounded source file snippet", "repository", "READ_ONLY", "READ_ONLY", "LOCAL_REPOSITORY"),
        EngineeringToolDefinition("repo.git_diff", "Read current diff or a specified change ref", "repository", "READ_ONLY", "READ_ONLY", "LOCAL_REPOSITORY"),
        EngineeringToolDefinition("repo.git_log", "Read recent git history", "repository", "READ_ONLY", "READ_ONLY", "LOCAL_REPOSITORY"),
        EngineeringToolDefinition("repo.find_tests", "Find tests related to changed code", "repository", "READ_ONLY", "READ_ONLY", "LOCAL_REPOSITORY"),
        EngineeringToolDefinition("repo.maven", "Run Maven verification command under permission policy", "command", "MEDIUM", "COMMAND_EXECUTE", "LOCAL_COMMAND"),
        EngineeringToolDefinition("repo.maven_background", "Start Maven verification command as a background task", "command", "MEDIUM", "COMMAND_EXECUTE", "LOCAL_COMMAND"),
        EngineeringToolDefinition("repo.exact_replace", "Replace exact text in a source file and return structured mismatch feedback", "repository", "MEDIUM", "SOURCE_WRITE", "LOCAL_REPOSITORY"),
        EngineeringToolDefinition("task.background_status", "Read background tool task status", "task", "READ_ONLY", "READ_ONLY", "LOCAL_MEMORY"),
        EngineeringToolDefinition("knowledge.search", "Search engineering knowledge documents", "knowledge", "READ_ONLY", "READ_ONLY", "LOCAL_REPOSITORY"),
        EngineeringToolDefinition("ops.query_prometheus", "Query metrics for online diagnosis", "observability", "READ_ONLY", "EXTERNAL_CALL", "REAL_GATEWAY"),
        EngineeringToolDefinition("ops.search_logs", "Search logs for online diagnosis", "observability", "READ_ONLY", "EXTERNAL_CALL", "REAL_GATEWAY"),
        EngineeringToolDefinition("ops.query_trace", "Query trace evidence for online diagnosis", "observability", "READ_ONLY", "EXTERNAL_CALL", "REAL_GATEWAY"),
        EngineeringToolDefinition("artifact.generate_review_report", "Generate review report draft", "artifact", "LOW_RISK_WRITE", "LOW_RISK_WRITE", "SANDBOX"),
    )
    REGISTRY_TOOLS = {"repo.create_snapshot", "repo.search_text", "repo.read_file_snippet", "repo.git_diff",
                      "repo.maven", "repo.maven_background", "repo.exact_replace", "task.background_status"}

    def __init__(self, runtime: ToolRuntime | None = None, security: SecurityPolicy | None = None,
                 external_adapters: dict[str, Any] | None = None):
        self.runtime = runtime or ToolRuntime()
        self.security = security or SecurityPolicy()
        self.external_adapters = external_adapters or {}
        self._background: dict[str, dict[str, Any]] = {}
        self._definitions = {item.tool_name: item for item in self.DEFINITIONS}

    def list_tools(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self.DEFINITIONS]

    def is_tool_allowed(self, tool_name: str) -> bool:
        return tool_name in self._definitions and self._definitions[tool_name].enabled

    def list_registered_tools(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self.DEFINITIONS if item.tool_name in self.REGISTRY_TOOLS]

    def is_registered_tool(self, tool_name: str) -> bool:
        return tool_name in self.REGISTRY_TOOLS and self.is_tool_allowed(tool_name)

    async def invoke(self, tool_name: str, arguments: dict[str, Any], *, budget: ToolBudget | None = None,
                     approved: bool = False) -> Any:
        definition = self._definitions.get(tool_name)
        if definition is None:
            raise KeyError(f"Unknown engineering tool: {tool_name}")
        command = " ".join(str(item) for item in arguments.get("args", []))
        decision = self.security.authorize(tool_name, approved=approved, command=f"mvn {command}" if tool_name.startswith("repo.maven") else command)
        if tool_name == "repo.exact_replace" and not self.security.is_write_allowed(
                arguments.get("repository", "."), str(arguments.get("filePath", "")), source_only=True):
            decision = SecurityDecision(False, "Write target is outside allowed repository source scope", "HIGH")
        record = self.runtime.begin(definition, json.dumps(arguments, ensure_ascii=False), arguments)
        if not decision.allowed:
            self.runtime.finish(record, "DENIED", decision.reason)
            raise PermissionError(decision.reason)
        selected_budget = budget or ToolBudget(20)
        try:
            result = await self._invoke_local(tool_name, arguments, selected_budget)
            self.runtime.finish(record, "SUCCESS", self._summary(result))
            return result
        except TimeoutError as exc:
            self.runtime.finish(record, "TIMEOUT", str(exc), exc)
            raise
        except Exception as exc:
            self.runtime.finish(record, "FAILED", str(exc), exc)
            raise

    async def _invoke_local(self, tool_name: str, args: dict[str, Any], budget: ToolBudget) -> Any:
        repository = args.get("repository", ".")
        toolkit = RepositoryToolkit(repository, budget) if tool_name.startswith(("repo.", "knowledge.")) else None
        if tool_name == "repo.create_snapshot":
            return toolkit.create_snapshot()
        if tool_name == "repo.search_text":
            return toolkit.search([str(item) for item in args.get("queries", [])], limit=int(args.get("maxMatches", 100)))
        if tool_name == "repo.list_files":
            return toolkit.list_files_by_pattern(str(args.get("pattern", "**/*")), int(args.get("limit", 1000)))
        if tool_name == "repo.read_file_snippet":
            center, radius = int(args.get("centerLine", 1)), max(1, int(args.get("radius", 20)))
            return toolkit.read(str(args.get("filePath", "")), start_line=center - radius, end_line=center + radius)
        if tool_name == "repo.git_diff":
            return toolkit.git_diff(str(args.get("changeRef", "")))
        if tool_name == "repo.git_log":
            return toolkit.git_log(int(args.get("limit", 20)))
        if tool_name == "repo.find_tests":
            return toolkit.find_tests([str(item) for item in args.get("changedFiles", [])])
        if tool_name == "knowledge.search":
            matches = toolkit.search([str(item) for item in args.get("queries", [args.get("query", "")])], limit=int(args.get("limit", 20)))
            return [item for item in matches if Path(item["file"]).suffix.lower() in {".md", ".txt", ".yml", ".yaml"}]
        if tool_name == "repo.maven":
            budget.consume("repo.maven")
            command = ["mvn.cmd" if os.name == "nt" else "mvn", *[str(item) for item in args.get("args", [])]]
            return (await TestRunner().run(repository, command, max(1, int(args.get("timeoutMillis", 120000))) // 1000)).to_dict()
        if tool_name == "repo.maven_background":
            budget.consume("repo.maven_background")
            task = self.runtime.current_task()
            command_args = [str(item) for item in args.get("args", [])]
            record = self._new_background_task(task, str(args.get("nodeId") or ""), command_args)
            self._background[record["backgroundTaskId"]] = record
            self._upsert_background_task(task, record)
            self._add_background_notification(task, record, "BACKGROUND_TASK_STARTED",
                                              "后台工具任务已开始：" + record["requestSummary"],
                                              {"timeoutMillis": int(args.get("timeoutMillis", 120000))})

            async def execute() -> None:
                try:
                    command = ["mvn.cmd" if os.name == "nt" else "mvn", *command_args]
                    result = (await TestRunner().run(repository, command,
                                                      max(1, int(args.get("timeoutMillis", 120000))) // 1000)).to_dict()
                    success = result["status"] == "PASSED"
                    record.update(status="SUCCESS" if success else "FAILED",
                                  resultSummary=f"exitCode={result['exit_code']}, costMillis={result['duration_ms']}",
                                  errorMessage="" if success else str(result.get("output") or ""),
                                  command=result.get("command", command),
                                  artifacts={"exitCode": result["exit_code"], "costMillis": result["duration_ms"],
                                             "output": str(result.get("output") or "")[:1200]}, updateTime=now_iso())
                    self._upsert_background_task(task, record)
                    self._add_background_notification(task, record,
                                                      "BACKGROUND_TASK_FINISHED" if success else "BACKGROUND_TASK_FAILED",
                                                      record["resultSummary"], record["artifacts"])
                except Exception as exc:
                    record.update(status="FAILED", resultSummary="background Maven execution failed: " + type(exc).__name__,
                                  errorMessage=str(exc), artifacts={"errorType": type(exc).__name__,
                                                                      "errorMessage": str(exc)}, updateTime=now_iso())
                    self._upsert_background_task(task, record)
                    self._add_background_notification(task, record, "BACKGROUND_TASK_FAILED",
                                                      record["resultSummary"], record["artifacts"])

            asyncio.create_task(execute())
            return self._background_snapshot(record)
        if tool_name == "task.background_status":
            task_id = str(args.get("backgroundTaskId") or args.get("taskId") or "")
            record = self._background.get(task_id) or self._background_from_context(self.runtime.current_task(), task_id)
            return self._background_snapshot(record) if record else {"backgroundTaskId": task_id, "status": "NOT_FOUND"}
        if tool_name == "repo.exact_replace":
            budget.consume("repo.exact_replace")
            return self._exact_replace(repository, args)
        if tool_name == "artifact.generate_review_report":
            budget.consume(tool_name)
            output = Path(tempfile.mkdtemp(prefix="codeops-artifact-")) / "review.md"
            output.write_text(str(args.get("content", "")), encoding="utf-8")
            return {"path": str(output), "sandboxed": True}
        if tool_name in {"ops.query_prometheus", "ops.search_logs", "ops.query_trace"}:
            adapter = self.external_adapters.get(tool_name)
            if adapter is None:
                raise RuntimeError(f"No external adapter configured for {tool_name}")
            result = adapter(args)
            return await result if hasattr(result, "__await__") else result
        raise RuntimeError(f"Registered engineering tool has no handler: {tool_name}")

    @staticmethod
    def _background_snapshot(record: dict[str, Any]) -> dict[str, Any]:
        return dict(record)

    @staticmethod
    def _node_id(task: dict[str, Any] | None, requested: str) -> str:
        if not task or not requested or requested.startswith("step-"):
            return requested
        nodes = (task.get("context") or {}).get("taskDagNodes")
        if not isinstance(nodes, list):
            return requested
        for node in reversed(nodes):
            if isinstance(node, dict) and node.get("skillId") == requested and str(node.get("nodeId") or "").strip():
                return str(node["nodeId"])
        return requested

    def _new_background_task(self, task: dict[str, Any] | None, node_id: str, command_args: list[str]) -> dict[str, Any]:
        now = now_iso()
        return {"backgroundTaskId": "bgt-" + str(uuid.uuid4()), "taskId": (task or {}).get("taskId", ""),
                "nodeId": self._node_id(task, node_id), "toolName": "repo.maven", "status": "RUNNING",
                "requestSummary": "mvn " + " ".join(command_args), "resultSummary": "", "errorMessage": "",
                "command": [], "artifacts": {}, "createTime": now, "updateTime": now}

    @staticmethod
    def _background_from_context(task: dict[str, Any] | None, background_task_id: str) -> dict[str, Any] | None:
        values = ((task or {}).get("context") or {}).get("backgroundToolTasks")
        if not isinstance(values, list):
            return None
        return next((item for item in values if isinstance(item, dict)
                     and item.get("backgroundTaskId") == background_task_id), None)

    def _upsert_background_task(self, task: dict[str, Any] | None, record: dict[str, Any]) -> None:
        self._background[record["backgroundTaskId"]] = record
        if task is None:
            return
        context = task.setdefault("context", {})
        records = [item for item in context.get("backgroundToolTasks", []) if isinstance(item, dict)
                   and item.get("backgroundTaskId") != record["backgroundTaskId"]]
        records.append(record)
        context["backgroundToolTasks"] = records

    @staticmethod
    def _add_background_notification(task: dict[str, Any] | None, record: dict[str, Any], notification_type: str,
                                     summary: str, payload: dict[str, Any]) -> None:
        if task is None:
            return
        context = task.setdefault("context", {})
        notifications = list(context.get("taskNotifications") or [])
        notifications.append({"notificationId": "ntf-" + str(uuid.uuid4()), "taskId": record["taskId"],
                              "nodeId": record["nodeId"], "backgroundTaskId": record["backgroundTaskId"],
                              "type": notification_type, "status": record["status"], "summary": summary,
                              "payload": payload, "consumed": False, "consumedBy": "", "consumedTime": "",
                              "createTime": now_iso()})
        context["taskNotifications"] = notifications

    @staticmethod
    def consume_terminal_notifications(task: dict[str, Any] | None, consumer: str = "agent-loop") -> list[dict[str, Any]]:
        if task is None:
            return []
        context = task.setdefault("context", {})
        notifications = list(context.get("taskNotifications") or [])
        consumed, now = [], now_iso()
        for notification in notifications:
            if not isinstance(notification, dict) or notification.get("consumed") is True:
                continue
            if notification.get("type") not in {"BACKGROUND_TASK_FINISHED", "BACKGROUND_TASK_FAILED", "BACKGROUND_TASK_SKIPPED"}:
                continue
            notification.update(consumed=True, consumedBy=consumer or "agent-loop", consumedTime=now)
            consumed.append(dict(notification))
        if consumed:
            context["taskNotifications"] = notifications
            snapshots = list(context.get("consumedTaskNotifications") or [])
            snapshots.extend(consumed)
            context["consumedTaskNotifications"] = snapshots
        return consumed

    @staticmethod
    def has_running_background_tasks(task: dict[str, Any] | None) -> bool:
        return any(isinstance(item, dict) and str(item.get("status") or "").upper() == "RUNNING"
                   for item in ((task or {}).get("context") or {}).get("backgroundToolTasks", []))

    @staticmethod
    def _exact_replace(repository: str | Path, args: dict[str, Any]) -> dict[str, Any]:
        file_path = str(args.get("filePath") or "")
        old_text = str(args.get("oldText") or "").replace("\r\n", "\n").replace("\r", "\n")
        new_text = str(args.get("newText") or "").replace("\r\n", "\n").replace("\r", "\n")
        output: dict[str, Any] = {"repository": str(repository), "filePath": file_path,
                                  "oldTextLength": len(old_text), "newTextLength": len(new_text)}
        if not str(repository).strip() or not file_path.strip() or not old_text.strip():
            raise ValueError("INVALID_ARGUMENT: repository, filePath and oldText are required")
        root = Path(repository).resolve()
        file = (root / file_path).resolve()
        try:
            file.relative_to(root)
        except ValueError as exc:
            raise FileNotFoundError(f"FILE_NOT_FOUND: {file_path} not found or outside repository") from exc
        if not file.is_file():
            raise FileNotFoundError(f"FILE_NOT_FOUND: {file_path} not found or outside repository")
        current = file.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
        first = current.find(old_text)
        if first < 0:
            start = max(0, min(len(current), len(current) // 2) - 300)
            output.update(failureReason="OLD_TEXT_NOT_FOUND", contextStale=True,
                          currentSnippet=current[start:start + 600])
            raise RuntimeError("CONTEXT_STALE: oldText not found; re-read the current file before retry")
        if current.find(old_text, first + len(old_text)) >= 0:
            output.update(failureReason="MULTIPLE_MATCHES",
                          firstMatchContext=current[max(0, first - 300):first + len(old_text) + 300])
            raise RuntimeError("MULTIPLE_MATCHES: oldText matched multiple locations; include a larger unique block")
        updated = current[:first] + new_text + current[first + len(old_text):]
        file.write_text(updated, encoding="utf-8", newline="")
        output.update(failureReason="", matchOffset=first, updated=True,
                      updatedSnippet=updated[max(0, first - 300):first + len(new_text) + 300])
        return output

    @staticmethod
    def _summary(value: Any) -> str:
        try:
            return json.dumps(value, ensure_ascii=False, default=str)[:1200]
        except Exception:
            return str(value)[:1200]
