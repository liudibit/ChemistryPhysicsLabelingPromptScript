#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""相似题打标流水线的公共模块（阶段一 / 阶段二 共用，不含大模型逻辑）。

职责边界：
  本模块只负责「题目」本身——题库接口调用、题库 HTML 清洗、字段归一化、
  题目 ID / 题目文件的读写、以及两个阶段之间的作业编译（jobs）。
  调用门神模型、标签校验、Excel / 复核页面输出一律不在这里。

因此：
  - fetch_questions.py  （阶段一：题目 ID -> 题目文件）只依赖本模块；
  - generate_math_tags.py（阶段二：题目文件 -> 标签）依赖本模块 + 打标逻辑。

依赖：requests（可选：beautifulsoup4，未安装时退化为正则清洗）
"""

from __future__ import annotations

import json
import os
import re
import time
from html import unescape
from pathlib import Path
from typing import Any, Iterable

import requests

try:
    from bs4 import BeautifulSoup
except ImportError:  # optional；未安装时退化为正则清洗
    BeautifulSoup = None


def load_env_file(path: Path) -> None:
    """Load simple KEY=VALUE entries without requiring python-dotenv.

    Existing process environment variables win over values in .env.
    """
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


def env_or_arg(value: str | None, env_name: str, required: bool = True) -> str:
    result = value or os.getenv(env_name, "")
    if required and not result:
        raise RuntimeError(f"缺少配置：命令行参数或环境变量 {env_name}")
    return result


def convert_sup_sub_to_latex(text: str) -> str:
    text = re.sub(r"</sup>\s*<sup[^>]*>", "", text)
    text = re.sub(r"</sub>\s*<sub[^>]*>", "", text)
    text = re.sub(r"<sup[^>]*>\s*(.*?)\s*</sup>", r"^{\1}", text, flags=re.I | re.S)
    text = re.sub(r"<sub[^>]*>\s*(.*?)\s*</sub>", r"_{\1}", text, flags=re.I | re.S)
    return text


def normalize_text_and_extract_imgs(value: Any) -> tuple[str, list[str]]:
    """清洗题库 HTML，同时用 <image> 占位符保持图片顺序。"""
    if value is None:
        return "", []
    if not isinstance(value, str):
        value = str(value)
    if not value.strip():
        return "", []

    urls: list[str] = []
    text = convert_sup_sub_to_latex(value)
    if BeautifulSoup is not None:
        soup = BeautifulSoup(text, "html.parser")
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src") or img.get("data-original")
            if isinstance(src, str) and src.strip():
                urls.append(src.strip())
                img.replace_with("<image>")
            else:
                img.decompose()
        for table in soup.find_all("table"):
            rows = []
            for tr in table.find_all("tr"):
                cells = [re.sub(r"\s+", " ", c.get_text(" ", strip=True)) for c in tr.find_all(["th", "td"])]
                if cells:
                    rows.append(" | ".join(cells))
            table.replace_with("\n" + "\n".join(rows) + "\n")
        text = soup.get_text(separator=" ")
    else:
        def replace_img(match: re.Match[str]) -> str:
            src = re.search(r"(?:src|data-src|data-original)\s*=\s*[\\\"']([^\\\"']+)", match.group(0), flags=re.I)
            if src:
                urls.append(src.group(1))
                return "<image>"
            return ""
        text = re.sub(r"<img\b[^>]*>", replace_img, text, flags=re.I)
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
        text = re.sub(r"<[^>]+>", "", text)

    text = unescape(text)
    text = text.replace("$(\\qquad)$", "(   )")
    text = re.sub(r"<span[^>]*class=['\"][^'\"]*underline fillblank[^'\"]*['\"][^>]*>.*?</span>", "______", text, flags=re.I | re.S)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.replace("<image>", "<image>")
    return text, list(dict.fromkeys(urls))


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    return normalize_text_and_extract_imgs(str(value))[0]


def first_value(obj: dict[str, Any], names: Iterable[str]) -> Any:
    for name in names:
        if name in obj and obj[name] not in (None, "", []):
            return obj[name]
    return ""


def stringify_field(value: Any) -> str:
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                title = first_value(item, ["title", "label", "key", "option"])
                body = first_value(item, ["htmlCode", "content", "text", "value", "body"])
                text = normalize_text_and_extract_imgs(body)[0]
                parts.append(f"{title}. {text}" if title and text else clean_text(title or body))
            else:
                parts.append(clean_text(item))
        return "\n".join(p for p in parts if p)
    if isinstance(value, dict):
        return clean_text(json.dumps(value, ensure_ascii=False))
    return clean_text(value)


def collect_urls(value: Any, key_hint: str = "") -> list[str]:
    urls: list[str] = []
    if isinstance(value, str):
        if re.match(r"https?://", value) and (
            "image" in key_hint.lower()
            or "img" in key_hint.lower()
            or re.search(r"\.(png|jpg|jpeg|webp|gif)(\?|$)", value, re.I)
        ):
            urls.append(value)
    elif isinstance(value, dict):
        for k, v in value.items():
            urls.extend(collect_urls(v, k))
    elif isinstance(value, list):
        for item in value:
            urls.extend(collect_urls(item, key_hint))
    return urls


def collect_field_urls(value: Any, field_name: str) -> list[str]:
    """Collect URLs belonging to a known question field.

    The field name is treated as an image context so URLs without a file
    extension, such as CDN URLs ending in a UUID, are retained too.
    """
    return collect_urls(value, f"{field_name}Image")


def question_id(obj: dict[str, Any]) -> str:
    value = first_value(obj, ["questionId", "question_id", "id", "sid", "subQuestionId"])
    return str(value) if value is not None else ""


def find_question_objects(value: Any) -> list[dict[str, Any]]:
    """兼容常见接口包裹结构，提取含题目 ID 的字典。"""
    found: list[dict[str, Any]] = []
    seen: set[int] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            qid = question_id(node)
            if qid and any(k in node for k in ("stem", "question", "analysis", "answer", "options", "content", "questionInfo")):
                marker = id(node)
                if marker not in seen:
                    found.append(node)
                    seen.add(marker)
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return found


def extract_api_questions(payload: Any, requested_id: str) -> list[dict[str, Any]]:
    """按题库接口的默认结构 data[].smallQuestions[] 展开题目。

    题库默认结构来自老师提供的标注平台：
    data[] -> questionId / parentId / questionInfo / smallQuestions[]。
    同时保留递归兜底，兼容接口外层增加 result/data 包装的情况。
    """
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict) and isinstance(payload["data"].get("data"), list):
        # 列表/检索接口的分页结构：data -> {pageNo, ..., data: [...]}
        roots = payload["data"]["data"]
    elif isinstance(payload, dict) and isinstance(payload.get("data"), (list, dict)):
        roots = payload["data"] if isinstance(payload["data"], list) else [payload["data"]]
    elif isinstance(payload, dict) and isinstance(payload.get("result"), (list, dict)):
        roots = payload["result"] if isinstance(payload["result"], list) else [payload["result"]]
    else:
        roots = [payload]

    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def expand(node: Any, inherited_parent: str = "") -> None:
        if not isinstance(node, dict):
            return
        parent = str(first_value(node, ["parentId", "parent_id", "bigQuestionId"]) or inherited_parent or requested_id)
        qid = question_id(node)
        info = node.get("questionInfo")
        has_content = isinstance(info, dict) or any(
            key in node for key in ("stem", "questionStem", "questionContent", "answer", "analysis", "options", "stemImageUrl")
        )
        if qid and has_content:
            marker = (parent, qid)
            if marker not in seen:
                result.append(node)
                seen.add(marker)
        for key in ("smallQuestions", "subQuestions", "children", "sons"):
            children = node.get(key)
            if isinstance(children, list):
                for child in children:
                    expand(child, parent)

    for root in roots:
        expand(root)
    if result:
        return result
    return find_question_objects(payload)


def normalize_question(raw: dict[str, Any], index: int) -> dict[str, Any]:
    info = raw.get("questionInfo") if isinstance(raw.get("questionInfo"), dict) else raw
    qid = question_id(raw) or question_id(info) or f"unknown-{index}"
    stem = first_value(info, ["stem", "questionStem", "stemContent", "questionContent", "question", "content"])
    options = first_value(info, ["options", "option", "choices"])
    answer = first_value(info, ["answer", "answerContent", "answers"])
    analysis = first_value(info, ["analysis", "解析", "solution", "explanation"])
    # 保留题库的知识点与解题思路，供学科打标提示词作为判定依据；字段名兼容不同接口版本。
    knowledge = first_value(info, ["knwInfos", "knowledge", "knowledgePoints", "knowledgePoint", "knowledgePointName", "知识点"])
    if not knowledge:
        knowledge = first_value(raw, ["knwInfos", "knowledge", "knowledgePoints", "knowledgePoint", "knowledgePointName", "知识点"])
    solution = first_value(info, ["solutionStep", "solution", "solvingIdea", "解题思路"])
    if not solution:
        solution = first_value(raw, ["solutionStep", "solution", "solvingIdea", "解题思路"])
    stem_text, stem_images = normalize_text_and_extract_imgs(stem)
    if isinstance(options, list):
        option_parts, option_images = [], []
        for item in options:
            if isinstance(item, dict):
                title = first_value(item, ["title", "label", "key", "option"])
                body = first_value(item, ["htmlCode", "content", "text", "value", "body"])
                body_text, body_images = normalize_text_and_extract_imgs(body)
                option_parts.append(f"{title}. {body_text}" if title and body_text else (body_text or clean_text(title)))
                option_images.extend(body_images)
            else:
                item_text, item_images = normalize_text_and_extract_imgs(item)
                option_parts.append(item_text)
                option_images.extend(item_images)
        options_text, options_images = "\n".join(x for x in option_parts if x), option_images
    else:
        options_text, options_images = normalize_text_and_extract_imgs(options)
    answer_text, answer_images = normalize_text_and_extract_imgs(answer)
    analysis_text, analysis_images = normalize_text_and_extract_imgs(analysis)
    field_images = {
        "stem": stem_images,
        "options": options_images,
        "analysis": analysis_images,
        "answer": answer_images,
    }
    # Some question responses store image URLs in separate fields instead of
    # embedding them in HTML. Keep those URLs in their corresponding Excel
    # column and preserve the API's field order.
    for field, names in {
        "stem": ["stemImage", "stemImageUrl", "questionImage", "questionImageUrl", "questionImgs", "stemImgs"],
        "options": ["optionImages", "optionImageUrls", "optionsImage", "optionsImages"],
        "analysis": ["analysisImage", "analysisImageUrl", "solutionImage", "solutionImageUrl", "analysisImgs"],
        "answer": ["answerImage", "answerImageUrl", "answerImgs"],
    }.items():
        for source in (info, raw):
            for name in names:
                if name in source:
                    field_images[field].extend(collect_field_urls(source[name], field))
        field_images[field] = list(dict.fromkeys(field_images[field]))
    images = [u for field in ("stem", "options", "analysis", "answer") for u in field_images[field]]
    images += collect_urls(raw)
    images = list(dict.fromkeys(images))
    return {
        "questionId": qid,
        "businessTypeName": clean_text(first_value(raw, ["businessTypeName", "questionTypeName"])),
        "structureTypeName": clean_text(first_value(raw, ["structureTypeName", "questionType"])),
        "stem": stem_text,
        "options": options_text,
        "answer": answer_text,
        "analysis": analysis_text,
        "knowledge": knowledge,
        "solution": clean_text(solution),
        "fieldImages": field_images,
        "imageUrls": images,
        "raw": raw,
    }


def load_ids(path: Path) -> list[str]:
    """读取题目 ID Excel（首列，跳过表头）。"""
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    result = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if row and row[0] is not None and str(row[0]).strip():
            result.append(str(row[0]).strip())
    if len(result) != len(set(result)):
        raise RuntimeError("题目 ID 文件存在重复大题 ID")
    return result


def unwrap_question_entries(value: Any) -> list[dict[str, Any]]:
    """从常见题库响应/题目文件中提取题目对象列表。

    兼容：data 为列表、data.data 分页列表、questions/responses 列表、
    以及 {题目ID: 题目对象} 的映射结构。
    """
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in ("questions", "data", "result", "list", "items", "records", "responses"):
            if key in value:
                found = unwrap_question_entries(value[key])
                if found:
                    return found
        if question_id(value):
            return [value]
        found = [item for item in value.values() if isinstance(item, dict) and question_id(item)]
        if found:
            return found
    return []


def load_question_file(path: Path) -> list[dict[str, Any]]:
    """读取本地题目文件（JSON 或包含 JSON 的 txt），返回原始题目对象列表。

    题目文件可以是：题库接口原始响应、dict 包装的 {questions: [...]}、
    或 {题目ID: 题目对象} 映射——见 unwrap_question_entries。
    """
    if not path.exists():
        raise RuntimeError(f"题目文件不存在：{path}")
    text = path.read_text(encoding="utf-8-sig").strip()
    if not text:
        raise RuntimeError(f"题目文件为空：{path}")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"题目文件不是合法 JSON：{path}（{exc}）") from exc
    entries = unwrap_question_entries(data)
    if not entries:
        raise RuntimeError(f"题目文件中没有识别到题目对象：{path}")
    return entries


def load_question_ids(path: Path) -> list[str]:
    """读取题目 ID：支持 xlsx 首列、txt 每行一个 ID、或含题目对象的 JSON/txt。"""
    suffix = path.suffix.lower()
    if suffix in {".json", ".txt"}:
        text = path.read_text(encoding="utf-8-sig").strip()
        if text[:1] in {"{", "["}:
            entries = unwrap_question_entries(json.loads(text))
            ids = [qid for qid in (question_id(entry) for entry in entries) if qid]
            ids = list(dict.fromkeys(ids))
            if not ids:
                raise RuntimeError(f"未能从文件中提取题目 ID：{path}")
            return ids
        ids = [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]
        if not ids:
            raise RuntimeError(f"题目 ID 文件为空：{path}")
        return ids
    if suffix in {".xlsx", ".xlsm"}:
        return load_ids(path)
    raise RuntimeError(f"不支持的题目 ID 文件类型：{path}")


def build_jobs_from_question_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把题目对象（题库响应或本地题目文件的条目）编译成打标作业列表。

    这是两个阶段之间的接口：每个作业 = {qid, questions(已归一化), payload}。
    阶段一「取题」负责产出可被 load_question_file 读取的文件；
    阶段二「打标」只消费该结构，不再关心题目从哪来。
    """
    jobs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries, 1):
        qid = question_id(entry) or f"unknown-{index}"
        if qid in seen:
            continue
        seen.add(qid)
        raws = extract_api_questions(entry, qid) or [entry]
        questions = [normalize_question(raw, i) for i, raw in enumerate(raws, 1)]
        jobs.append({"qid": qid, "questions": questions, "payload": entry})
    return jobs


def fetch_questions(
    session: requests.Session,
    url: str,
    headers: dict[str, str],
    qid: str,
    timeout: int,
    retries: int,
    timestamp: str = "",
) -> tuple[Any, list[dict[str, Any]], list[str]]:
    """阶段一核心：按大题 ID 调题库接口，返回 (原始响应, 归一化小题列表, 错误列表)。"""
    errors = []
    last_payload: Any = None
    for attempt in range(retries + 1):
        try:
            request_headers = dict(headers)
            request_headers["timeStamp"] = timestamp or str(int(time.time() * 1000))
            response = session.post(url, headers=request_headers, json={"questionList": [{"questionId": qid}]}, timeout=timeout)
            response.raise_for_status()
            payload = response.json()
            last_payload = payload
            if isinstance(payload, dict):
                api_code = payload.get("code")
                api_success = payload.get("success")
                if api_success is False or api_code not in (None, 0, "0", 200, "200"):
                    message = payload.get("message") or "题库接口返回业务错误"
                    raise RuntimeError(f"题库接口业务错误 code={api_code}: {message}")
            questions = [normalize_question(x, i) for i, x in enumerate(extract_api_questions(payload, qid), 1)]
            if not questions:
                raise RuntimeError("接口响应中没有识别到题目对象")
            return payload, questions, errors
        except Exception as exc:  # noqa: BLE001
            errors.append(f"attempt={attempt + 1}: {type(exc).__name__}: {exc}")
            if attempt < retries:
                time.sleep(min(2 ** attempt, 8))
    return last_payload, [], errors


def write_question_file(
    path: Path,
    collected: list[dict[str, Any]],
    requested_ids: list[str],
    errors: list[str] | None = None,
    tiku_url: str = "",
) -> Path:
    """阶段一产物（题目文件）的统一写出格式，供阶段二 load_question_file 读取。"""
    from datetime import datetime

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "generatedAt": datetime.now().isoformat(timespec="seconds"),
                "tikuUrl": tiku_url,
                "requestedIds": requested_ids,
                "downloadedCount": len(collected),
                "errors": list(errors or []),
                "questions": list(collected),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return path
