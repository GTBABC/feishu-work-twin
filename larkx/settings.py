"""Runtime settings shared by validation, consumers and the settings UI."""

import math
import os
from dataclasses import asdict, dataclass, field

DEFAULT_RULES = (
    "你是我的工作数字人，以我的身份和同事沟通。回复自然、简短、准确。"
    "尽量自动处理沟通，明确请求提取为待办。不能假装已经执行实际工作；"
    "不能编造排期、承诺、文件内容或他人决定。需要我决策、授权或资料时生成异常待办。"
    "只记录工作事实、需求、决定和偏好，不记录普通寒暄、表情或辱骂为长期记忆。"
)
DEFAULT_SUFFIX = "reply by Bot"
GROUPS = [
    {"key": "general", "label": "常规", "description": "本人身份与全局回复习惯。"},
    {
        "key": "messages",
        "label": "消息处理",
        "description": "调整响应速度、连续消息合并和后台整理。",
    },
    {
        "key": "model",
        "label": "模型与上下文",
        "description": "调整模型和分析时可见的背景。",
    },
    {
        "key": "advanced",
        "label": "性能与高级",
        "description": "控制资源占用、刷新频率和展示数量。",
    },
    {
        "key": "connection",
        "label": "连接与存储",
        "description": "连接状态与下次启动配置。",
    },
]
MESSAGE_MAX_CHARS = 12000
UPLOAD_MAX_BYTES = 10 * 1024 * 1024
SEND_IMAGE_LIMIT = 6


@dataclass(frozen=True)
class SettingSpec:
    key: str
    label: str
    group: str
    kind: str
    default: object
    minimum: float | None = None
    maximum: float | None = None
    step: float = 1
    unit: str = ""
    description: str = ""
    env: str = ""
    choices: tuple = ()
    effect: str = "下一次处理生效"
    scale: float = 1
    choice_labels: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.env:
            object.__setattr__(self, "env", "LARKX_" + self.key.upper())

    def validate(self, value):
        valid = (
            isinstance(value, bool)
            if self.kind == "boolean"
            else isinstance(value, str)
            if self.kind in ("text", "textarea", "select")
            else isinstance(value, int) and not isinstance(value, bool)
            if self.kind == "integer"
            else isinstance(value, (int, float)) and not isinstance(value, bool)
        )
        if not valid:
            raise ValueError(f"{self.label}格式无效")
        if self.kind in ("integer", "number"):
            if not math.isfinite(value) or not self.minimum <= value <= self.maximum:
                raise ValueError(
                    f"{self.label}必须在 {self.minimum} 到 {self.maximum} 之间"
                )
            if self.scale > 1 and value % self.scale:
                raise ValueError(f"{self.label}必须使用整数{self.unit}")
        if (
            isinstance(value, str)
            and self.maximum is not None
            and len(value) > self.maximum
        ):
            raise ValueError(f"{self.label}最多 {int(self.maximum)} 字符")
        if self.choices and value not in self.choices:
            raise ValueError(f"{self.label}选项无效")
        if self.key in ("codex_model", "reply_suffix"):
            value = value.strip()
        return value

    def decode(self, raw):
        if self.key == "context_budget":
            raw = max(self.minimum, min(int(raw), self.maximum))
        if self.kind == "boolean":
            if str(raw).lower() not in (
                "true",
                "false",
                "1",
                "0",
                "yes",
                "no",
                "on",
                "off",
            ):
                raise ValueError(self.key)
            raw = str(raw).lower() in ("true", "1", "yes", "on")
        elif self.kind == "integer":
            if self.unit in ("秒", "分钟"):
                raw = float(raw)
                if raw < 0:
                    raise ValueError(self.key)
                # Round legacy fractions up so an existing wait is never shortened.
                raw = math.ceil(raw / self.scale) * self.scale
            raw = int(raw)
        elif self.kind == "number":
            raw = float(raw)
        return self.validate(raw)


SPECS = [
    SettingSpec(
        "default_private_reply",
        "私聊默认回复",
        "general",
        "select",
        "off",
        choices=("auto", "draft", "off"),
        choice_labels={"auto": "自动回复", "draft": "仅草稿", "off": "不回复"},
    ),
    SettingSpec(
        "default_group_reply",
        "群聊默认回复",
        "general",
        "select",
        "off",
        choices=("off", "at_me", "at_all", "all"),
        choice_labels={
            "off": "不回复",
            "at_me": "仅 @我",
            "at_all": "仅 @全体",
            "all": "自动回复",
        },
    ),
    SettingSpec(
        "own_rules", "回复规则", "general", "textarea", DEFAULT_RULES, maximum=12000
    ),
    SettingSpec(
        "reply_suffix",
        "回复尾缀",
        "general",
        "text",
        DEFAULT_SUFFIX,
        maximum=200,
    ),
    SettingSpec(
        "mark_read",
        "自动标记已读",
        "general",
        "boolean",
        True,
        env="LARKX_MARK_READ",
    ),
    SettingSpec(
        "batch_delay",
        "消息停顿时间",
        "messages",
        "integer",
        2,
        1,
        30,
        1,
        "秒",
        "停顿后合并处理连续消息。",
        "LARKX_BATCH_DELAY",
        effect="下一批消息生效",
    ),
    SettingSpec(
        "batch_max_wait",
        "最长等待时间",
        "messages",
        "integer",
        5,
        1,
        60,
        1,
        "秒",
        "持续收到消息时的等待上限。",
        "LARKX_BATCH_MAX_WAIT",
        effect="下一批消息生效",
    ),
    SettingSpec(
        "human_pause_seconds",
        "人工回复后暂停",
        "messages",
        "integer",
        120,
        0,
        86400,
        60,
        "分钟",
        "设为 0 不暂停。",
        scale=60,
        effect="保存后调整仍在暂停的会话",
    ),
    SettingSpec(
        "background_interval",
        "后台分析间隔",
        "messages",
        "integer",
        30,
        1,
        600,
        1,
        "秒",
        "定期汇总普通消息。",
        "LARKX_BACKGROUND_INTERVAL",
        effect="下一轮后台等待生效",
    ),
    SettingSpec(
        "batch_size",
        "每批消息上限",
        "messages",
        "integer",
        20,
        1,
        50,
        env="LARKX_BATCH_SIZE",
        effect="下一批消息生效",
    ),
    SettingSpec(
        "codex_model",
        "模型名称",
        "model",
        "text",
        "",
        maximum=100,
        env="LARKX_CODEX_MODEL",
        effect="下一次模型调用生效",
    ),
    SettingSpec(
        "codex_reasoning_effort",
        "推理强度",
        "model",
        "select",
        "low",
        env="LARKX_CODEX_REASONING_EFFORT",
        choices=("minimal", "low", "medium", "high", "xhigh"),
        effect="下一次模型调用生效",
    ),
    SettingSpec(
        "codex_timeout",
        "生成超时",
        "model",
        "integer",
        180,
        10,
        1800,
        unit="秒",
        env="LARKX_CODEX_TIMEOUT",
        effect="下一次模型调用生效",
    ),
    SettingSpec(
        "api_output_tokens",
        "最大输出长度",
        "model",
        "integer",
        8192,
        256,
        131072,
        256,
        "tokens",
        "需在所选模型支持范围内。",
    ),
    SettingSpec(
        "control_history_turns",
        "指令对话保留",
        "model",
        "integer",
        6,
        0,
        50,
        unit="轮",
        description="与数字人聊天时携带的历史轮数。",
    ),
    SettingSpec(
        "control_history_chars",
        "指令历史字数",
        "model",
        "integer",
        8000,
        1000,
        50000,
        1000,
        "字符",
    ),
    SettingSpec(
        "image_lookback_seconds",
        "图片回溯时间",
        "model",
        "integer",
        180,
        0,
        7200,
        60,
        "分钟",
        scale=60,
    ),
    SettingSpec(
        "image_reference_seconds",
        "提及图片时回溯",
        "model",
        "integer",
        1800,
        0,
        86400,
        60,
        "分钟",
        scale=60,
    ),
    SettingSpec(
        "context_budget",
        "上下文长度",
        "model",
        "integer",
        48000,
        24000,
        200000,
        1000,
        "字符",
        "超出后自动压缩。",
        "LARKX_CONTEXT_BUDGET",
        effect="下一次模型调用生效",
    ),
    SettingSpec(
        "recent_message_limit",
        "近期消息数",
        "model",
        "integer",
        24,
        1,
        100,
    ),
    SettingSpec("memory_limit", "记忆条数", "model", "integer", 15, 0, 100),
    SettingSpec(
        "task_context_limit",
        "参考的未完成任务数",
        "model",
        "integer",
        100,
        1,
        200,
    ),
    SettingSpec(
        "model_image_limit",
        "模型图片数",
        "model",
        "integer",
        3,
        0,
        6,
        description="设为 0 不发送图片。",
    ),
    SettingSpec(
        "max_concurrency",
        "模型总并发",
        "advanced",
        "integer",
        2,
        1,
        8,
        effect="新调用生效，当前调用继续完成",
    ),
    SettingSpec(
        "background_concurrency",
        "后台模型并发",
        "advanced",
        "integer",
        1,
        1,
        8,
        description="低于总并发可为即时回复留出名额。",
        effect="新调用生效，当前调用继续完成",
    ),
    SettingSpec(
        "poll_interval",
        "工作台刷新间隔",
        "advanced",
        "integer",
        5,
        1,
        60,
        1,
        "秒",
        effect="下一轮刷新生效",
    ),
    SettingSpec(
        "job_poll_interval",
        "任务进度刷新间隔",
        "advanced",
        "integer",
        2,
        1,
        10,
        1,
        "秒",
        effect="下一次进度查询生效",
    ),
    SettingSpec(
        "network_retries",
        "模型网络重试次数",
        "advanced",
        "integer",
        1,
        0,
        3,
    ),
    SettingSpec(
        "retry_delay", "模型重试等待", "advanced", "integer", 1, 1, 30, 1, "秒"
    ),
    SettingSpec("chat_cache_limit", "会话缓存数量", "advanced", "integer", 8, 1, 64),
    SettingSpec(
        "completed_task_limit",
        "已完成任务显示数量",
        "advanced",
        "integer",
        300,
        10,
        1000,
    ),
    SettingSpec(
        "actions_limit",
        "执行记录显示数量",
        "advanced",
        "integer",
        100,
        10,
        1000,
    ),
    SettingSpec(
        "send_image_limit",
        "每条消息图片上限",
        "messages",
        "integer",
        SEND_IMAGE_LIMIT,
        1,
        SEND_IMAGE_LIMIT,
        unit="张",
    ),
    SettingSpec(
        "upload_max_mb",
        "单张图片大小上限",
        "messages",
        "integer",
        UPLOAD_MAX_BYTES // 1024 // 1024,
        1,
        UPLOAD_MAX_BYTES // 1024 // 1024,
        unit="MB",
    ),
    SettingSpec(
        "message_max_chars",
        "发送文字上限",
        "messages",
        "integer",
        MESSAGE_MAX_CHARS,
        200,
        MESSAGE_MAX_CHARS,
        100,
        "字符",
        "包含回复尾缀和 @内容。",
    ),
]
SETTINGS = {spec.key: spec for spec in SPECS}


def resolve_settings(overrides=None):
    values, sources, defaults = {}, {}, {}
    for spec in SPECS:
        value, source = spec.default, "默认值"
        if spec.env and spec.env in os.environ:
            try:
                value, source = spec.decode(os.environ[spec.env]), "环境变量"
            except (ValueError, TypeError, OverflowError):
                pass
        defaults[spec.key] = value
        if overrides and spec.key in overrides:
            try:
                value, source = spec.decode(overrides[spec.key]), "已保存"
            except (ValueError, TypeError, OverflowError):
                pass
        values[spec.key], sources[spec.key] = value, source
    for resolved in (values, defaults):
        resolved["batch_max_wait"] = max(
            resolved["batch_delay"], resolved["batch_max_wait"]
        )
        resolved["background_concurrency"] = min(
            resolved["max_concurrency"], resolved["background_concurrency"]
        )
    return values, defaults, sources


def validate_changes(changes, current):
    if not isinstance(changes, dict) or changes.keys() - SETTINGS.keys():
        raise ValueError("设置包含未知字段或格式无效")
    values = {
        **current,
        **{key: SETTINGS[key].validate(value) for key, value in changes.items()},
    }
    if values["batch_max_wait"] < values["batch_delay"]:
        raise ValueError("最长等待时间不能小于停顿时间")
    if values["background_concurrency"] > values["max_concurrency"]:
        raise ValueError("后台并发不能超过模型总并发")
    return values


def settings_metadata():
    return [asdict(spec) for spec in SPECS]


def message_limits(settings):
    return {
        "send_images": settings["send_image_limit"],
        "upload_bytes": settings["upload_max_mb"] * 1024 * 1024,
        "message_chars": settings["message_max_chars"],
    }
