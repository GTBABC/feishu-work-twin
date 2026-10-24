"""Deterministic context budgets; full source records remain in the database."""

import copy
import html
import json
import re
from xml.sax.saxutils import escape
from ..settings import resolve_settings


def context_budget(settings=None):
    return (resolve_settings()[0] if settings is None else settings)["context_budget"]


def bounded_text(value, limit):
    value = str(value or "")
    if len(value) <= limit:
        return value
    marker = "\n[部分内容已省略]\n"
    if limit <= len(marker):
        return marker[:limit]
    available = limit - len(marker)
    first = (available * 2) // 3
    return value[:first] + marker + value[-(available - first) :]


def project(row, fields, limit=1000):
    return {
        key: bounded_text(row[key], limit) if isinstance(row[key], str) else row[key]
        for key in fields
        if key in row
    }


PROFILE_FIELDS = (
    "key",
    "external_id",
    "chat_id",
    "name",
    "kind",
    "mode",
    "effective_mode",
    "role",
    "description",
    "reply_style",
    "group_reply_policy",
    "effective_group_reply_policy",
)
TASK_FIELDS = (
    "id",
    "chat_id",
    "title",
    "status",
    "priority",
    "due_at",
    "details",
    "draft_reply",
)


def fit_narratives(value, budget):
    narrative = {
        "overview",
        "description",
        "reply_style",
        "content",
        "details",
        "draft_reply",
        "role",
    }

    def shrink(item, cap):
        if isinstance(item, dict):
            return {
                key: bounded_text(content, cap)
                if key in narrative and isinstance(content, str)
                else shrink(content, cap)
                for key, content in item.items()
            }
        if isinstance(item, list):
            return [shrink(content, cap) for content in item]
        return item

    cap = 2000
    while len(json.dumps(value, ensure_ascii=False)) > budget and cap >= 32:
        value = shrink(value, cap)
        cap //= 2
    return value


def compact_context(context, budget=16000, duplicate_ids=()):
    result = {
        "overview": bounded_text(context.get("overview"), 5000),
        "persona": project(context.get("persona", {}), PROFILE_FIELDS, 1800),
        "sender": project(context.get("sender", {}), PROFILE_FIELDS, 1800),
        "memories": [
            project(row, ("id", "content", "source_msg_id"), 600)
            for row in context.get("memories", [])
        ],
        "tasks": [project(row, TASK_FIELDS, 600) for row in context.get("tasks", [])],
        "recent_messages": copy.deepcopy(context.get("recent_messages", [])),
    }
    for key in ("self_user_id", "analysis_mode"):
        if key in context:
            result[key] = context[key]
    if "participants" in context:
        result["participants"] = {
            key: project(row, PROFILE_FIELDS, 500)
            for key, row in context["participants"].items()
        }
    duplicate_ids = set(duplicate_ids)
    for row in result["recent_messages"]:
        row["content"] = (
            "[见本次消息]"
            if row.get("msg_id") in duplicate_ids
            else bounded_text(row.get("content"), 1000)
        )
    result = fit_narratives(result, budget)
    # IDs, statuses and participant identity survive text clipping. Drop oldest
    # optional history only if metadata itself exceeds the remaining budget.
    for key, index in (("recent_messages", 0), ("memories", -1)):
        while len(json.dumps(result, ensure_ascii=False)) > budget and result[key]:
            result[key].pop(index)
    if len(json.dumps(result, ensure_ascii=False)) > budget:
        raise ValueError("角色与任务信息过多，请提高上下文预算或缩小本次分析范围")
    return result


def compact_message_body(body, budget):
    if len(body) <= budget:
        return body
    blocks = list(re.finditer(r"<content>([\s\S]*?)</content>", body))
    if not blocks:
        return bounded_text(body, budget)
    overhead = len(body) - sum(len(match.group(1)) for match in blocks)
    if overhead >= budget:
        raise ValueError("本批消息身份信息过多，请缩小批次或提高上下文预算")
    remaining = budget - overhead
    weights = [1] * len(blocks)
    weights[-1] = 3
    total = sum(weights)
    pieces, start = [], 0
    for match, weight in zip(blocks, weights):
        allowance = remaining * weight // total
        raw = html.unescape(match.group(1))
        cap = allowance
        clipped = escape(bounded_text(raw, cap))
        if len(clipped) > allowance:
            low, high, clipped = 0, min(len(raw), allowance), ""
            while low <= high:
                cap = (low + high) // 2
                candidate = escape(bounded_text(raw, cap))
                if len(candidate) <= allowance:
                    clipped = candidate
                    low = cap + 1
                else:
                    high = cap - 1
        pieces.extend((body[start : match.start(1)], clipped))
        start = match.end(1)
    pieces.append(body[start:])
    return "".join(pieces)
