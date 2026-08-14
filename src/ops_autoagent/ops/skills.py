from __future__ import annotations

import re
from typing import Any

from ..config import Settings


class OpsAgentSkillService:
    SCORE_WEIGHTS = {"matchedAlertRules": 10, "symptoms": 6, "logPatterns": 7,
                     "tracePatterns": 7, "keyMetrics": 5}

    def __init__(self, settings: Settings):
        self.settings = settings

    def match(self, text: str, top_k: int = 3) -> list[dict[str, Any]]:
        if not self.settings.ops_agent_skill_enabled:
            return []
        normalized = self._normalize(text)
        scored = []
        for skill in self.load():
            score = sum(self._score_list(skill.get(field, []), normalized, weight)
                        for field, weight in self.SCORE_WEIGHTS.items())
            if score > 0:
                scored.append({**skill, "score": min(score, 100)})
        return sorted(scored, key=lambda item: (-item["score"], item["skillId"]))[:max(1, top_k)]

    def load(self) -> list[dict[str, Any]]:
        base = self.settings.ops_agent_skill_base_path
        if not base.is_dir():
            return []
        result = []
        for path in sorted(base.glob("*.md")):
            if path.name.lower() == "skill_template.md":
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            metadata = self._front_matter(content)
            skill_id = metadata.get("skillId", path.stem)
            result.append({"skillId": skill_id, "name": metadata.get("name", skill_id),
                           "category": metadata.get("category", "general"),
                           **{field: self._list(metadata.get(field, "")) for field in (
                               "matchedAlertRules", "symptoms", "recommendedTools", "keyMetrics", "logPatterns",
                               "tracePatterns", "rootCauseRules", "temporaryFixes", "longTermFixes")},
                           "runbookPath": metadata.get("runbookPath", ""), "content": content[:4000], "score": 0})
        return result

    @staticmethod
    def to_runbook_matches(skills: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [{"runbookId": f"skill:{skill['skillId']}", "title": f"[Skill] {skill['name']}",
                 "category": skill["category"], "score": skill["score"], "path": skill.get("runbookPath", ""),
                 "summary": (f"skillId={skill['skillId']}, category={skill['category']}, "
                             f"recommendedTools={skill['recommendedTools']}, temporaryFixes={skill['temporaryFixes']}, "
                             f"longTermFixes={skill['longTermFixes']}"), "content": str(skill)[:2400],
                 "source": "OPS_SKILL"} for skill in skills]

    @staticmethod
    def recommended_tools(skills: list[dict[str, Any]]) -> list[str]:
        return list(dict.fromkeys(tool for skill in skills for tool in skill.get("recommendedTools", [])))

    @staticmethod
    def _front_matter(content: str) -> dict[str, str]:
        if not content.startswith("---"):
            return {}
        result = {}
        for line in content.splitlines()[1:]:
            if line.strip() == "---":
                break
            if ":" in line:
                key, value = line.split(":", 1)
                result[key.strip()] = value.strip()
        return result

    @staticmethod
    def _list(value: str) -> list[str]:
        return [item.strip() for item in value.split(",") if item.strip()]

    @staticmethod
    def _normalize(value: str) -> str:
        return " ".join(re.findall(r"[a-z0-9_\u4e00-\u9fff]+", (value or "").lower()))

    @classmethod
    def _score_list(cls, values: list[str], normalized: str, weight: int) -> int:
        score = 0
        for value in values:
            term = cls._normalize(value)
            if term and term in normalized:
                score += weight if len(term) > 4 else max(2, weight // 2)
        return score
