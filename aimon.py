#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 模型雷达 (aimon)
==================

每天抓取 OpenRouter 模型目录与 LiteLLM 价格库，合并成一份可离线打开的
HTML 仪表盘，并记录模型新增、下架、价格变动与废弃日期。

数据目录：~/.aimonitor/
    dashboard.html     生成的仪表盘（可用浏览器直接打开）
    data/aimon.db      SQLite：每日快照 + 变动记录
    data/latest.json   最新一版原始数据
    reports/           Markdown 日报
    config.json        配置（厂商官网链接、更新频率等）

常用命令：
    aimon update       抓取并生成仪表盘
    aimon report       在终端查看最新摘要
    aimon links        列出所有厂商官网入口
    aimon history 关键词 [--days 90]
    aimon open         用浏览器打开仪表盘
    aimon install      安装/重装 launchd 每日定时任务
    aimon status       查看定时任务与数据状态
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

APP_DIR = Path.home() / ".aimonitor"
DATA_DIR = APP_DIR / "data"
REPORT_DIR = APP_DIR / "reports"
DB_PATH = DATA_DIR / "aimon.db"
CONFIG_PATH = APP_DIR / "config.json"
DASH_PATH = APP_DIR / "dashboard.html"
TEMPLATE_PATH = APP_DIR / "template.html"
LATEST_PATH = DATA_DIR / "latest.json"
CATALOG_PATH = DATA_DIR / "catalog.json"
COVERAGE_PATH = DATA_DIR / "coverage.json"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / "com.mac.aimonitor.plist"
DESKTOP_LINK = Path.home() / "Desktop" / "AI模型监控.html"
DESKTOP_REFRESH = Path.home() / "Desktop" / "更新AI模型雷达.command"
WRAPPER_PATH = APP_DIR / "aimon"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")

OPENROUTER_URL = "https://openrouter.ai/api/v1/models"
MODELSDEV_URL = "https://models.dev/api.json"
LITELLM_URLS = [
    "https://cdn.jsdelivr.net/gh/BerriAI/litellm@main/model_prices_and_context_window.json",
    "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json",
]
FX_URL = "https://open.er-api.com/v6/latest/USD"

TEXT_MODES = {"chat", "responses", "completion"}

# models.dev 的厂商 ID -> 本程序的统一厂商 ID
MD_PROVIDER_VENDOR = {
    "openai": "openai", "anthropic": "anthropic", "google": "google",
    "xai": "xai", "mistral": "mistral", "cohere": "cohere",
    "deepseek": "deepseek", "alibaba-cn": "qwen", "alibaba": "qwen",
    "zhipuai": "zhipu", "zai": "zhipu",
    "moonshotai-cn": "moonshot", "moonshotai": "moonshot",
    "minimax-cn": "minimax", "minimax": "minimax",
    "volcengine": "doubao", "tencent-tokenhub": "tencent",
    "stepfun": "stepfun", "stepfun-ai": "stepfun",
    "xiaomi": "xiaomi", "meta": "meta", "nvidia": "nvidia",
    "perplexity": "perplexity", "ai21": "ai21", "upstage": "upstage",
    "liquid": "liquid", "sensenova": "sensenova", "bailing": "bailing",
    "longcat": "longcat", "sarvam": "sarvam", "inception": "inception",
    "poolside": "poolside", "sakana": "sakana", "arcee": "arcee",
    "nova": "amazon", "watsonx": "ibm",
}

# 同一家厂商有中国站/国际站两份目录时，优先中国站（价格更贴近国内）
MD_PROVIDER_PRIORITY = {
    "alibaba-cn": 0, "alibaba": 1,
    "zhipuai": 0, "zai": 1,
    "moonshotai-cn": 0, "moonshotai": 1,
    "minimax-cn": 0, "minimax": 1,
    "stepfun": 0, "stepfun-ai": 1,
}

# 平台会托管别家模型；只保留平台自有型号（用子串匹配 model id + name）
MD_PREFIX_FILTER = {
    "alibaba-cn": ("qwen", "qwq", "qvq"), "alibaba": ("qwen", "qwq", "qvq"),
    "zhipuai": ("glm",), "zai": ("glm",),
    "moonshotai-cn": ("kimi", "moonshot"), "moonshotai": ("kimi", "moonshot"),
    "minimax-cn": ("minimax",), "minimax": ("minimax",),
    "volcengine": ("doubao", "seed"),
    "stepfun": ("step",), "stepfun-ai": ("step",),
    "xiaomi": ("mimo",), "meta": ("muse",), "nvidia": ("nemotron",),
    "perplexity": ("sonar",), "ai21": ("jamba",), "upstage": ("solar",),
    "liquid": ("liquid",), "sensenova": ("sensenova",),
    "bailing": ("ling", "ring"), "longcat": ("longcat",),
    "sarvam": ("sarvam",), "inception": ("mercury",),
    "poolside": ("laguna",), "sakana": ("fugu", "namazu", "sakana"),
    "arcee": ("trinity",), "nova": ("nova",), "watsonx": ("granite",),
}

# models.dev canonical_model_id 的前缀 -> 厂商
CANON_PREFIX_VENDOR = {
    "openai": "openai", "anthropic": "anthropic", "google": "google",
    "xai": "xai", "x-ai": "xai", "mistralai": "mistral", "mistral": "mistral",
    "cohere": "cohere", "deepseek": "deepseek", "deepseek-ai": "deepseek",
    "qwen": "qwen",
    "zhipuai": "zhipu", "z-ai": "zhipu", "zai-org": "zhipu",
    "moonshotai": "moonshot", "moonshotai-cn": "moonshot",
    "minimax": "minimax", "minimax-cn": "minimax", "minimaxai": "minimax",
    "bytedance-seed": "doubao", "bytedance": "doubao",
    "tencent": "tencent",
    "stepfun-ai": "stepfun", "stepfun": "stepfun",
    "xiaomi": "xiaomi", "meta": "meta", "nvidia": "nvidia",
    "perplexity": "perplexity", "ai21": "ai21", "upstage": "upstage",
    "liquid": "liquid", "bailing": "bailing",
    "longcat": "longcat", "sarvam": "sarvam", "inception": "inception",
    "poolside": "poolside", "sakana": "sakana", "arcee": "arcee",
    "amazon": "amazon", "ibm": "ibm",
}

# 这些平台会同时托管别家模型；官方价表格只保留平台自有模型，
# 别家模型在对应厂商（DeepSeek / 智谱 / Kimi 等）自己的卡片里已有。
FIRST_PARTY_PREFIXES = {
    "dashscope": ("qwen", "qwq"),
    "qwencloud": ("qwen", "qwq"),
    "volcengine": ("doubao",),
}

# ---------------------------------------------------------------------------
# 厂商配置
# ---------------------------------------------------------------------------

DEFAULT_VENDORS = [
    {
        "id": "anthropic", "name": "Anthropic", "sub": "Claude",
        "color": "#D97757", "desc": "Claude Opus / Sonnet / Haiku 系列",
        "homepage": "https://www.anthropic.com/",
        "pricing": "https://platform.claude.com/docs/en/about-claude/pricing",
        "models": "https://platform.claude.com/docs/en/about-claude/models/overview",
        "docs": "https://platform.claude.com/docs/",
    },
    {
        "id": "openai", "name": "OpenAI", "sub": "GPT / o 系列",
        "color": "#10A37F", "desc": "GPT、o 系列与推理模型",
        "homepage": "https://openai.com/",
        "pricing": "https://openai.com/api/pricing/",
        "models": "https://platform.openai.com/docs/models",
        "docs": "https://platform.openai.com/docs/",
    },
    {
        "id": "google", "name": "Google", "sub": "Gemini",
        "color": "#4285F4", "desc": "Gemini 系列多模态模型",
        "homepage": "https://ai.google.dev/",
        "pricing": "https://ai.google.dev/gemini-api/docs/pricing",
        "models": "https://ai.google.dev/gemini-api/docs/models",
        "docs": "https://ai.google.dev/gemini-api/docs",
    },
    {
        "id": "xai", "name": "xAI", "sub": "Grok",
        "color": "#4B5563", "desc": "Grok 系列模型",
        "homepage": "https://x.ai/",
        "pricing": "https://docs.x.ai/developers/pricing",
        "models": "https://docs.x.ai/developers/models",
        "docs": "https://docs.x.ai/",
    },
    {
        "id": "deepseek", "name": "DeepSeek", "sub": "V 系列 / R 系列",
        "color": "#4D6BFE", "desc": "DeepSeek 通用与推理模型",
        "homepage": "https://www.deepseek.com/",
        "pricing": "https://api-docs.deepseek.com/quick_start/pricing",
        "models": "https://api-docs.deepseek.com/quick_start/pricing",
        "docs": "https://api-docs.deepseek.com/",
    },
    {
        "id": "mistral", "name": "Mistral AI", "sub": "Mistral / Codestral",
        "color": "#FF7000", "desc": "Mistral、Codestral、Devstral 等",
        "homepage": "https://mistral.ai/",
        "pricing": "https://mistral.ai/pricing",
        "models": "https://docs.mistral.ai/getting-started/models/",
        "docs": "https://docs.mistral.ai/",
    },
    {
        "id": "cohere", "name": "Cohere", "sub": "Command",
        "color": "#39594D", "desc": "Command 系列企业模型",
        "homepage": "https://cohere.com/",
        "pricing": "https://cohere.com/pricing",
        "models": "https://docs.cohere.com/docs/models",
        "docs": "https://docs.cohere.com/",
    },
    {
        "id": "qwen", "name": "阿里通义千问", "sub": "Qwen",
        "color": "#615CED", "desc": "通义千问 Qwen 全系列",
        "homepage": "https://tongyi.aliyun.com/",
        "pricing": "https://help.aliyun.com/zh/model-studio/billing-for-model-studio",
        "models": "https://help.aliyun.com/zh/model-studio/models",
        "docs": "https://help.aliyun.com/zh/model-studio/",
    },
    {
        "id": "zhipu", "name": "智谱 GLM", "sub": "GLM",
        "color": "#1E5EFF", "desc": "智谱 GLM 系列（含 Z.ai 国际站）",
        "homepage": "https://www.bigmodel.cn/",
        "pricing": "https://www.bigmodel.cn/pricing",
        "models": "https://docs.bigmodel.cn/cn/guide/models",
        "docs": "https://docs.bigmodel.cn/",
    },
    {
        "id": "moonshot", "name": "月之暗面 Kimi", "sub": "Kimi",
        "color": "#6B7280", "desc": "Kimi K 系列模型",
        "homepage": "https://www.moonshot.cn/",
        "pricing": "https://platform.moonshot.cn/docs/pricing/chat",
        "models": "https://platform.moonshot.cn/docs/intro",
        "docs": "https://platform.moonshot.cn/docs/",
    },
    {
        "id": "minimax", "name": "MiniMax", "sub": "M 系列",
        "color": "#E73562", "desc": "MiniMax M 系列模型",
        "homepage": "https://www.minimaxi.com/",
        "pricing": "https://platform.minimaxi.com/document/Price",
        "models": "https://platform.minimaxi.com/document/Models",
        "docs": "https://platform.minimaxi.com/document/",
    },
    {
        "id": "doubao", "name": "字节豆包", "sub": "Doubao / Seed",
        "color": "#3370FF", "desc": "火山引擎豆包大模型",
        "homepage": "https://www.volcengine.com/product/ark",
        "pricing": "https://www.volcengine.com/pricing?product=ark",
        "models": "https://www.volcengine.com/docs/82379/1099320",
        "docs": "https://www.volcengine.com/docs/82379",
    },
    {
        "id": "tencent", "name": "腾讯云 TokenHub", "sub": "托管模型",
        "color": "#00A4FF", "desc": "腾讯云 TokenHub 托管的模型价格",
        "homepage": "https://cloud.tencent.com/product/tclm",
        "pricing": "https://cloud.tencent.com/document/product/1823/130055",
        "models": "https://cloud.tencent.com/product/tclm",
        "docs": "https://cloud.tencent.com/document/product/1729",
    },
    {
        "id": "meta", "name": "Meta", "sub": "Muse / Llama",
        "color": "#0668E1", "desc": "Meta Muse 系列（Llama 另见 OpenRouter）",
        "homepage": "https://www.meta.com/",
        "pricing": "https://dev.meta.ai/docs",
        "models": "https://dev.meta.ai/docs",
        "docs": "https://dev.meta.ai/docs",
    },
    {
        "id": "nvidia", "name": "NVIDIA", "sub": "Nemotron",
        "color": "#76B900", "desc": "NVIDIA Nemotron 系列",
        "homepage": "https://www.nvidia.com/en-us/ai/",
        "pricing": "https://build.nvidia.com/models",
        "models": "https://docs.api.nvidia.com/nim/",
        "docs": "https://docs.api.nvidia.com/nim/",
    },
    {
        "id": "stepfun", "name": "阶跃星辰", "sub": "Step",
        "color": "#7C3AED", "desc": "Step 系列多模态模型",
        "homepage": "https://www.stepfun.com/",
        "pricing": "https://platform.stepfun.com/docs/zh/overview/concept",
        "models": "https://platform.stepfun.com/docs/zh/overview/concept",
        "docs": "https://platform.stepfun.com/docs/zh/overview/concept",
    },
    {
        "id": "xiaomi", "name": "小米 MiMo", "sub": "MiMo",
        "color": "#FF6900", "desc": "小米 MiMo 系列模型",
        "homepage": "https://www.xiaomi.com/",
        "pricing": "https://platform.xiaomimimo.com/#/docs",
        "models": "https://platform.xiaomimimo.com/#/docs",
        "docs": "https://platform.xiaomimimo.com/#/docs",
    },
    {
        "id": "amazon", "name": "Amazon Nova", "sub": "Nova",
        "color": "#FF9900", "desc": "Amazon Nova 系列模型",
        "homepage": "https://aws.amazon.com/ai/generative-ai/nova/",
        "pricing": "https://aws.amazon.com/bedrock/pricing/",
        "models": "https://docs.aws.amazon.com/nova/latest/userguide/models.html",
        "docs": "https://docs.aws.amazon.com/nova/latest/userguide/",
    },
    {
        "id": "perplexity", "name": "Perplexity", "sub": "Sonar",
        "color": "#20808D", "desc": "Perplexity Sonar 联网搜索模型",
        "homepage": "https://www.perplexity.ai/",
        "pricing": "https://docs.perplexity.ai/getting-started/pricing",
        "models": "https://docs.perplexity.ai/getting-started/models",
        "docs": "https://docs.perplexity.ai/",
    },
    {
        "id": "ai21", "name": "AI21 Labs", "sub": "Jamba",
        "color": "#E11D48", "desc": "Jamba 系列模型",
        "homepage": "https://www.ai21.com/",
        "pricing": "https://www.ai21.com/pricing",
        "models": "https://docs.ai21.com/docs/jamba-foundation-models",
        "docs": "https://docs.ai21.com/",
    },
    {
        "id": "upstage", "name": "Upstage", "sub": "Solar",
        "color": "#0EA5E9", "desc": "Solar 系列模型",
        "homepage": "https://www.upstage.ai/",
        "pricing": "https://developers.upstage.ai/docs/getting-started/pricing",
        "models": "https://developers.upstage.ai/docs/apis/chat",
        "docs": "https://developers.upstage.ai/docs",
    },
    {
        "id": "liquid", "name": "Liquid AI", "sub": "LFM",
        "color": "#06B6D4", "desc": "Liquid 推理模型",
        "homepage": "https://www.liquid.ai/",
        "pricing": "https://www.liquid.ai/pricing",
        "models": "https://docs.liquid.ai/",
        "docs": "https://docs.liquid.ai/",
    },
    {
        "id": "sensenova", "name": "商汤日日新", "sub": "SenseNova",
        "color": "#E11D48", "desc": "商汤 SenseNova 系列",
        "homepage": "https://www.sensenova.cn/",
        "pricing": "https://platform.sensenova.cn/docs",
        "models": "https://platform.sensenova.cn/docs",
        "docs": "https://platform.sensenova.cn/docs",
    },
    {
        "id": "bailing", "name": "蚂蚁百灵", "sub": "Ling / Ring",
        "color": "#1677FF", "desc": "蚂蚁百灵 Ling / Ring 系列",
        "homepage": "https://alipaytbox.yuque.com/sxs0ba/ling/intro",
        "pricing": "https://alipaytbox.yuque.com/sxs0ba/ling/intro",
        "models": "https://alipaytbox.yuque.com/sxs0ba/ling/intro",
        "docs": "https://alipaytbox.yuque.com/sxs0ba/ling/intro",
    },
    {
        "id": "longcat", "name": "美团 LongCat", "sub": "LongCat",
        "color": "#FFB000", "desc": "美团 LongCat 系列",
        "homepage": "https://longcat.chat/",
        "pricing": "https://longcat.chat/platform/docs/",
        "models": "https://longcat.chat/platform/docs/",
        "docs": "https://longcat.chat/platform/docs/",
    },
    {
        "id": "sarvam", "name": "Sarvam AI", "sub": "Sarvam",
        "color": "#F97316", "desc": "Sarvam 系列模型",
        "homepage": "https://www.sarvam.ai/",
        "pricing": "https://docs.sarvam.ai/",
        "models": "https://docs.sarvam.ai/",
        "docs": "https://docs.sarvam.ai/",
    },
    {
        "id": "inception", "name": "Inception", "sub": "Mercury",
        "color": "#8B5CF6", "desc": "Mercury 扩散语言模型",
        "homepage": "https://www.inceptionlabs.ai/",
        "pricing": "https://docs.inceptionlabs.ai/",
        "models": "https://docs.inceptionlabs.ai/",
        "docs": "https://docs.inceptionlabs.ai/",
    },
    {
        "id": "poolside", "name": "Poolside", "sub": "Laguna",
        "color": "#14B8A6", "desc": "Laguna 代码模型",
        "homepage": "https://poolside.ai/",
        "pricing": "https://platform.poolside.ai",
        "models": "https://platform.poolside.ai",
        "docs": "https://platform.poolside.ai",
    },
    {
        "id": "sakana", "name": "Sakana AI", "sub": "Fugu / Namazu",
        "color": "#EF4444", "desc": "Sakana Fugu / Namazu 系列",
        "homepage": "https://sakana.ai/",
        "pricing": "https://console.sakana.ai/models",
        "models": "https://console.sakana.ai/models",
        "docs": "https://console.sakana.ai/models",
    },
    {
        "id": "arcee", "name": "Arcee AI", "sub": "Trinity",
        "color": "#F59E0B", "desc": "Arcee Trinity 系列",
        "homepage": "https://www.arcee.ai/",
        "pricing": "https://docs.arcee.ai",
        "models": "https://docs.arcee.ai",
        "docs": "https://docs.arcee.ai",
    },
    {
        "id": "ibm", "name": "IBM Granite", "sub": "Granite",
        "color": "#0F62FE", "desc": "IBM Granite 系列",
        "homepage": "https://www.ibm.com/products/watsonx-ai/foundation-models",
        "pricing": "https://www.ibm.com/products/watsonx-ai/foundation-models",
        "models": "https://www.ibm.com/docs/en/watsonx/saas?topic=solutions-supported-foundation-models",
        "docs": "https://www.ibm.com/docs/en/watsonx/saas",
    },
    {
        "id": "baidu", "name": "百度文心", "sub": "ERNIE",
        "color": "#2932E1", "desc": "文心一言 ERNIE 系列（官方目录，手动查看）",
        "homepage": "https://cloud.baidu.com/product/wenxinworkshop",
        "pricing": "https://cloud.baidu.com/doc/WENXINWORKSHOP/s/hlrk4akp7",
        "models": "https://cloud.baidu.com/doc/WENXINWORKSHOP/s/hlrk4akp7",
        "docs": "https://cloud.baidu.com/doc/WENXINWORKSHOP/",
        "link_only": True,
    },
    {
        "id": "iflytek", "name": "讯飞星火", "sub": "Spark",
        "color": "#2563EB", "desc": "讯飞星火系列（官方目录，手动查看）",
        "homepage": "https://xinghuo.xfyun.cn/",
        "pricing": "https://xinghuo.xfyun.cn/sparkapi",
        "models": "https://xinghuo.xfyun.cn/sparkapi",
        "docs": "https://www.xfyun.cn/doc/spark/",
        "link_only": True,
    },
    {
        "id": "lingyiwanwu", "name": "零一万物 Yi", "sub": "Yi",
        "color": "#10B981", "desc": "Yi 系列（官方文档，手动查看）",
        "homepage": "https://www.lingyiwanwu.com/",
        "pricing": "https://platform.lingyiwanwu.com/docs",
        "models": "https://platform.lingyiwanwu.com/docs",
        "docs": "https://platform.lingyiwanwu.com/docs",
        "link_only": True,
    },
    {
        "id": "huawei", "name": "华为盘古", "sub": "Pangu",
        "color": "#CF0A2C", "desc": "盘古大模型（官方文档，手动查看）",
        "homepage": "https://www.huaweicloud.com/product/pangu.html",
        "pricing": "https://support.huaweicloud.com/pangu-lm/",
        "models": "https://support.huaweicloud.com/pangu-lm/",
        "docs": "https://support.huaweicloud.com/pangu-lm/",
        "link_only": True,
    },
    {
        "id": "baichuan", "name": "百川智能", "sub": "Baichuan",
        "color": "#F59E0B", "desc": "百川系列模型（官方文档，手动查看）",
        "homepage": "https://www.baichuan-ai.com/",
        "pricing": "https://platform.baichuan-ai.com/docs",
        "models": "https://platform.baichuan-ai.com/docs",
        "docs": "https://platform.baichuan-ai.com/docs",
        "link_only": True,
    },
    {
        "id": "microsoft", "name": "Microsoft Phi", "sub": "Phi",
        "color": "#00A4EF", "desc": "Microsoft Phi 系列（Azure AI，手动查看）",
        "homepage": "https://azure.microsoft.com/en-us/products/phi",
        "pricing": "https://azure.microsoft.com/en-us/pricing/details/phi-3/",
        "models": "https://learn.microsoft.com/en-us/azure/ai-foundry/model-inference/concepts/models",
        "docs": "https://learn.microsoft.com/en-us/azure/ai-foundry/",
        "link_only": True,
    },
]

AGGREGATOR_LINKS = [
    {
        "id": "openrouter", "name": "OpenRouter", "sub": "聚合渠道",
        "color": "#6366F1", "desc": "统一 API 与实时渠道价，覆盖数百个模型",
        "homepage": "https://openrouter.ai/",
        "pricing": "https://openrouter.ai/models",
        "models": "https://openrouter.ai/models",
        "docs": "https://openrouter.ai/docs",
    },
    {
        "id": "litellm", "name": "LiteLLM 价格库", "sub": "数据源",
        "color": "#0EA5E9", "desc": "开源模型价格与上下文窗口数据库",
        "homepage": "https://github.com/BerriAI/litellm",
        "pricing": "https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json",
        "models": "https://github.com/BerriAI/litellm/blob/main/model_prices_and_context_window.json",
        "docs": "https://docs.litellm.ai/",
    },
    {
        "id": "modelsdev", "name": "models.dev", "sub": "全量目录",
        "color": "#14B8A6", "desc": "235 家渠道、8900+ 条模型目录，按 canonical ID 归并",
        "homepage": "https://models.dev/",
        "pricing": "https://models.dev/",
        "models": "https://models.dev/",
        "docs": "https://models.dev/",
    },
]

DEFAULT_CONFIG = {
    "light_interval_minutes": 15,
    "full_interval_hours": 3,
    "notify": True,
    "history_days": 90,
    "snapshot_keep_days": 365,
    "fx_url": FX_URL,
    "official_providers": [
        "anthropic", "openai", "gemini", "xai", "deepseek", "mistral",
        "cohere", "cohere_chat", "dashscope", "qwencloud", "bigmodel",
        "zai", "moonshot", "minimax", "volcengine", "tencent",
    ],
    "openrouter": {
        "enabled": True,
        "url": OPENROUTER_URL,
        "exclude_variants": ["batch", "online", "nitro", "floor", "extended"],
        "exclude_vendors": ["openrouter"],
        "recent_days": 14,
    },
    "litellm": {
        "enabled": True,
        "urls": LITELLM_URLS,
    },
    "modelsdev": {
        "enabled": True,
        "url": MODELSDEV_URL,
    },
    "publish": {
        "enabled": True,
        "worktree": str(Path.home() / "Code" / "ai-model-radar-pages"),
        "branch": "gh-pages",
    },
    "vendors": DEFAULT_VENDORS,
}

PROVIDER_VENDOR = {
    "anthropic": "anthropic",
    "openai": "openai",
    "text-completion-openai": "openai",
    "gemini": "google",
    "xai": "xai",
    "deepseek": "deepseek",
    "mistral": "mistral",
    "cohere": "cohere",
    "cohere_chat": "cohere",
    "dashscope": "qwen",
    "qwencloud": "qwen",
    "bigmodel": "zhipu",
    "zai": "zhipu",
    "moonshot": "moonshot",
    "minimax": "minimax",
    "volcengine": "doubao",
    "tencent": "tencent",
}

PROVIDER_PRIORITY = {
    "anthropic": 0, "openai": 0, "gemini": 0, "xai": 0, "deepseek": 0,
    "mistral": 0, "cohere_chat": 0, "cohere": 1, "dashscope": 0,
    "qwencloud": 1, "bigmodel": 0, "zai": 1, "moonshot": 0,
    "minimax": 0, "volcengine": 0, "tencent": 0,
}

MD_PROVIDER_LABEL = {
    "alibaba-cn": "阿里云百炼（中国）", "alibaba": "阿里云百炼（国际）",
    "zhipuai": "智谱开放平台", "zai": "Z.ai",
    "moonshotai-cn": "月之暗面（中国）", "moonshotai": "月之暗面（国际）",
    "minimax-cn": "MiniMax（中国）", "minimax": "MiniMax（国际）",
    "volcengine": "火山引擎", "tencent-tokenhub": "腾讯云 TokenHub",
    "stepfun": "阶跃星辰", "stepfun-ai": "阶跃星辰（国际）",
    "xiaomi": "小米 MiMo", "meta": "Meta", "nvidia": "NVIDIA",
    "perplexity": "Perplexity", "ai21": "AI21 Labs", "upstage": "Upstage",
    "liquid": "Liquid AI", "sensenova": "商汤日日新", "bailing": "蚂蚁百灵",
    "longcat": "美团 LongCat", "sarvam": "Sarvam AI", "inception": "Inception",
    "poolside": "Poolside", "sakana": "Sakana AI", "arcee": "Arcee AI",
    "nova": "Amazon Nova", "watsonx": "IBM watsonx",
}

PROVIDER_LABEL = {
    "anthropic": "Anthropic", "openai": "OpenAI", "gemini": "Google",
    "xai": "xAI", "deepseek": "DeepSeek", "mistral": "Mistral",
    "cohere": "Cohere", "cohere_chat": "Cohere", "dashscope": "阿里云百炼",
    "qwencloud": "Qwen Cloud", "bigmodel": "智谱开放平台", "zai": "Z.ai",
    "moonshot": "Moonshot", "minimax": "MiniMax", "volcengine": "火山引擎",
    "tencent": "腾讯云",
}

OR_VENDOR_NAMES = {
    "openai": "OpenAI", "anthropic": "Anthropic", "google": "Google",
    "qwen": "阿里通义千问", "z-ai": "智谱 GLM", "deepseek": "DeepSeek",
    "moonshotai": "月之暗面 Kimi", "minimax": "MiniMax", "x-ai": "xAI",
    "mistralai": "Mistral", "meta-llama": "Meta", "meta": "Meta",
    "cohere": "Cohere", "bytedance-seed": "字节 Seed", "bytedance": "字节跳动",
    "tencent": "腾讯", "xiaomi": "小米", "stepfun": "阶跃星辰",
    "nvidia": "NVIDIA", "amazon": "Amazon", "perplexity": "Perplexity",
    "microsoft": "Microsoft", "nousresearch": "Nous Research",
    "ai21": "AI21 Labs", "baidu": "百度", "inclusionai": "InclusionAI",
    "liquid": "Liquid AI", "rekaai": "Reka", "sakana": "Sakana AI",
    "thinkingmachines": "Thinking Machines", "writer": "Writer",
    "ibm-granite": "IBM", "upstage": "Upstage", "meituan": "美团",
    "particle-ai": "Particle AI", "arcee-ai": "Arcee AI", "tngtech": "TNG",
    "inflection": "Inflection", "jamba": "AI21", "aion-labs": "Aion Labs",
    "cognitivecomputations": "Cognitive Computations", "neversleep": "NeverSleep",
    "sao10k": "Sao10K", "undi95": "Undi95", "gryphe": "Gryphe",
    "mancer": "Mancer", "alpindale": "Alpindale", "open-r1": "Open R1",
    "rednote-hilab": "RedNote HiLab", "moondream": "Moondream",
    "upstage": "Upstage", "nex-agi": "Nex AGI", "scaledown": "ScaleDown",
    "perceptron": "Perceptron", "inference-net": "Inference.net",
}

VENDOR_COLORS = {v["id"]: v["color"] for v in DEFAULT_VENDORS}
VENDOR_COLORS.update({
    "openrouter": "#6366F1", "openai": "#10A37F", "anthropic": "#D97757",
    "google": "#4285F4", "qwen": "#615CED", "z-ai": "#1E5EFF",
    "deepseek": "#4D6BFE", "moonshotai": "#6B7280", "minimax": "#E73562",
    "x-ai": "#4B5563", "mistralai": "#FF7000", "meta-llama": "#0668E1",
    "meta": "#0668E1", "cohere": "#39594D", "bytedance-seed": "#3370FF",
    "tencent": "#00A4FF", "xiaomi": "#FF6900", "stepfun": "#7C3AED",
    "nvidia": "#76B900", "amazon": "#FF9900", "perplexity": "#20808D",
    "nousresearch": "#8B5CF6", "microsoft": "#00A4EF", "baidu": "#2932E1",
})

PRICE_FIELDS = [
    ("input_usd", "输入"),
    ("output_usd", "输出"),
    ("cache_read_usd", "缓存读"),
    ("cache_write_usd", "缓存写"),
]
META_FIELDS = [
    ("context", "上下文"),
    ("max_output", "最大输出"),
    ("deprecation", "废弃日期"),
]


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def now_local() -> dt.datetime:
    return dt.datetime.now().astimezone()


def today_str() -> str:
    return now_local().strftime("%Y-%m-%d")


def log(msg: str) -> None:
    print(f"[{now_local().strftime('%H:%M:%S')}] {msg}", flush=True)


def err(msg: str) -> None:
    print(f"[{now_local().strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def run_curl(url: str, timeout: int = 90, retries: int = 3) -> str:
    """用系统 curl 抓取，失败自动退避重试。"""
    cmd = [
        "/usr/bin/curl", "-sSL", "--compressed",
        "--max-time", str(timeout),
        "-A", UA,
        "-H", "Accept: application/json,text/plain,*/*",
        url,
    ]
    last = ""
    for attempt in range(1, retries + 1):
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True,
                timeout=timeout + 15,
            )
            body = proc.stdout or ""
            if proc.returncode == 0 and body.strip():
                return body
            last = (proc.stderr or "").strip() or f"curl exit {proc.returncode}"
        except subprocess.TimeoutExpired:
            last = f"超时 {timeout + 15}s"
        if attempt < retries:
            err(f"  ⏳ 第 {attempt} 次抓取失败（{last[:120]}），{3 * attempt} 秒后重试…")
            time.sleep(3 * attempt)
    raise RuntimeError(f"抓取失败：{url}（{last[:200]}）")


def fetch_json(url: str, timeout: int = 90, retries: int = 3):
    return json.loads(run_curl(url, timeout=timeout, retries=retries))


def per_million(value):
    """美元/token -> 美元/百万 token，保留 6 位小数。"""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v < 0:
        return None
    return round(v * 1_000_000, 6)


def per_million_already(value):
    """数据源本身已经是美元/百万 token（models.dev）。"""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v < 0:
        return None
    return round(v, 6)


def norm_model(name: str) -> str:
    """把不同源的模型名压成可比较的键（去掉厂商前缀、标点、大小写）。"""
    s = str(name or "").lower()
    if "/" in s:
        s = s.rsplit("/", 1)[-1]
    return re.sub(r"[^a-z0-9]+", "", s)


def strip_provider_prefix(key: str, provider: str) -> str:
    prefix = provider.lower() + "/"
    if key.lower().startswith(prefix):
        return key[len(prefix):]
    return key


def pretty_vendor(slug: str) -> str:
    if not slug:
        return "其他"
    if slug in OR_VENDOR_NAMES:
        return OR_VENDOR_NAMES[slug]
    return slug.replace("-", " ").replace("_", " ").title()


def is_http_url(value) -> bool:
    if not isinstance(value, str):
        return False
    try:
        p = urlparse(value)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


def human_int(n) -> str:
    if n is None:
        return "—"
    n = int(n)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".rstrip("0").rstrip(".")
    if n >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


# ---------------------------------------------------------------------------
# 配置 / 数据库
# ---------------------------------------------------------------------------

def deep_merge(base, override):
    if isinstance(base, dict) and isinstance(override, dict):
        out = dict(base)
        for k, v in override.items():
            out[k] = deep_merge(base[k], v) if k in base else v
        return out
    return override


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if CONFIG_PATH.exists():
        try:
            user_cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            cfg = deep_merge(cfg, user_cfg)
        except Exception as exc:
            err(f"⚠ 配置文件读取失败，使用默认配置：{exc}")
    # 用户可以从 config.json 里删掉不想看的厂商；只有完全没有 vendors 时才用默认列表
    if "vendors" not in cfg or not isinstance(cfg.get("vendors"), list):
        cfg["vendors"] = [dict(v) for v in DEFAULT_VENDORS]
    if not CONFIG_PATH.exists():
        save_config(cfg)
    return cfg


def save_config(cfg: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def atomic_write_text(path: Path, text: str) -> None:
    """先写临时文件再 rename，避免页面刷新时读到半截 HTML。"""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def vendor_map(cfg: dict) -> dict:
    return {v["id"]: v for v in cfg.get("vendors", []) if isinstance(v, dict)}


def connect_db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH), timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS snapshots (
            day            TEXT NOT NULL,
            key            TEXT NOT NULL,
            source         TEXT NOT NULL,
            vendor         TEXT NOT NULL,
            vendor_name    TEXT NOT NULL,
            provider       TEXT,
            model_id       TEXT NOT NULL,
            name           TEXT NOT NULL,
            input_usd      REAL,
            output_usd     REAL,
            cache_read_usd REAL,
            cache_write_usd REAL,
            context        INTEGER,
            max_output     INTEGER,
            released       INTEGER,
            deprecation    TEXT,
            mode           TEXT,
            link           TEXT,
            note           TEXT,
            extra          TEXT,
            PRIMARY KEY (day, key)
        );
        CREATE INDEX IF NOT EXISTS idx_snapshots_source_day
            ON snapshots(source, day);
        CREATE INDEX IF NOT EXISTS idx_snapshots_model
            ON snapshots(name);

        CREATE TABLE IF NOT EXISTS changes (
            day        TEXT NOT NULL,
            key        TEXT NOT NULL,
            kind       TEXT NOT NULL,
            source     TEXT,
            vendor     TEXT,
            vendor_name TEXT,
            model_name TEXT,
            provider   TEXT,
            field      TEXT NOT NULL,
            old_value  TEXT,
            new_value  TEXT,
            PRIMARY KEY (day, key, kind, field)
        );
        CREATE INDEX IF NOT EXISTS idx_changes_day ON changes(day);

        CREATE TABLE IF NOT EXISTS runs (
            day             TEXT PRIMARY KEY,
            finished_at     TEXT,
            fx              REAL,
            official_count  INTEGER,
            openrouter_count INTEGER,
            changes_count   INTEGER,
            ok              INTEGER,
            detail          TEXT
        );

        CREATE TABLE IF NOT EXISTS job_state (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        """
    )
    con.commit()
    return con


def get_state(con: sqlite3.Connection, key: str, default=None):
    row = con.execute("SELECT value FROM job_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(con: sqlite3.Connection, key: str, value: str) -> None:
    with con:
        con.execute(
            "INSERT INTO job_state(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


# ---------------------------------------------------------------------------
# 抓取与归一化
# ---------------------------------------------------------------------------

class SourceError(RuntimeError):
    pass


def build_openrouter_rows(cfg: dict):
    or_cfg = cfg.get("openrouter", {})
    if not or_cfg.get("enabled", True):
        return [], {"ok": False, "count": 0, "note": "已在配置中关闭"}
    data = fetch_json(or_cfg.get("url", OPENROUTER_URL), timeout=75, retries=3)
    models = data.get("data") if isinstance(data, dict) else None
    if not isinstance(models, list):
        raise SourceError("OpenRouter 返回格式异常")

    excluded_variants = {str(x).lstrip(":") for x in or_cfg.get("exclude_variants", [])}
    excluded_vendors = {str(x) for x in or_cfg.get("exclude_vendors", [])}
    rows = []
    for m in models:
        if not isinstance(m, dict):
            continue
        mid = str(m.get("id") or "").strip()
        if not mid or mid.startswith("~"):
            continue
        if "/" not in mid:
            continue
        vendor_slug = mid.split("/", 1)[0]
        if vendor_slug in excluded_vendors:
            continue
        if ":" in mid:
            variant = mid.rsplit(":", 1)[1]
            if variant in excluded_variants:
                continue
        pricing = m.get("pricing") or {}
        if not isinstance(pricing, dict):
            pricing = {}
        input_usd = per_million(pricing.get("prompt"))
        output_usd = per_million(pricing.get("completion"))
        cache_read = per_million(pricing.get("input_cache_read"))
        cache_write = per_million(pricing.get("input_cache_write"))
        if input_usd is None and output_usd is None:
            continue
        overrides = pricing.get("overrides")
        note = "阶梯计价" if isinstance(overrides, list) and overrides else ""
        created = m.get("created")
        try:
            created = int(created) if created else None
        except (TypeError, ValueError):
            created = None
        ctx = m.get("context_length")
        try:
            ctx = int(ctx) if ctx else None
        except (TypeError, ValueError):
            ctx = None
        top = m.get("top_provider") or {}
        max_out = top.get("max_completion_tokens") if isinstance(top, dict) else None
        try:
            max_out = int(max_out) if max_out else None
        except (TypeError, ValueError):
            max_out = None
        name = str(m.get("name") or mid).strip()
        rows.append({
            "key": f"openrouter:{mid}",
            "source": "openrouter",
            "vendor": vendor_slug,
            "vendor_name": pretty_vendor(vendor_slug),
            "provider": "OpenRouter",
            "model_id": mid,
            "name": name,
            "input_usd": input_usd,
            "output_usd": output_usd,
            "cache_read_usd": cache_read,
            "cache_write_usd": cache_write,
            "context": ctx,
            "max_output": max_out,
            "released": created,
            "deprecation": None,
            "mode": "chat",
            "link": f"https://openrouter.ai/{mid}",
            "note": note,
            "extra": None,
        })
    if not rows:
        raise SourceError("OpenRouter 没有解析到任何模型")
    rows.sort(key=lambda r: (r["vendor_name"].lower(), r["name"].lower()))
    return rows, {"ok": True, "count": len(rows), "note": ""}


def build_official_rows(cfg: dict, or_rows):
    ll_cfg = cfg.get("litellm", {})
    if not ll_cfg.get("enabled", True):
        return [], {"ok": False, "count": 0, "note": "已在配置中关闭"}

    raw = None
    last_exc = None
    for url in ll_cfg.get("urls", LITELLM_URLS):
        try:
            candidate = json.loads(run_curl(url, timeout=120, retries=2))
            if isinstance(candidate, dict) and len(candidate) > 100:
                raw = candidate
                break
        except Exception as exc:
            last_exc = exc
            continue
    if raw is None:
        raise SourceError(f"LiteLLM 价格库抓取失败：{last_exc}")

    providers = set(cfg.get("official_providers", []))
    vendors = vendor_map(cfg)
    or_index = {}
    for r in or_rows:
        k = norm_model(r["model_id"])
        if k and k not in or_index:
            or_index[k] = r

    chosen = {}
    for key, meta in raw.items():
        if not isinstance(meta, dict):
            continue
        provider = str(meta.get("litellm_provider") or "")
        if provider not in providers:
            continue
        mode = str(meta.get("mode") or "")
        if mode not in TEXT_MODES:
            continue
        vendor_id = PROVIDER_VENDOR.get(provider)
        if not vendor_id:
            continue
        model_id = strip_provider_prefix(str(key), provider).strip()
        if not model_id or model_id.lower().startswith("ft:"):
            continue
        if model_id.lower() in {"container"}:
            continue
        prefixes = FIRST_PARTY_PREFIXES.get(provider)
        if prefixes and not model_id.lower().startswith(prefixes):
            continue
        input_usd = per_million(meta.get("input_cost_per_token"))
        output_usd = per_million(meta.get("output_cost_per_token"))
        cache_read = per_million(meta.get("cache_read_input_token_cost"))
        cache_write = per_million(meta.get("cache_creation_input_token_cost"))
        # 火山引擎在 LiteLLM 里大量条目是 0/0（表示未提供，而不是免费），
        # 直接显示 $0 会误导，统一按“未收录”处理。
        if provider == "volcengine" and input_usd == 0 and output_usd == 0:
            input_usd = output_usd = None
        note_parts = []
        tiers = meta.get("tiered_pricing")
        has_tiers = isinstance(tiers, list) and bool(tiers)
        tiered = has_tiers or any(
            k.startswith("input_cost_per_token_above_")
            or k.startswith("output_cost_per_token_above_")
            for k in meta.keys()
        )
        if (input_usd is None or (input_usd == 0 and output_usd == 0)) and has_tiers:
            # 部分厂商（阿里百炼、火山引擎）把价格只写在 tiered_pricing 里，
            # 顶层没有 input/output_cost_per_token。取最低一档作为展示价。
            try:
                first_tier = sorted(
                    tiers,
                    key=lambda t: (t.get("range") or [0])[0],
                )[0]
                input_usd = per_million(first_tier.get("input_cost_per_token"))
                output_usd = per_million(first_tier.get("output_cost_per_token"))
                if cache_read is None:
                    cache_read = per_million(first_tier.get("cache_read_input_token_cost"))
                if input_usd is not None or output_usd is not None:
                    note_parts.append("阶梯计价（首档）")
            except Exception:
                pass
        if tiered and not note_parts:
            note_parts.append("阶梯计价")
        price_from_or = False
        or_match = or_index.get(norm_model(model_id))
        if or_match and (input_usd is None or (input_usd == 0 and output_usd == 0)):
            or_in = or_match.get("input_usd")
            or_out = or_match.get("output_usd")
            if (or_in or 0) > 0 or (or_out or 0) > 0 or input_usd is None:
                input_usd = or_match.get("input_usd")
                output_usd = or_match.get("output_usd")
                if cache_read is None:
                    cache_read = or_match.get("cache_read_usd")
                cache_write = or_match.get("cache_write_usd") if cache_write is None else cache_write
                price_from_or = True
                note_parts.append("价格参考 OpenRouter")
        dep = meta.get("deprecation_date")
        if not isinstance(dep, str) or not re.match(r"^\d{4}-\d{2}-\d{2}", dep):
            dep = None
        source_url = meta.get("source")
        vendor = vendors.get(vendor_id, {})
        link = source_url if is_http_url(source_url) else vendor.get("pricing", "")
        if is_http_url(link):
            parsed = urlparse(link)
            if parsed.netloc.lower().startswith("api.") or "/v1/" in parsed.path:
                link = vendor.get("models") or vendor.get("pricing") or link
        if vendor_id in {"qwen", "tencent", "moonshot"}:
            link = vendor.get("pricing") or vendor.get("models") or link
        ctx = meta.get("max_input_tokens")
        max_out = meta.get("max_output_tokens")
        try:
            ctx = int(ctx) if ctx else None
        except (TypeError, ValueError):
            ctx = None
        try:
            max_out = int(max_out) if max_out else None
        except (TypeError, ValueError):
            max_out = None
        released = or_match.get("released") if or_match else None
        row = {
            "_priority_provider": provider,
            # key 按「厂商 + 归一化模型名」生成：同一模型在 dashscope/qwencloud
            # 或 bigmodel/zai 之间切换时不会被误判成下架 + 新增。
            "key": f"official:{vendor_id}:{norm_model(model_id)}",
            "source": "official",
            "vendor": vendor_id,
            "vendor_name": vendor.get("name") or PROVIDER_LABEL.get(provider, provider),
            "provider": PROVIDER_LABEL.get(provider, provider),
            "model_id": model_id,
            "name": model_id,
            "input_usd": input_usd,
            "output_usd": output_usd,
            "cache_read_usd": cache_read,
            "cache_write_usd": cache_write,
            "context": ctx,
            "max_output": max_out,
            "released": released,
            "deprecation": dep,
            "mode": mode,
            "link": link,
            "note": " · ".join(note_parts),
            "extra": json.dumps({
                "sources": ["litellm"],
                "price_from_openrouter": price_from_or,
            }, ensure_ascii=False),
        }
        dedupe_key = (vendor_id, norm_model(model_id))
        existing = chosen.get(dedupe_key)
        if existing is None:
            chosen[dedupe_key] = row
        else:
            priority = PROVIDER_PRIORITY.get(provider, 9)
            old_priority = PROVIDER_PRIORITY.get(existing.get("_priority_provider", ""), 9)
            if priority < old_priority:
                chosen[dedupe_key] = row

    rows = list(chosen.values())
    for r in rows:
        r.pop("_priority_provider", None)
    order = {v["id"]: i for i, v in enumerate(cfg.get("vendors", []))}
    rows.sort(key=lambda r: (order.get(r["vendor"], 99), r["model_id"].lower()))
    if not rows:
        raise SourceError("LiteLLM 没有解析到任何官方模型")
    return rows, {
        "ok": True,
        "count": len(rows),
        "entries": len(raw),
        "providers": len({str(v.get("litellm_provider")) for v in raw.values() if isinstance(v, dict)}),
        "note": "",
    }


def _date_to_epoch(value):
    if not value or not isinstance(value, str):
        return None
    try:
        d = dt.datetime.strptime(value[:10], "%Y-%m-%d")
        return int(d.replace(tzinfo=dt.timezone.utc).timestamp())
    except ValueError:
        return None


def _md_is_text_model(meta: dict) -> bool:
    """只要文本输出模型（聊天/推理/realtime），排除图像、语音、向量、重排。"""
    t = meta.get("type")
    if t in {"embedding", "image", "video", "speech", "transcription", "reranking"}:
        return False
    modalities = meta.get("modalities") or {}
    output = modalities.get("output") or []
    if output and "text" not in output:
        return False
    return True


def _md_model_allowed(pid: str, mid: str, meta: dict) -> bool:
    prefixes = MD_PREFIX_FILTER.get(pid)
    if not prefixes:
        return True
    mid_l = str(mid).lower()
    name_l = str(meta.get("name") or "").lower()
    for p in prefixes:
        if p == "nemotron":
            # NVIDIA 的 id 形如 nvidia/nemotron-...，用子串匹配
            if p in mid_l or p in name_l:
                return True
        elif mid_l.startswith(p) or name_l.startswith(p):
            return True
    return False


def build_modelsdev_rows(cfg: dict):
    """models.dev 的公开目录：第一方厂商的文本模型。"""
    md_cfg = cfg.get("modelsdev", {})
    if not md_cfg.get("enabled", True):
        return [], {"ok": False, "count": 0, "note": "已在配置中关闭"}, None

    raw = fetch_json(md_cfg.get("url", MODELSDEV_URL), timeout=150, retries=2)
    if not isinstance(raw, dict) or not raw:
        raise SourceError("models.dev 返回格式异常")

    vendors = vendor_map(cfg)
    total_models = 0
    chosen = {}
    cny_vendor_ids = {
        "qwen", "zhipu", "moonshot", "minimax", "doubao", "tencent",
        "stepfun", "xiaomi", "sensenova", "bailing", "longcat",
    }

    for pid, prov in raw.items():
        if not isinstance(prov, dict):
            continue
        models = prov.get("models") or {}
        if not isinstance(models, dict):
            continue
        total_models += len(models)
        vendor_id = MD_PROVIDER_VENDOR.get(pid)
        if not vendor_id:
            continue
        vendor = vendors.get(vendor_id, {})
        provider_name = MD_PROVIDER_LABEL.get(pid, str(prov.get("name") or pid))
        prov_doc = prov.get("doc")
        priority = MD_PROVIDER_PRIORITY.get(pid, 50)

        for mid, meta in models.items():
            if not isinstance(meta, dict):
                continue
            if not _md_is_text_model(meta):
                continue
            if not _md_model_allowed(pid, mid, meta):
                continue
            cost = meta.get("cost") or {}
            if not isinstance(cost, dict):
                cost = {}
            input_usd = per_million_already(cost.get("input"))
            output_usd = per_million_already(cost.get("output"))
            cache_read = per_million_already(cost.get("cache_read"))
            cache_write = per_million_already(cost.get("cache_write"))
            note_parts = []
            if cost.get("tiers") or cost.get("context_over_200k"):
                note_parts.append("阶梯计价")
            status = meta.get("status")
            if status == "beta":
                note_parts.append("Beta")
            elif status == "deprecated":
                note_parts.append("已废弃")

            link = prov_doc if is_http_url(prov_doc) else vendor.get("pricing", "")
            if vendor_id in cny_vendor_ids:
                link = vendor.get("pricing") or vendor.get("models") or link

            limit = meta.get("limit") or {}
            ctx = limit.get("context")
            max_out = limit.get("output")
            try:
                ctx = int(ctx) if ctx else None
            except (TypeError, ValueError):
                ctx = None
            try:
                max_out = int(max_out) if max_out else None
            except (TypeError, ValueError):
                max_out = None

            row = {
                "_priority_provider": priority,
                "key": f"official:{vendor_id}:{norm_model(mid)}",
                "source": "official",
                "vendor": vendor_id,
                "vendor_name": vendor.get("name") or provider_name,
                "provider": provider_name,
                "model_id": str(mid),
                "name": str(meta.get("name") or mid),
                "input_usd": input_usd,
                "output_usd": output_usd,
                "cache_read_usd": cache_read,
                "cache_write_usd": cache_write,
                "context": ctx,
                "max_output": max_out,
                "released": _date_to_epoch(meta.get("release_date")),
                "deprecation": None,
                "mode": "chat",
                "link": link,
                "note": " · ".join(note_parts),
                "extra": json.dumps({
                    "sources": ["models.dev"],
                    "status": status,
                    "modalities": meta.get("modalities"),
                    "open_weights": meta.get("open_weights"),
                    "family": meta.get("family"),
                }, ensure_ascii=False),
            }
            existing = chosen.get(row["key"])
            if existing is None or priority < existing.get("_priority_provider", 99):
                chosen[row["key"]] = row

    rows = list(chosen.values())
    for r in rows:
        r.pop("_priority_provider", None)
    order = {v["id"]: i for i, v in enumerate(cfg.get("vendors", []))}
    rows.sort(key=lambda r: (order.get(r["vendor"], 99), r["model_id"].lower()))
    if not rows:
        raise SourceError("models.dev 没有解析到任何第一方文本模型")
    return rows, {
        "ok": True,
        "count": len(rows),
        "providers": len(raw),
        "models": total_models,
        "note": "",
    }, raw


def _row_sources(row) -> set:
    try:
        extra = json.loads(row.get("extra") or "{}")
    except Exception:
        extra = {}
    return set(extra.get("sources") or [])


def _merge_extra(base_extra, top_extra):
    try:
        base = json.loads(base_extra or "{}")
    except Exception:
        base = {}
    try:
        top = json.loads(top_extra or "{}")
    except Exception:
        top = {}
    out = dict(base)
    out.update(top)
    sources = []
    for s in list(base.get("sources") or []) + list(top.get("sources") or []):
        if s not in sources:
            sources.append(s)
    out["sources"] = sources
    return json.dumps(out, ensure_ascii=False)


def _merge_row(base: dict, top: dict) -> dict:
    """base=LiteLLM（或沿用数据），top=models.dev；top 的非空字段优先。"""
    row = dict(base)
    for field in (
        "name", "model_id", "provider", "input_usd", "output_usd",
        "cache_read_usd", "cache_write_usd", "context", "max_output",
        "released", "link", "mode",
    ):
        v = top.get(field)
        if v is not None and v != "":
            row[field] = v
    if top.get("deprecation"):
        row["deprecation"] = top["deprecation"]
    notes = []
    for n in (base.get("note") or "", top.get("note") or ""):
        for part in n.split(" · "):
            part = part.strip()
            if part and part not in notes:
                notes.append(part)
    row["note"] = " · ".join(notes)
    row["extra"] = _merge_extra(base.get("extra"), top.get("extra"))
    return row


def merge_official_rows(base_rows, ll_rows, md_rows, cfg):
    """按 key 合并三个来源；base 是失败源沿用的旧数据。

    合并顺序很重要：
      1. 旧数据打底（只包含失败源，避免把成功源的旧模型带进来）
      2. 新鲜 LiteLLM 覆盖旧数据
      3. 新鲜 models.dev 覆盖 LiteLLM
    """
    merged = {}
    for r in base_rows:
        merged[r["key"]] = dict(r)

    def apply(rows):
        for r in rows:
            key = r["key"]
            if key in merged:
                merged[key] = _merge_row(merged[key], r)
            else:
                merged[key] = dict(r)

    apply(ll_rows)
    apply(md_rows)

    rows = list(merged.values())
    order = {v["id"]: i for i, v in enumerate(cfg.get("vendors", []))}
    rows.sort(key=lambda r: (order.get(r["vendor"], 99), (r.get("name") or r["model_id"]).lower()))
    return rows


def _md_entry(pid, prov, mid, meta):
    cost = meta.get("cost") or {}
    if not isinstance(cost, dict):
        cost = {}
    vendor_id = MD_PROVIDER_VENDOR.get(pid)
    official = bool(vendor_id and _md_model_allowed(pid, mid, meta) and _md_is_text_model(meta))
    limit = meta.get("limit") or {}
    return {
        "pid": pid,
        "provider": str(prov.get("name") or pid),
        "doc": prov.get("doc") if is_http_url(prov.get("doc")) else "",
        "name": str(meta.get("name") or mid),
        "input": per_million_already(cost.get("input")),
        "output": per_million_already(cost.get("output")),
        "context": limit.get("context"),
        "released": _date_to_epoch(meta.get("release_date")),
        "status": meta.get("status"),
        "official": official,
        "vendor_id": vendor_id if official else None,
    }


def _catalog_entry_from_row(row):
    return {
        "pid": row.get("source") or "official",
        "provider": "OpenRouter" if row.get("source") == "openrouter" else "官方目录",
        "doc": row.get("link") if is_http_url(row.get("link")) else "",
        "name": row.get("name") or row.get("model_id"),
        "input": row.get("input_usd"),
        "output": row.get("output_usd"),
        "context": row.get("context"),
        "released": row.get("released"),
        "status": None,
        "official": row.get("source") == "official",
        "vendor_id": row.get("vendor"),
    }


# 只匹配模型名 / id 的“开头”，避免把 “Cogito-Qwen-32B” 这类
# 基于 Qwen 的微调模型误归到通义千问。
VENDOR_PREFIXES = [
    ("deepseek", "deepseek"), ("claude", "anthropic"), ("anthropic", "anthropic"),
    ("gpt", "openai"), ("chatgpt", "openai"), ("openai", "openai"),
    ("codex", "openai"), ("computer-use", "openai"), ("o1", "openai"),
    ("o3", "openai"), ("o4", "openai"),
    ("gemini", "google"), ("gemma", "google"), ("google", "google"),
    ("grok", "xai"), ("qwen", "qwen"), ("qwq", "qwen"), ("qvq", "qwen"),
    ("glm", "zhipu"), ("z-ai", "zhipu"), ("z.ai", "zhipu"), ("zhipu", "zhipu"),
    ("kimi", "moonshot"), ("moonshot", "moonshot"),
    ("minimax", "minimax"), ("doubao", "doubao"), ("seed", "doubao"),
    ("mistral", "mistral"), ("codestral", "mistral"), ("devstral", "mistral"),
    ("magistral", "mistral"), ("pixtral", "mistral"),
    ("command", "cohere"), ("aya", "cohere"), ("cohere", "cohere"),
    ("meta", "meta"), ("llama", "meta"), ("muse", "meta"),
    ("nvidia", "nvidia"), ("nemotron", "nvidia"),
    ("solar", "upstage"), ("jamba", "ai21"), ("sonar", "perplexity"),
    ("fugu", "sakana"), ("namazu", "sakana"), ("sakana", "sakana"),
    ("trinity", "arcee"), ("arcee", "arcee"),
    ("mercury", "inception"), ("laguna", "poolside"),
    ("mimo", "xiaomi"), ("xiaomi", "xiaomi"),
    ("ling", "bailing"), ("ring", "bailing"), ("longcat", "longcat"),
    ("sarvam", "sarvam"), ("sensenova", "sensenova"),
    ("granite", "ibm"), ("ibm", "ibm"),
    ("hunyuan", "tencent"), ("hy3", "tencent"), ("hy4", "tencent"),
    ("tencent", "tencent"), ("nova", "amazon"), ("amazon", "amazon"),
    ("stepfun", "stepfun"), ("step", "stepfun"),
    ("yi", "lingyiwanwu"), ("ernie", "baidu"), ("文心", "baidu"),
    ("spark", "iflytek"), ("pangu", "huawei"), ("盘古", "huawei"),
    ("phi", "microsoft"), ("microsoft", "microsoft"), ("baichuan", "baichuan"),
]


def infer_catalog_vendor(cid, mid, meta, pid):
    """从 canonical id / 模型名 / 模型 id 开头推断模型作者，而不是渠道商。"""
    cid_text = str(cid)
    if "/" in cid_text:
        prefix = cid_text.split("/", 1)[0].lower()
        if prefix in CANON_PREFIX_VENDOR:
            return CANON_PREFIX_VENDOR[prefix]
    name_l = str(meta.get("name") or "").lower()
    mid_seg = str(mid).rsplit("/", 1)[-1].lower()
    for token, vid in VENDOR_PREFIXES:
        if name_l.startswith(token) or mid_seg.startswith(token):
            return vid
    # 渠道商自己的托管/路由模型，只有在确认是它自己的第一方模型时才归给它
    pv = MD_PROVIDER_VENDOR.get(pid)
    if pv and _md_model_allowed(pid, mid, meta):
        return pv
    return None


def build_catalog(md_raw, or_rows, official_rows, cfg):
    """全量模型目录：按 canonical_model_id 去重，一个模型一行，聚合渠道价。"""
    vendors = vendor_map(cfg)
    groups = {}
    by_norm = {}
    by_cid = {}

    def ensure_group(cid, norm, name, vendor_id, vendor_name):
        cid_text = str(cid)
        key = by_cid.get(cid_text)
        if key is None:
            # 用归一化后的 canonical id 作为 key：不同区域的同一模型
            # （如 alibaba-cn/qvq-max 与 alibaba/qvq-max）会合并。
            slug = norm_model(cid_text) or norm
            key = f"catalog:{slug}"
            if key in groups:
                # 同一模型的不同区域/渠道写法，合并到同一组
                by_cid[cid_text] = key
                if norm and norm not in by_norm:
                    by_norm[norm] = key
                return groups[key]
            g = {
                "key": key,
                "cid": cid_text,
                "norm": norm,
                "name": name,
                "vendor": vendor_id or "other",
                "vendor_name": vendor_name or "其他",
                "entries": [],
            }
            groups[key] = g
            by_cid[cid_text] = key
            if norm and norm not in by_norm:
                by_norm[norm] = key
        return groups[key]

    # 1) models.dev 全部渠道（包括转售商），按 canonical id 归并
    if isinstance(md_raw, dict):
        for pid, prov in md_raw.items():
            if not isinstance(prov, dict):
                continue
            for mid, meta in (prov.get("models") or {}).items():
                if not isinstance(meta, dict) or not _md_is_text_model(meta):
                    continue
                cid = meta.get("canonical_model_id") or f"{pid}/{mid}"
                norm = norm_model(mid)
                vendor_id = infer_catalog_vendor(cid, mid, meta, pid)
                vendor = vendors.get(vendor_id or "", {})
                prefix = str(cid).split("/")[0].lower() if "/" in str(cid) else ""
                vendor_name = vendor.get("name") or (pretty_vendor(vendor_id) if vendor_id else "其他")
                g = ensure_group(cid, norm, str(meta.get("name") or mid), vendor_id, vendor_name)
                g["entries"].append(_md_entry(pid, prov, mid, meta))

    # 2) OpenRouter 中 models.dev 没覆盖到的模型
    for row in or_rows:
        norm = norm_model(row.get("model_id"))
        key = by_norm.get(norm)
        if key:
            groups[key]["entries"].append(_catalog_entry_from_row(row))
        else:
            vendor = vendors.get(row.get("vendor") or "", {})
            g = ensure_group(row["model_id"], norm, row.get("name") or row["model_id"],
                             row.get("vendor"), vendor.get("name") or row.get("vendor_name"))
            g["entries"].append(_catalog_entry_from_row(row))

    # 3) LiteLLM/models.dev 合并后的官方模型，补漏
    for row in official_rows:
        norm = norm_model(row.get("model_id"))
        key = by_norm.get(norm)
        if key:
            # 已有 models.dev 条目时，不重复加入官方条目；但补上官方标记
            g = groups[key]
            if not any(e.get("official") for e in g["entries"]):
                g["entries"].append(_catalog_entry_from_row(row))
        else:
            vendor = vendors.get(row.get("vendor") or "", {})
            g = ensure_group(row["model_id"], norm, row.get("name") or row["model_id"],
                             row.get("vendor"), vendor.get("name") or row.get("vendor_name"))
            g["entries"].append(_catalog_entry_from_row(row))

    rows = []
    all_providers = set()
    order = {v["id"]: i for i, v in enumerate(cfg.get("vendors", []))}
    for g in groups.values():
        entries = g["entries"]
        if not entries:
            continue
        official_entries = [e for e in entries if e.get("official")]
        # 官方价优先；官方条目没有价格时退回渠道最低价
        def min_price(field):
            official_vals = [e[field] for e in official_entries if e.get(field) is not None]
            if official_vals:
                return min(official_vals), True
            channel_vals = [e[field] for e in entries if e.get(field) is not None]
            return (min(channel_vals) if channel_vals else None), False
        input_usd, official_input = min_price("input")
        output_usd, official_output = min_price("output")
        official_price = bool(official_entries and (official_input or official_output))
        contexts = [int(e["context"]) for e in entries if e.get("context")]
        released = [int(e["released"]) for e in entries if e.get("released")]
        provider_names = sorted({e["provider"] for e in entries if e.get("provider")})
        all_providers.update(provider_names)
        # 名称选最短的，尽量避开 "(EU)" / " (latest)" 之类后缀
        names = [e["name"] for e in entries if e.get("name")]
        name = min(names, key=lambda x: (len(x), x)) if names else g["name"]
        link = ""
        for e in official_entries:
            if e.get("doc"):
                link = e["doc"]
                break
        if not link:
            for e in sorted(entries, key=lambda e: e.get("input") if e.get("input") is not None else 1e18):
                if e.get("doc"):
                    link = e["doc"]
                    break
        statuses = {e.get("status") for e in entries if e.get("status")}
        status = "deprecated" if statuses and statuses == {"deprecated"} else ("beta" if "beta" in statuses else None)
        vendor_id = g["vendor"] if g["vendor"] in vendors else (official_entries[0].get("vendor_id") if official_entries else None)
        if not vendor_id or vendor_id == "other":
            vendor_id = (official_entries[0].get("vendor_id") if official_entries else None) or "other"
        vendor = vendors.get(vendor_id, {})
        rows.append({
            "key": g["key"],
            "vendor": vendor_id,
            "vendor_name": vendor.get("name") or g["vendor_name"],
            "name": name,
            "model_id": g["cid"] or g["norm"],
            "input_usd": input_usd,
            "output_usd": output_usd,
            "context": max(contexts) if contexts else None,
            "released": max(released) if released else None,
            "provider_count": len(provider_names),
            "official": bool(official_entries),
            "status": status,
            "link": link,
            "note": "官方价" if official_price else "渠道最低价",
        })
    rows.sort(key=lambda r: (order.get(r["vendor"], 99), (r["name"] or "").lower()))
    return rows, len(all_providers)


def build_coverage(cfg, sources, official_rows, catalog_rows, catalog_provider_count=0):
    vendors = cfg.get("vendors", [])
    counts = {}
    vendor_sources = {}
    for r in official_rows:
        vid = r["vendor"]
        counts[vid] = counts.get(vid, 0) + 1
        vendor_sources.setdefault(vid, set()).update(_row_sources(r))
    tracked = []
    link_only = []
    for v in vendors:
        vid = v.get("id")
        item = {
            "id": vid,
            "name": v.get("name", vid),
            "sub": v.get("sub", ""),
            "color": v.get("color", ""),
            "link_only": bool(v.get("link_only")),
            "model_count": counts.get(vid, 0),
            "sources": sorted(vendor_sources.get(vid, [])),
            "homepage": v.get("homepage", ""),
            "pricing": v.get("pricing", ""),
            "models": v.get("models", ""),
            "docs": v.get("docs", ""),
        }
        if item["link_only"]:
            link_only.append(item)
        else:
            tracked.append(item)
    tracked_with_models = [t for t in tracked if t["model_count"] > 0]
    missing = [t for t in tracked if t["model_count"] == 0]
    md = sources.get("modelsdev") or {}
    ll = sources.get("litellm") or {}
    or_meta = sources.get("openrouter") or {}
    return {
        "first_party_total": len(tracked),
        "first_party_tracked": len(tracked_with_models),
        "link_only_count": len(link_only),
        "missing_count": len(missing),
        "official_models": len(official_rows),
        "canonical_models": len(catalog_rows),
        "catalog_providers": catalog_provider_count,
        "tracked": tracked,
        "missing": missing,
        "link_only": link_only,
        "sources": [
            {"name": "models.dev", "providers": md.get("providers", 0), "models": md.get("models", 0),
             "url": MODELSDEV_URL},
            {"name": "LiteLLM", "providers": ll.get("providers", 0), "models": ll.get("entries", 0),
             "url": LITELLM_URLS[0]},
            {"name": "OpenRouter", "providers": or_meta.get("vendors", 0), "models": or_meta.get("count", 0),
             "url": OPENROUTER_URL},
        ],
        "scope": "覆盖三个公开目录中可结构化获取的第一方文本模型；纯转售/聚合渠道不逐条计入官方表，按 canonical_model_id 汇总到全量目录。",
    }


def build_openrouter_rows_with_meta(cfg):
    return build_openrouter_rows(cfg)


def fetch_fx(cfg: dict, con: sqlite3.Connection):
    """抓汇率；失败则沿用数据库里最近一次成功的汇率。"""
    url = cfg.get("fx_url", FX_URL)
    try:
        data = fetch_json(url, timeout=25, retries=2)
        rate = None
        if isinstance(data, dict):
            if data.get("result") == "success":
                rate = (data.get("rates") or {}).get("CNY")
            elif isinstance(data.get("rates"), dict):
                rate = data["rates"].get("CNY")
            if rate is None and data.get("conversion_rates"):
                rate = data["conversion_rates"].get("CNY")
        if rate:
            return round(float(rate), 4), "open.er-api.com", True
    except Exception as exc:
        err(f"⚠ 汇率抓取失败：{exc}")
    try:
        row = con.execute(
            "SELECT fx FROM runs WHERE fx IS NOT NULL ORDER BY day DESC LIMIT 1"
        ).fetchone()
        if row and row["fx"]:
            return float(row["fx"]), "沿用上次汇率", False
    except Exception:
        pass
    return 7.0, "默认汇率", False


# ---------------------------------------------------------------------------
# 快照 / 变动
# ---------------------------------------------------------------------------

def latest_day_before(con: sqlite3.Connection, day: str):
    row = con.execute(
        "SELECT MAX(day) AS d FROM snapshots WHERE day < ?", (day,)
    ).fetchone()
    return row["d"] if row and row["d"] else None


def load_rows_for_day(con: sqlite3.Connection, day: str, source=None):
    sql = "SELECT * FROM snapshots WHERE day = ?"
    args = [day]
    if source:
        sql += " AND source = ?"
        args.append(source)
    return [dict(r) for r in con.execute(sql, args).fetchall()]


def last_count_for_source(con: sqlite3.Connection, source: str, before_day: str) -> int:
    row = con.execute(
        "SELECT COUNT(*) AS n FROM snapshots WHERE source = ? AND day = ("
        " SELECT MAX(day) FROM snapshots WHERE source = ? AND day < ?)",
        (source, source, before_day),
    ).fetchone()
    return int(row["n"]) if row else 0


def carry_forward(con, source, day):
    """数据源失败时，沿用该源最近一天的快照，避免误报“下架”。"""
    row = con.execute(
        "SELECT MAX(day) AS d FROM snapshots WHERE source = ? AND day < ?",
        (source, day),
    ).fetchone()
    carried_day = row["d"] if row and row["d"] else None
    if not carried_day:
        return [], None
    rows = load_rows_for_day(con, carried_day, source=source)
    return rows, carried_day


def compute_changes(prev_rows, rows, day):
    changes = []
    if not prev_rows:
        return changes
    prev = {r["key"]: r for r in prev_rows}
    prev_sources = {r["source"] for r in prev_rows}
    cur_keys = {r["key"] for r in rows}

    for r in rows:
        key = r["key"]
        if r["source"] not in prev_sources:
            continue  # 新启用的数据源，首日不报“新增”
        old = prev.get(key)
        base = {
            "day": day, "key": key, "source": r["source"],
            "vendor": r["vendor"], "vendor_name": r["vendor_name"],
            "model_name": r["name"], "provider": r["provider"],
        }
        if old is None:
            changes.append({**base, "kind": "new", "field": "model",
                            "old_value": None, "new_value": r["name"]})
            continue
        for field, _label in PRICE_FIELDS:
            a, b = old.get(field), r.get(field)
            if _same_num(a, b):
                continue
            changes.append({**base, "kind": "price", "field": field,
                            "old_value": _num_text(a), "new_value": _num_text(b)})
        for field, _label in META_FIELDS:
            a, b = old.get(field), r.get(field)
            if (a or None) == (b or None):
                continue
            changes.append({**base, "kind": "meta", "field": field,
                            "old_value": None if a is None else str(a),
                            "new_value": None if b is None else str(b)})

    for key, old in prev.items():
        if key not in cur_keys and old["source"] in {r["source"] for r in rows}:
            changes.append({
                "day": day, "key": key, "kind": "removed", "source": old["source"],
                "vendor": old["vendor"], "vendor_name": old["vendor_name"],
                "model_name": old["name"], "provider": old["provider"],
                "field": "model", "old_value": old["name"], "new_value": None,
            })
    return changes


def _same_num(a, b):
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        return abs(float(a) - float(b)) < 0.0000005
    except (TypeError, ValueError):
        return a == b


def _num_text(v):
    return None if v is None else f"{float(v):.6f}".rstrip("0").rstrip(".")


def write_snapshot(con, day, rows, changes, run_meta):
    with con:
        con.execute("DELETE FROM snapshots WHERE day = ?", (day,))
        con.execute("DELETE FROM changes WHERE day = ?", (day,))
        con.executemany(
            """INSERT OR REPLACE INTO snapshots
               (day,key,source,vendor,vendor_name,provider,model_id,name,
                input_usd,output_usd,cache_read_usd,cache_write_usd,context,
                max_output,released,deprecation,mode,link,note,extra)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    day, r["key"], r["source"], r["vendor"], r["vendor_name"],
                    r["provider"], r["model_id"], r["name"], r["input_usd"],
                    r["output_usd"], r["cache_read_usd"], r["cache_write_usd"],
                    r["context"], r["max_output"], r["released"], r["deprecation"],
                    r["mode"], r["link"], r["note"], r.get("extra"),
                )
                for r in rows
            ],
        )
        con.executemany(
            """INSERT OR REPLACE INTO changes
               (day,key,kind,source,vendor,vendor_name,model_name,provider,
                field,old_value,new_value)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            [
                (
                    c["day"], c["key"], c["kind"], c.get("source"),
                    c.get("vendor"), c.get("vendor_name"), c.get("model_name"),
                    c.get("provider"), c["field"], c.get("old_value"),
                    c.get("new_value"),
                )
                for c in changes
            ],
        )
        con.execute(
            """INSERT OR REPLACE INTO runs
               (day,finished_at,fx,official_count,openrouter_count,
                changes_count,ok,detail)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                day, run_meta["finished_at"], run_meta.get("fx"),
                run_meta.get("official_count", 0), run_meta.get("openrouter_count", 0),
                len(changes), 1 if run_meta.get("ok") else 0,
                json.dumps(run_meta.get("sources", {}), ensure_ascii=False),
            ),
        )
        keep = int(run_meta.get("snapshot_keep_days") or 365)
        cutoff = (now_local() - dt.timedelta(days=keep)).strftime("%Y-%m-%d")
        con.execute("DELETE FROM snapshots WHERE day < ?", (cutoff,))
        con.execute("DELETE FROM changes WHERE day < ?", (cutoff,))


def mark_run_failed(con: sqlite3.Connection, day: str) -> None:
    try:
        with con:
            con.execute("UPDATE runs SET ok = 0 WHERE day = ?", (day,))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 仪表盘数据
# ---------------------------------------------------------------------------

def load_dashboard_data(con: sqlite3.Connection, cfg: dict, day=None):
    if day is None:
        row = con.execute("SELECT MAX(day) AS d FROM snapshots").fetchone()
        day = row["d"] if row and row["d"] else None
    if not day:
        raise RuntimeError("数据库里还没有任何快照，请先运行：aimon update")

    rows = load_rows_for_day(con, day)
    official = [r for r in rows if r["source"] == "official"]
    openrouter = [r for r in rows if r["source"] == "openrouter"]
    changes = [dict(r) for r in con.execute(
        "SELECT * FROM changes WHERE day = ? ORDER BY "
        "CASE kind WHEN 'price' THEN 0 WHEN 'new' THEN 1 WHEN 'removed' THEN 2 "
        "WHEN 'meta' THEN 3 ELSE 4 END, vendor_name, model_name",
        (day,),
    ).fetchall()]

    run = con.execute("SELECT * FROM runs WHERE day = ?", (day,)).fetchone()
    run = dict(run) if run else {}
    sources = {}
    try:
        sources = json.loads(run.get("detail") or "{}")
    except Exception:
        pass
    fx = run.get("fx") or 7.0

    catalog_rows = []
    coverage = {}
    try:
        if CATALOG_PATH.exists():
            catalog_rows = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        if COVERAGE_PATH.exists():
            coverage = json.loads(COVERAGE_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        err(f"⚠ 全量目录读取失败：{exc}")
    if not coverage:
        try:
            coverage = build_coverage(cfg, sources, official, catalog_rows)
        except Exception:
            coverage = {}

    prev_day = latest_day_before(con, day)
    first_run = prev_day is None

    # 历史曲线
    history_days = int(cfg.get("history_days", 90))
    start = (dt.datetime.strptime(day, "%Y-%m-%d") - dt.timedelta(days=history_days - 1)).strftime("%Y-%m-%d")
    latest_keys = {r["key"] for r in rows}
    hist_rows = con.execute(
        "SELECT day,key,name,vendor,vendor_name,source,input_usd,output_usd "
        "FROM snapshots WHERE day >= ? ORDER BY key, day",
        (start,),
    ).fetchall()
    series = {}
    for h in hist_rows:
        if h["key"] not in latest_keys:
            continue
        item = series.setdefault(h["key"], {
            "key": h["key"], "name": h["name"], "vendor_name": h["vendor_name"],
            "source": h["source"], "points": [],
        })
        item["points"].append([h["day"], h["input_usd"], h["output_usd"]])
    history = sorted(series.values(), key=lambda s: (s["source"], s["vendor_name"].lower(), s["name"].lower()))

    vendors = cfg.get("vendors", [])
    official_counts = {}
    for r in official:
        official_counts[r["vendor"]] = official_counts.get(r["vendor"], 0) + 1
    link_cards = []
    for v in list(vendors) + list(AGGREGATOR_LINKS):
        card = dict(v)
        card["model_count"] = official_counts.get(v["id"], 0)
        if v["id"] == "openrouter":
            card["model_count"] = len(openrouter)
        elif v["id"] == "litellm":
            card["model_count"] = len(official)
        elif v["id"] == "modelsdev":
            card["model_count"] = len(catalog_rows)
        link_cards.append(card)

    today = now_local().date()
    soon = today + dt.timedelta(days=60)
    deprecations = []
    for r in official:
        if not r.get("deprecation"):
            continue
        try:
            d = dt.date.fromisoformat(r["deprecation"][:10])
        except ValueError:
            continue
        if today <= d <= soon:
            deprecations.append({
                "key": r["key"], "name": r["name"], "vendor_name": r["vendor_name"],
                "deprecation": r["deprecation"], "link": r["link"],
                "past": False,
            })
    deprecations.sort(key=lambda x: x["deprecation"])

    recent_days = int((cfg.get("openrouter") or {}).get("recent_days", 14))
    cutoff = time.time() - recent_days * 86400
    recent = []
    for r in openrouter:
        if r.get("released") and r["released"] >= cutoff:
            recent.append({
                "name": r["name"], "vendor_name": r["vendor_name"],
                "released": r["released"], "link": r["link"],
                "input_usd": r["input_usd"], "output_usd": r["output_usd"],
            })
    recent.sort(key=lambda x: x["released"], reverse=True)
    recent = recent[:12]

    price_change_keys = {c["key"] for c in changes if c["kind"] == "price"}
    new_keys = {c["key"] for c in changes if c["kind"] == "new"}
    removed_keys = {c["key"] for c in changes if c["kind"] == "removed"}

    data = {
        "generated_at": run.get("finished_at") or now_local().strftime("%Y-%m-%d %H:%M:%S"),
        "day": day,
        "first_run": first_run,
        "fx": fx,
        "stats": {
            "official": len(official),
            "openrouter": len(openrouter),
            "total": len(rows),
            "catalog": len(catalog_rows),
            "first_party_vendors": coverage.get("first_party_tracked", len({r["vendor"] for r in official})),
            "known_vendors": coverage.get("first_party_total", len(cfg.get("vendors", []))),
            "price_changes": len(price_change_keys),
            "new": len(new_keys),
            "removed": len(removed_keys),
            "deprecations": len(deprecations),
        },
        "sources": sources,
        "run_ok": bool(run.get("ok", 1)),
        "official": official,
        "openrouter": openrouter,
        "catalog": catalog_rows,
        "coverage": coverage,
        "changes": changes,
        "history": history,
        "deprecations": deprecations,
        "recent": recent,
        "vendors": link_cards,
        "vendor_colors": VENDOR_COLORS,
        "official_provider_count": len({r["vendor"] for r in official}),
        "openrouter_vendor_count": len({r["vendor"] for r in openrouter}),
    }
    return data


# ---------------------------------------------------------------------------
# 渲染 HTML / 日报
# ---------------------------------------------------------------------------

def render_dashboard(data: dict):
    if not TEMPLATE_PATH.exists():
        raise RuntimeError(f"缺少模板文件：{TEMPLATE_PATH}")
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("</", "<\\/")
    html_out = template.replace("/*__AIMON_DATA__*/", payload)
    atomic_write_text(DASH_PATH, html_out)
    atomic_write_text(LATEST_PATH, json.dumps(data, ensure_ascii=False, indent=1) + "\n")


def fmt_price(v):
    if v is None:
        return "—"
    v = float(v)
    if v == 0:
        return "0"
    if v < 0.01:
        return f"{v:.4f}"
    if v < 1:
        return f"{v:.3f}"
    if v < 100:
        return f"{v:.2f}"
    return f"{v:.1f}"


def generate_report(data: dict) -> Path:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    day = data["day"]
    path = REPORT_DIR / f"{day}.md"
    lines = [
        f"# AI 模型雷达 · {day}",
        "",
        f"- 更新时间：{data['generated_at']}",
        f"- 官方 API 模型：{data['stats']['official']} 个（{data['official_provider_count']} 家）",
        f"- OpenRouter：{data['stats']['openrouter']} 个（{data['openrouter_vendor_count']} 家）",
        f"- 全量目录：{data['stats'].get('catalog', 0)} 个规范模型（canonical ID 去重）",
        f"- 美元/人民币：{data['fx']}",
        f"- 今日变动：价格 {data['stats']['price_changes']} · 新增 {data['stats']['new']} · "
        f"下架 {data['stats']['removed']}",
        "",
    ]
    if data["first_run"]:
        lines += ["> 首次建立快照，明天起开始显示模型与价格变动。", ""]

    groups = {"new": [], "price": [], "removed": [], "meta": []}
    for c in data["changes"]:
        groups.setdefault(c["kind"], []).append(c)

    def change_name(c):
        return c.get("model_name") or c.get("name") or c.get("key") or "未知模型"

    def change_vendor(c):
        return c.get("vendor_name") or c.get("vendor") or ""

    def add_group(title, items, limit=40):
        if not items:
            return
        lines.append(f"## {title}（{len(items)}）")
        lines.append("")
        for c in items[:limit]:
            if c["kind"] == "new":
                lines.append(f"- **{change_name(c)}** · {change_vendor(c)} · 新收录")
            elif c["kind"] == "price":
                label = dict(PRICE_FIELDS).get(c["field"], c["field"])
                lines.append(
                    f"- **{change_name(c)}** · {change_vendor(c)} · {label} "
                    f"${fmt_price(float(c['old_value']) if c['old_value'] else None)} → "
                    f"${fmt_price(float(c['new_value']) if c['new_value'] else None)}"
                )
            elif c["kind"] == "removed":
                lines.append(f"- **{change_name(c)}** · {change_vendor(c)} · 已从数据源移除")
            else:
                label = dict(META_FIELDS).get(c["field"], c["field"])
                lines.append(f"- **{change_name(c)}** · {change_vendor(c)} · {label} "
                             f"{c['old_value'] or '—'} → {c['new_value'] or '—'}")
        if len(items) > limit:
            lines.append(f"- …另有 {len(items) - limit} 条")
        lines.append("")

    add_group("新增模型", groups.get("new", []))
    add_group("价格变动", groups.get("price", []))
    add_group("下架 / 移除", groups.get("removed", []))
    add_group("元数据变化", groups.get("meta", []))

    active_dep = list(data.get("deprecations", []))
    if active_dep:
        lines.append(f"## 即将废弃（{len(active_dep)}）")
        lines.append("")
        for d in active_dep[:40]:
            lines.append(f"- **{d['name']}** · {d['vendor_name']} · {d['deprecation']}")
        lines.append("")

    lines += [
        "## 说明",
        "",
        "- 官方价格合并自 models.dev 与 LiteLLM 两个公开目录；OpenRouter 价格为其渠道实时价，三者可能不同。",
        "- 价格单位：美元 / 百万 tokens；人民币按当日汇率估算。",
        "- 数据仅供参考，下单前请以厂商官网为准。",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def print_summary(data: dict, report_path=None):
    s = data["stats"]
    print("")
    print(f"📡 AI 模型雷达 · {data['day']} {data['generated_at'][11:]}")
    print(f"   官方 API  {s['official']} 个模型 / {data['official_provider_count']} 家厂商")
    print(f"   OpenRouter {s['openrouter']} 个模型 / {data['openrouter_vendor_count']} 家厂商")
    print(f"   全量目录  {s.get('catalog', 0)} 个规范模型（按 canonical ID 去重）")
    print(f"   汇率 1 USD = {data['fx']} CNY")
    status = []
    for name, label in (("openrouter", "OpenRouter"), ("litellm", "LiteLLM"), ("modelsdev", "models.dev")):
        info = (data.get("sources") or {}).get(name) or {}
        if info.get("ok"):
            status.append(f"{label} ✓")
        elif info.get("carried_from"):
            status.append(f"{label} ⚠ 沿用 {info['carried_from']}")
        else:
            status.append(f"{label} ✗")
    print(f"   数据源  {' · '.join(status)}")
    if data["first_run"]:
        print("   ✨ 首次快照已建立，明天起显示变动")
    else:
        print(f"   今日变动  价格 {s['price_changes']} · 新增 {s['new']} · "
              f"下架 {s['removed']} · 即将废弃 {s['deprecations']}")
        for c in data["changes"][:8]:
            if c["kind"] == "price":
                label = dict(PRICE_FIELDS).get(c["field"], c["field"])
                old = float(c["old_value"]) if c["old_value"] else 0
                new = float(c["new_value"]) if c["new_value"] else 0
                pct = (new - old) / old * 100 if old else 0
                arrow = "↑" if new > old else "↓"
                print(f"     {arrow} {c.get('model_name') or c.get('key')}（{c.get('vendor_name') or c.get('vendor')}）"
                      f"{label} ${fmt_price(old)} → ${fmt_price(new)}  {pct:+.1f}%")
            elif c["kind"] == "new":
                print(f"     + {c.get('model_name') or c.get('key')}（{c.get('vendor_name') or c.get('vendor')}）新收录")
            elif c["kind"] == "removed":
                print(f"     - {c.get('model_name') or c.get('key')}（{c.get('vendor_name') or c.get('vendor')}）下架")
    print(f"   面板  {DASH_PATH}")
    if report_path:
        print(f"   日报  {report_path}")


def send_notification(message: str, subtitle: str):
    script = (
        'display notification {} with title "AI 模型雷达" subtitle {}'
    ).format(json.dumps(message, ensure_ascii=False), json.dumps(subtitle, ensure_ascii=False))
    try:
        subprocess.run(["/usr/bin/osascript", "-e", script], timeout=10,
                       capture_output=True)
    except Exception:
        pass


def snapshot_hash(rows) -> str:
    h = hashlib.sha256()
    for r in sorted(rows, key=lambda x: x["key"]):
        h.update((
            f'{r["key"]}|{r.get("name")}|{r.get("input_usd")}|'
            f'{r.get("output_usd")}|{r.get("context")}|'
            f'{r.get("deprecation")}'
        ).encode("utf-8"))
    return h.hexdigest()


def maybe_notify(con, cfg, args, rows, changes, mode):
    """只在状态真的变化时通知，避免每次轮询重复弹窗。"""
    if not (getattr(args, "notify", False) or cfg.get("notify")):
        return
    state_hash = snapshot_hash(rows)
    last_hash = get_state(con, "last_notify_hash")
    set_state(con, "last_notify_hash", state_hash)
    if last_hash is None or not changes:
        return
    price = sum(1 for c in changes if c["kind"] == "price")
    new = sum(1 for c in changes if c["kind"] == "new")
    removed = sum(1 for c in changes if c["kind"] == "removed")
    parts = []
    if price:
        parts.append(f"价格 {price}")
    if new:
        parts.append(f"新增 {new}")
    if removed:
        parts.append(f"下架 {removed}")
    message = " · ".join(parts) or f"{len(changes)} 条变化"
    subtitle = f"{mode} · {len(rows)} 个模型"
    send_notification(message, subtitle)


def publish_dashboard(cfg, rows, con):
    """把当前 dashboard 直接推到 GitHub Pages 的 gh-pages 分支。

    GitHub Actions 的 Pages artifact 偶尔会抽风，而且 scheduled workflow
    会被 GitHub 延迟。本机 15 分钟/3 小时更新后直接 push，页面更稳。
    """
    pub = cfg.get("publish") or {}
    if not pub.get("enabled"):
        return
    worktree = Path(pub.get("worktree", "")).expanduser()
    if not worktree.exists():
        err(f"⚠ 发布目录不存在：{worktree}")
        return
    index = worktree / "index.html"
    state_hash = snapshot_hash(rows)
    if get_state(con, "last_publish_hash") == state_hash and index.exists():
        return

    html = DASH_PATH.read_text(encoding="utf-8")
    changed = True
    if index.exists():
        try:
            changed = index.read_text(encoding="utf-8") != html
        except Exception:
            changed = True
    if changed:
        atomic_write_text(index, html)
        atomic_write_text(worktree / ".nojekyll", "")
        subprocess.run(
            ["/usr/bin/git", "-C", str(worktree), "add", "index.html", ".nojekyll"],
            capture_output=True,
        )
        staged = subprocess.run(
            ["/usr/bin/git", "-C", str(worktree), "diff", "--cached", "--quiet"],
            capture_output=True,
        )
        if staged.returncode != 0:
            subprocess.run(
                ["/usr/bin/git", "-C", str(worktree),
                 "-c", "user.name=aimon-bot",
                 "-c", "user.email=aimon-bot@users.noreply.github.com",
                 "commit", "--amend", "-m",
                 f"dashboard: {now_local().strftime('%Y-%m-%d %H:%M:%S')}"],
                capture_output=True,
            )

    push = subprocess.run(
        ["/usr/bin/git", "-C", str(worktree), "push", "--force", "origin",
         pub.get("branch", "gh-pages")],
        capture_output=True, text=True, timeout=90,
    )
    if push.returncode == 0:
        set_state(con, "last_publish_hash", state_hash)
        log("线上页面已推送到 gh-pages")
    else:
        err(f"⚠ 线上页面推送失败：{(push.stderr or push.stdout or '').strip()[:200]}")


# ---------------------------------------------------------------------------
# 命令
# ---------------------------------------------------------------------------

def cmd_update(args):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    con = connect_db()
    if getattr(args, "light", False):
        return run_light(cfg, con, args)
    day = today_str()
    started = now_local()

    sources = {}
    or_rows = []
    ll_rows = []
    md_rows = []
    md_raw = None

    # --- OpenRouter ---
    try:
        or_rows, meta = build_openrouter_rows(cfg)
        last_n = last_count_for_source(con, "openrouter", day)
        if last_n >= 50 and len(or_rows) < last_n * 0.5:
            raise SourceError(f"模型数量异常（{len(or_rows)} / 上次 {last_n}）")
        meta["vendors"] = len({r["vendor"] for r in or_rows})
        sources["openrouter"] = meta
    except Exception as exc:
        err(f"⚠ OpenRouter 抓取失败：{exc}")
        carried, carried_day = carry_forward(con, "openrouter", day)
        if carried:
            or_rows = [{k: v for k, v in r.items() if k != "day"} for r in carried]
            sources["openrouter"] = {
                "ok": False, "count": len(or_rows),
                "vendors": len({r["vendor"] for r in or_rows}),
                "carried_from": carried_day,
                "note": f"沿用 {carried_day} 的数据",
            }
        else:
            sources["openrouter"] = {"ok": False, "count": 0, "vendors": 0, "note": str(exc)}

    # --- LiteLLM ---
    try:
        ll_rows, meta = build_official_rows(cfg, or_rows)
        sources["litellm"] = meta
    except Exception as exc:
        err(f"⚠ LiteLLM 抓取失败：{exc}")
        sources["litellm"] = {"ok": False, "count": 0, "note": str(exc)}

    # --- models.dev ---
    try:
        md_rows, meta, md_raw = build_modelsdev_rows(cfg)
        sources["modelsdev"] = meta
    except Exception as exc:
        err(f"⚠ models.dev 抓取失败：{exc}")
        sources["modelsdev"] = {"ok": False, "count": 0, "note": str(exc)}

    if not or_rows and not ll_rows and not md_rows:
        err("❌ 所有数据源都不可用，且没有历史快照可沿用。")
        return 1

    fx, fx_source, fx_ok = fetch_fx(cfg, con)
    sources["fx"] = {"ok": fx_ok, "rate": fx, "note": fx_source}

    prev_day = latest_day_before(con, day)
    prev_rows = load_rows_for_day(con, prev_day) if prev_day else []
    prev_official = [r for r in prev_rows if r["source"] == "official"]

    md_failed = not sources.get("modelsdev", {}).get("ok")
    ll_failed = not sources.get("litellm", {}).get("ok")

    # 某个源失败时，沿用该源的旧行，并让旧数据优先于另一个源的“降级值”。
    md_prev = [r for r in prev_official if "models.dev" in _row_sources(r)] if md_failed else []
    ll_prev = [r for r in prev_official if "litellm" in _row_sources(r)] if ll_failed else []
    if md_failed and md_prev:
        sources["modelsdev"]["carried_from"] = prev_day
        sources["modelsdev"]["count"] = len(md_prev)
    if ll_failed and ll_prev:
        sources["litellm"]["carried_from"] = prev_day
        sources["litellm"]["count"] = len(ll_prev)

    if md_failed and ll_failed:
        # 两个官方源都失败：完整沿用旧数据
        official_rows = merge_official_rows(prev_official, [], [], cfg)
    elif md_failed:
        # models.dev 失败：以新鲜 LiteLLM 打底，再把旧 models.dev 数据盖回去，
        # 避免因为降级到 LiteLLM 而误报几百条价格变动。
        official_rows = merge_official_rows(ll_rows, [], md_prev, cfg)
    elif ll_failed:
        # LiteLLM 失败：以旧 LiteLLM 打底，新鲜 models.dev 优先覆盖。
        official_rows = merge_official_rows(ll_prev, [], md_rows, cfg)
    else:
        official_rows = merge_official_rows([], ll_rows, md_rows, cfg)
    if not official_rows and not or_rows:
        err("❌ 官方目录和 OpenRouter 都没有可用数据。")
        return 1

    # 全量目录 / 覆盖审计
    catalog_rows = []
    coverage = {}
    if md_raw is not None:
        catalog_rows, catalog_provider_count = build_catalog(md_raw, or_rows, official_rows, cfg)
        coverage = build_coverage(cfg, sources, official_rows, catalog_rows, catalog_provider_count)
        atomic_write_text(CATALOG_PATH, json.dumps(catalog_rows, ensure_ascii=False, separators=(",", ":")) + "\n")
        atomic_write_text(COVERAGE_PATH, json.dumps(coverage, ensure_ascii=False, indent=1) + "\n")
    else:
        if CATALOG_PATH.exists() and COVERAGE_PATH.exists():
            try:
                catalog_rows = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
                coverage = json.loads(COVERAGE_PATH.read_text(encoding="utf-8"))
            except Exception:
                catalog_rows = []
                coverage = {}
        if not catalog_rows:
            catalog_rows, catalog_provider_count = build_catalog(None, or_rows, official_rows, cfg)
            coverage = build_coverage(cfg, sources, official_rows, catalog_rows, catalog_provider_count)
            atomic_write_text(CATALOG_PATH, json.dumps(catalog_rows, ensure_ascii=False, separators=(",", ":")) + "\n")
            atomic_write_text(COVERAGE_PATH, json.dumps(coverage, ensure_ascii=False, indent=1) + "\n")

    rows = official_rows + or_rows
    changes = compute_changes(prev_rows, rows, day)

    run_meta = {
        "finished_at": now_local().strftime("%Y-%m-%d %H:%M:%S"),
        "fx": fx,
        "official_count": len(official_rows),
        "openrouter_count": len(or_rows),
        "catalog_count": len(catalog_rows),
        "ok": bool(
            sources.get("openrouter", {}).get("ok")
            and sources.get("litellm", {}).get("ok")
            and sources.get("modelsdev", {}).get("ok")
        ),
        "sources": sources,
        "snapshot_keep_days": cfg.get("snapshot_keep_days", 365),
    }
    write_snapshot(con, day, rows, changes, run_meta)
    set_state(con, "last_full_at", now_local().isoformat())

    try:
        data = load_dashboard_data(con, cfg, day=day)
        render_dashboard(data)
        try:
            publish_dashboard(cfg, rows, con)
        except Exception as exc:
            err(f"⚠ 线上页面发布失败：{exc}")
        report_path = generate_report(data)
        if not args.quiet:
            print_summary(data, report_path)
        elif not run_meta["ok"]:
            err("⚠ 本次更新部分降级，详见日志。")
        maybe_notify(con, cfg, args, rows, changes, "全量更新")
    except Exception:
        mark_run_failed(con, day)
        raise

    elapsed = (now_local() - started).total_seconds()
    log(f"全量更新完成，用时 {elapsed:.1f}s，写入 {len(rows)} 条记录，{len(changes)} 条变动。")
    return 0


def print_light_summary(data, tick_changes, report_path=None):
    price = sum(1 for c in tick_changes if c["kind"] == "price")
    new = sum(1 for c in tick_changes if c["kind"] == "new")
    removed = sum(1 for c in tick_changes if c["kind"] == "removed")
    print("")
    print(f"🔄 轻量更新 · {data['generated_at']}")
    print(f"   OpenRouter {data['stats']['openrouter']} 个模型 · 官方快照沿用 {data['stats']['official']} 个")
    print(f"   本次变化  价格 {price} · 新增 {new} · 下架 {removed}")
    print(f"   面板  {DASH_PATH}")
    if report_path:
        print(f"   日报  {report_path}")


def run_light(cfg, con, args):
    """只抓 OpenRouter，官方目录沿用当天全量快照；用于 15 分钟级检查。"""
    started = now_local()
    day = today_str()
    today_rows = load_rows_for_day(con, day)
    official = [r for r in today_rows if r["source"] == "official"]
    if not official:
        log("今天还没有官方快照，转为全量更新。")
        return cmd_update(argparse.Namespace(
            quiet=getattr(args, "quiet", False),
            notify=getattr(args, "notify", False),
            light=False,
        ))

    old_or = [r for r in today_rows if r["source"] == "openrouter"]
    try:
        or_rows, meta = build_openrouter_rows(cfg)
        if old_or and len(or_rows) < len(old_or) * 0.5:
            raise SourceError(f"OpenRouter 数量异常（{len(or_rows)} / 上次 {len(old_or)}）")
        meta["vendors"] = len({r["vendor"] for r in or_rows})
    except Exception as exc:
        err(f"⚠ OpenRouter 轻量更新失败：{exc}")
        return 1

    tick_changes = compute_changes(old_or, or_rows, day) if old_or else []

    run = con.execute("SELECT * FROM runs WHERE day = ?", (day,)).fetchone()
    run = dict(run) if run else {}
    try:
        sources = json.loads(run.get("detail") or "{}")
    except Exception:
        sources = {}
    sources["openrouter"] = meta
    sources.setdefault("fx", {"ok": True, "rate": run.get("fx") or 7.0,
                              "note": "沿用上次汇率"})
    fx = run.get("fx") or sources.get("fx", {}).get("rate") or 7.0

    prev_day = latest_day_before(con, day)
    prev_rows = load_rows_for_day(con, prev_day) if prev_day else []
    rows = official + or_rows
    changes = compute_changes(prev_rows, rows, day)

    ok = all(
        sources.get(k, {}).get("ok", False)
        for k in ("openrouter", "litellm", "modelsdev")
    )
    run_meta = {
        "finished_at": now_local().strftime("%Y-%m-%d %H:%M:%S"),
        "fx": fx,
        "official_count": len(official),
        "openrouter_count": len(or_rows),
        "ok": ok,
        "sources": sources,
        "snapshot_keep_days": cfg.get("snapshot_keep_days", 365),
    }
    write_snapshot(con, day, rows, changes, run_meta)

    try:
        data = load_dashboard_data(con, cfg, day=day)
        render_dashboard(data)
        try:
            publish_dashboard(cfg, rows, con)
        except Exception as exc:
            err(f"⚠ 线上页面发布失败：{exc}")
        report_path = generate_report(data)
        if not getattr(args, "quiet", False):
            print_light_summary(data, tick_changes, report_path)
        maybe_notify(con, cfg, args, rows, tick_changes, "轻量更新")
    except Exception:
        mark_run_failed(con, day)
        raise

    elapsed = (now_local() - started).total_seconds()
    log(f"轻量更新完成，用时 {elapsed:.1f}s，OpenRouter {len(or_rows)} 个，"
        f"本次 {len(tick_changes)} 条变化。")
    return 0


def cmd_tick(args):
    """launchd 每隔 15 分钟调用：距上次全量超过 3 小时则全量，否则轻量。"""
    cfg = load_config()
    con = connect_db()
    day = today_str()
    has_official = con.execute(
        "SELECT COUNT(*) AS n FROM snapshots WHERE day = ? AND source = 'official'",
        (day,),
    ).fetchone()["n"]
    last_full = get_state(con, "last_full_at")
    due = True
    if has_official and last_full:
        try:
            last_dt = dt.datetime.fromisoformat(last_full)
            interval = float(cfg.get("full_interval_hours", 3)) * 3600
            due = (now_local() - last_dt).total_seconds() >= interval
        except Exception:
            due = True
    if due:
        log("tick：开始全量更新")
        return cmd_update(argparse.Namespace(
            quiet=getattr(args, "quiet", False),
            notify=getattr(args, "notify", False),
            light=False,
        ))
    return run_light(cfg, con, argparse.Namespace(
        quiet=getattr(args, "quiet", False),
        notify=getattr(args, "notify", False),
        light=True,
    ))


def cmd_report(args):
    cfg = load_config()
    con = connect_db()
    data = load_dashboard_data(con, cfg)
    print_summary(data)
    return 0


def cmd_links(args):
    cfg = load_config()
    con = connect_db()
    try:
        data = load_dashboard_data(con, cfg)
    except Exception:
        data = {"vendors": [dict(v, model_count=0) for v in cfg.get("vendors", [])]}
    for v in data.get("vendors", []):
        if v.get("id") == "openrouter":
            continue
        print(f"\n{v['name']}  ({v.get('sub', '')})  {v.get('model_count', 0)} 个模型")
        for key, label in (("homepage", "官网"), ("pricing", "定价"),
                           ("models", "模型"), ("docs", "文档")):
            if v.get(key):
                print(f"  {label}: {v[key]}")
    return 0


def cmd_history(args):
    cfg = load_config()
    con = connect_db()
    q = args.keyword.strip().lower()
    if not q:
        err("用法：aimon history <模型关键词> [--days 90]")
        return 2
    rows = con.execute(
        "SELECT DISTINCT key,name,vendor_name,source FROM snapshots "
        "WHERE lower(name) LIKE ? OR lower(key) LIKE ? ORDER BY lower(name) LIMIT 30",
        (f"%{q}%", f"%{q}%"),
    ).fetchall()
    if not rows:
        print(f"没有找到包含「{args.keyword}」的模型。")
        return 1
    exact = [r for r in rows if r["name"].lower() == q or r["key"].lower() == q]
    if len(exact) > 1:
        official_exact = [r for r in exact if r["source"] == "official"]
        if len(official_exact) == 1:
            exact = official_exact
    if len(exact) == 1:
        rows = exact
    if len(rows) > 1:
        print("匹配到多个模型，请缩小关键词：")
        for r in rows:
            print(f"  {r['name']}  ({r['vendor_name']}, {r['source']})")
        return 0
    model = rows[0]
    start = (now_local() - dt.timedelta(days=args.days)).strftime("%Y-%m-%d")
    hist = con.execute(
        "SELECT day,input_usd,output_usd FROM snapshots WHERE key=? AND day>=? "
        "ORDER BY day", (model["key"], start),
    ).fetchall()
    print(f"\n{model['name']}  ({model['vendor_name']} · {model['source']})")
    print(f"{'日期':<12}{'输入 $/M':>12}{'输出 $/M':>12}")
    for h in hist:
        def f(v):
            return "—" if v is None else f"{v:.4f}".rstrip("0").rstrip(".")
        print(f"{h['day']:<12}{f(h['input_usd']):>12}{f(h['output_usd']):>12}")
    return 0


def cmd_open(args):
    if not DASH_PATH.exists():
        err("仪表盘还不存在，请先运行：aimon update")
        return 1
    subprocess.run(["/usr/bin/open", str(DASH_PATH)])
    return 0


def make_plist(cfg):
    interval = int(cfg.get("light_interval_minutes", 15)) * 60
    full_hours = cfg.get("full_interval_hours", 3)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.mac.aimonitor</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>{APP_DIR}/aimon.py</string>
    <string>tick</string>
    <string>--quiet</string>
  </array>

  <!-- 每 {interval // 60} 分钟轻量检查；距上次全量超过 {full_hours} 小时则自动全量。 -->
  <key>StartInterval</key>
  <integer>{interval}</integer>
  <key>RunAtLoad</key>
  <true/>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>LANG</key><string>zh_CN.UTF-8</string>
  </dict>
  <key>StandardOutPath</key>
  <string>{APP_DIR}/aimon.log</string>
  <key>StandardErrorPath</key>
  <string>{APP_DIR}/aimon.err.log</string>
  <key>ProcessType</key>
  <string>Background</string>
</dict>
</plist>
"""


def cmd_install(args):
    cfg = load_config()
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.write_text(make_plist(cfg), encoding="utf-8")
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/com.mac.aimonitor"],
                   capture_output=True)
    p = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(PLIST_PATH)],
                       capture_output=True, text=True)
    if p.returncode != 0:
        p2 = subprocess.run(["launchctl", "load", "-w", str(PLIST_PATH)],
                            capture_output=True, text=True)
        if p2.returncode != 0:
            err(f"❌ launchd 安装失败：{p.stderr.strip() or p2.stderr.strip()}")
            return 1
    # 桌面软链接
    try:
        if DESKTOP_LINK.is_symlink():
            DESKTOP_LINK.unlink()
        if not DESKTOP_LINK.exists():
            DESKTOP_LINK.symlink_to(DASH_PATH)
        if DESKTOP_REFRESH.is_symlink():
            DESKTOP_REFRESH.unlink()
        if not DESKTOP_REFRESH.exists() and (APP_DIR / "refresh.command").exists():
            DESKTOP_REFRESH.symlink_to(APP_DIR / "refresh.command")
    except Exception as exc:
        err(f"⚠ 桌面软链接创建失败：{exc}")
    print("✅ 已安装准实时定时任务")
    print(f"   轻量检查：每 {int(cfg.get('light_interval_minutes', 15))} 分钟（只抓 OpenRouter）")
    print(f"   全量更新：距上次超过 {cfg.get('full_interval_hours', 3)} 小时（models.dev + LiteLLM + 全量目录）")
    print("   登录 / 开机时也会补跑")
    print(f"   面板：{DASH_PATH}")
    print(f"   快捷：{DESKTOP_LINK}")
    print(f"   日志：{APP_DIR}/aimon.log")
    return 0


def cmd_status(args):
    cfg = load_config()
    con = connect_db()
    row = con.execute("SELECT MAX(day) AS d FROM snapshots").fetchone()
    day = row["d"] if row else None
    print(f"数据目录：{APP_DIR}")
    print(f"数据库：{DB_PATH}  ({DB_PATH.stat().st_size / 1024:.0f} KB)" if DB_PATH.exists() else "数据库：尚未创建")
    if day:
        run = con.execute("SELECT * FROM runs WHERE day=?", (day,)).fetchone()
        n = con.execute("SELECT COUNT(*) AS n FROM snapshots WHERE day=?", (day,)).fetchone()["n"]
        print(f"最新快照：{day}（{n} 条）")
        if run:
            print(f"最近运行：{run['finished_at']}  ok={bool(run['ok'])}  变动={run['changes_count']}")
    else:
        print("最新快照：无")
    p = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/com.mac.aimonitor"],
                       capture_output=True, text=True)
    last_full = get_state(con, "last_full_at")
    if last_full:
        print(f"上次全量：{last_full}")
    print(f"更新策略：每 {int(cfg.get('light_interval_minutes', 15))} 分钟轻量检查 / "
          f"每 {cfg.get('full_interval_hours', 3)} 小时全量")
    print(f"定时任务：{'已加载' if p.returncode == 0 else '未加载'}")
    if DESKTOP_LINK.exists():
        print(f"桌面快捷：{DESKTOP_LINK}")
    if DESKTOP_REFRESH.exists():
        print(f"手动刷新：{DESKTOP_REFRESH}")
    return 0


def cmd_install_cli(args):
    target = Path("/opt/homebrew/bin/aimon")
    wrapper = f"""#!/bin/bash
exec /usr/bin/python3 "{APP_DIR}/aimon.py" "$@"
"""
    WRAPPER_PATH.write_text(wrapper, encoding="utf-8")
    WRAPPER_PATH.chmod(0o755)
    try:
        if target.exists() or target.is_symlink():
            target.unlink()
        target.symlink_to(WRAPPER_PATH)
        print(f"✅ 已安装命令：aimon -> {WRAPPER_PATH}")
    except Exception as exc:
        print(f"⚠ 未能安装到 {target}：{exc}")
        print(f"   可直接运行：{WRAPPER_PATH}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="aimon", description="AI 模型雷达：持续跟踪模型版本与 API 价格")
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser("update", help="抓取数据并生成仪表盘")
    p.add_argument("--quiet", action="store_true", help="只输出错误")
    p.add_argument("--notify", action="store_true", help="有变动时发 macOS 通知")
    p.add_argument("--light", action="store_true", help="只抓 OpenRouter，官方目录沿用当天快照")
    p.set_defaults(func=cmd_update)

    p = sub.add_parser("tick", help="launchd 用：距上次全量超过 3 小时则全量，否则轻量")
    p.add_argument("--quiet", action="store_true", help="只输出错误")
    p.add_argument("--notify", action="store_true", help="有变动时发 macOS 通知")
    p.set_defaults(func=cmd_tick)

    p = sub.add_parser("report", help="终端查看最新摘要")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("links", help="列出厂商官网链接")
    p.set_defaults(func=cmd_links)

    p = sub.add_parser("history", help="查看某个模型的价格历史")
    p.add_argument("keyword")
    p.add_argument("--days", type=int, default=90)
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("open", help="打开仪表盘")
    p.set_defaults(func=cmd_open)

    p = sub.add_parser("install", help="安装每日 launchd 定时任务")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("status", help="查看运行状态")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("install-cli", help="把 aimon 命令链接到 /opt/homebrew/bin")
    p.set_defaults(func=cmd_install_cli)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    try:
        return args.func(args)
    except KeyboardInterrupt:
        err("已取消。")
        return 130
    except Exception as exc:
        err(f"❌ {exc}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
