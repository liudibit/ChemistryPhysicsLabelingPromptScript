# -*- coding: utf-8 -*-
"""
小学数学题库统一处理脚本。

合并原三个脚本的功能：
1. 将 API 导出中的 smallQuestions 扁平化；
2. 清洗题干、选项、解析、答案中的 HTML；
3. 按 parent_id 重建父子题结构，并按原逻辑输出；
4. 合并 API 的 knwIds/selfKnwIds，或读取 SQL 的 origin_knw_id；
5. 原样保留题库原始标签及 knwInfos 名称，目录过滤结果单独写入 normalized 字段；
6. 为 API 题组的父题和全部小题复制同一份最外层 global_outer_urls。

脚本不使用 argparse。修改文件底部 CONFIG 后直接运行：
    /Users/yexinyong/miniconda3/bin/python \
        /Users/yexinyong/0_work_data/llm/project_files/question_bank_process/2026_11_06_question_bank_process_unified.py
"""

from __future__ import annotations

import os
import re
import time
from multiprocessing import Pool
from typing import Any, Iterable, Iterator, Optional

import orjson
from bs4 import BeautifulSoup
from tqdm import tqdm


OUTER_URL_FIELDS = ("stemImageUrl", "analysisImageUrl", "answerImageUrl")
MOVED_TO_QUESTION_INFO_FIELDS = (
    "question_index",
    "business_type",
    "structure_type",
    "difficulty",
    "create_time",
)
# 这些字段是递归子题容器。缓存原始接口对象时移除，避免父题把整棵子树重复存一遍。
SOURCE_TREE_FIELDS = ("smallQuestions", "subQuestions", "children", "sons")
# 题库有时不把图片写在 HTML 里，而是放在独立字段中；这些字段也必须保留。
QUESTION_IMAGE_FIELD_NAMES = {
    "stem": ("stemImage", "stemImageUrl", "questionImage", "questionImageUrl", "questionImgs", "stemImgs"),
    "options": ("optionImages", "optionImageUrls", "optionsImage", "optionsImages"),
    "analysis": ("analysisImage", "analysisImageUrl", "solutionImage", "solutionImageUrl", "analysisImgs"),
    "answer": ("answerImage", "answerImageUrl", "answerImgs"),
}
IMG_TOKEN = "[[__IMG_TOKEN__]]"

_WORKER_LABEL_WHITELIST: set[str] = set()
_WORKER_FILTER_LABELS = True
_WORKER_INPUT_TYPE = ""
_WORKER_ORIGIN_LABEL_NAME_MAP: dict[str, str] = {}
_WORKER_ORIGIN_LABEL_ID_WHITELIST: set[str] = set()


# ============================================================================
# HTML / 文本清洗
# ============================================================================

def convert_sup_sub_to_latex_robust(text: str) -> str:
    """把 HTML 上下角标转换为 LaTeX 上下标，并合并无意义的连续标签。"""
    while re.search(r"</sup>\s*<sup[^>]*>", text):
        text = re.sub(r"</sup>\s*<sup[^>]*>", "", text)

    while re.search(r"</sub>\s*<sub[^>]*>", text):
        text = re.sub(r"</sub>\s*<sub[^>]*>", "", text)

    text = re.sub(r"<sup[^>]*>\s*(.*?)\s*</sup>", r"^{\1}", text)
    text = re.sub(r"<sub[^>]*>\s*(.*?)\s*</sub>", r"_{\1}", text)
    return text


def normalize_text_and_extract_imgs(text: Any) -> tuple[str, list[str]]:
    """清洗一段 HTML 文本，并返回图片占位符和图片 URL。"""
    if not text or not isinstance(text, str):
        return "", []

    extracted_urls: list[str] = []

    def extract_and_replace(match: re.Match[str]) -> str:
        full_tag = match.group(0)
        src_match = re.search(r"src=['\"](.*?)['\"]", full_tag)
        if src_match:
            url = src_match.group(1).strip()
            if url:
                extracted_urls.append(url)
            return IMG_TOKEN
        return ""

    text = re.sub(r"<img[^>]*>", extract_and_replace, text)
    text = convert_sup_sub_to_latex_robust(text)
    text = text.replace("$(\\qquad)$", "(   )")
    text = re.sub(
        r"<span[^>]*class=['\"][^'\"]*underline fillblank[^'\"]*['\"][^>]*>.*?</span>",
        "______",
        text,
    )

    soup = BeautifulSoup(text, "html.parser")

    # 先把表格转换成一格一行的 Markdown 表格，再统一提取纯文本。
    for table in reversed(soup.find_all("table")):
        try:
            md_table: list[str] = []
            rows = table.find_all("tr")
            if not rows:
                continue

            has_added_header_line = False
            for row in rows:
                cols = row.find_all(["td", "th"])
                if not cols:
                    continue

                col_texts: list[str] = []
                for col in cols:
                    cell_text = re.sub(
                        r"\s+", " ", col.get_text(separator=" ", strip=True)
                    )
                    cell_text = cell_text.replace("|", "\\|")
                    col_texts.append(cell_text)

                md_table.append("| " + " | ".join(col_texts) + " |")
                if not has_added_header_line:
                    md_table.append("|" + "|".join(["---"] * len(cols)) + "|")
                    has_added_header_line = True

            if md_table:
                table.replace_with(" \n" + "\n".join(md_table) + "\n ")
        except Exception:
            # 单个异常表格不影响整题，继续由 BeautifulSoup 提取其余内容。
            continue

    cleaned_text = soup.get_text(separator=" ")
    cleaned_text = re.sub(r"(?:\$\s*)+", " ", cleaned_text)
    cleaned_text = re.sub(
        r"\s*(?:\\!\s*)*%\s*(?:\\!\s*)*(?:\\text{\s*})?", r"\%", cleaned_text
    )

    # 全角转半角，保留中文标点。
    full_width_punctuations_to_keep = {"，", "。", "；", "：", "？", "！", "．"}
    result: list[str] = []
    for char in cleaned_text:
        code = ord(char)
        if 0xFF01 <= code <= 0xFF5E and char not in full_width_punctuations_to_keep:
            result.append(chr(code - 0xFEE0))
        else:
            result.append(char)
    normalized_str = "".join(result)

    replacements = {
        "﹣": "-",
        "−": "-",
        "～": "~",
        "≥": ">=",
        "≤": "<=",
        "≠": "!=",
        "\u3000": " ",
        "\u00A0": " ",
        "…": "...",
        "\r": "",
    }
    for old, new in replacements.items():
        normalized_str = normalized_str.replace(old, new)

    normalized_str = re.sub(r"[^\S\n]+", " ", normalized_str).strip()
    normalized_str = normalized_str.replace(IMG_TOKEN, "<image>")
    return normalized_str, extracted_urls


def format_options_and_extract_imgs(options: Any) -> tuple[str, list[str]]:
    """清洗选项，返回格式化字符串和选项图片 URL。"""
    if not options or not isinstance(options, list):
        return "", []

    formatted_parts: list[str] = []
    all_options_urls: list[str] = []

    for option_data in options:
        if not isinstance(option_data, dict):
            continue

        label = option_data.get("title")
        content_html = option_data.get("htmlCode")
        if label and content_html:
            cleaned_content, urls = normalize_text_and_extract_imgs(content_html)
            formatted_parts.append(f"{str(label).strip()}. {cleaned_content}")
            all_options_urls.extend(urls)

    if not formatted_parts:
        return "", []
    return "\n".join(formatted_parts), all_options_urls


def format_answer(answer: Any) -> tuple[str, list[str]]:
    """清洗答案；支持字符串、答案组列表和单答案组字典。"""
    if not answer:
        return "", []

    all_urls: list[str] = []

    if isinstance(answer, str):
        cleaned_answer, ans_urls = normalize_text_and_extract_imgs(answer)
        all_urls.extend(ans_urls)
        return (cleaned_answer.rstrip(".;,") if cleaned_answer else ""), all_urls

    if isinstance(answer, dict):
        answer = [answer]

    if isinstance(answer, list):
        text_parts: list[str] = []
        for ans_group in answer:
            if isinstance(ans_group, dict):
                raw_answers_list = ans_group.get("answers", [])
            elif isinstance(ans_group, list):
                raw_answers_list = ans_group
            else:
                continue

            if not isinstance(raw_answers_list, list):
                continue

            group_texts: list[str] = []
            for one_answer in raw_answers_list:
                cleaned_text, urls = normalize_text_and_extract_imgs(one_answer)
                if cleaned_text:
                    group_texts.append(cleaned_text)
                all_urls.extend(urls)

            ans_str = "、".join(group_texts)
            if ans_str:
                text_parts.append(ans_str)

        return "； ".join(text_parts), all_urls

    return "", []


def post_process_text(text: str) -> str:
    if not text:
        return ""
    text = text.replace("()", "(   )")
    text = re.sub(r"\s+(?=[。，！？；：”’）】])", "", text)
    return text.strip()


def parse_question_info(value: Any) -> dict[str, Any]:
    """兼容 dict 和 JSON 字符串形式的 question_info。"""
    if value is None or value == "":
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, (str, bytes)):
        parsed = orjson.loads(value)
        if not isinstance(parsed, dict):
            raise ValueError("question_info 不是 JSON 对象")
        return parsed
    raise ValueError(f"question_info 类型不支持: {type(value).__name__}")


def _collect_image_urls(value: Any) -> list[str]:
    """从字符串、列表或常见图片对象中提取 URL，并保持原顺序去重。"""
    urls: list[str] = []

    def walk(node: Any) -> None:
        if node is None:
            return
        if isinstance(node, str):
            value = node.strip()
            if value:
                urls.append(value)
            return
        if isinstance(node, (list, tuple, set)):
            for item in node:
                walk(item)
            return
        if isinstance(node, dict):
            for key in ("url", "src", "imageUrl", "imageURL", "path"):
                if key in node:
                    walk(node[key])
            for key in ("urls", "images", "imageUrls", "imageURLs"):
                if key in node:
                    walk(node[key])

    walk(value)
    return list(dict.fromkeys(urls))


def _cache_source_item(item: dict[str, Any]) -> dict[str, Any]:
    """保存接口原始题目对象，但移除递归子题容器，避免父子题重复膨胀。"""
    return {key: value for key, value in item.items() if key not in SOURCE_TREE_FIELDS}


def clean_question_info(
    question_info: dict[str, Any], raw_item: Optional[dict[str, Any]] = None
) -> dict[str, Any]:
    """清洗正文，同时保留知识点、解题思路和接口独立图片字段。"""
    raw_stem = question_info.get("stem", "")
    raw_options = question_info.get("options", [])

    cleaned_stem, stem_urls = normalize_text_and_extract_imgs(raw_stem)
    cleaned_stem = post_process_text(cleaned_stem)

    formatted_options, options_urls = format_options_and_extract_imgs(raw_options)

    cleaned_analysis, analysis_urls = normalize_text_and_extract_imgs(
        question_info.get("analysis")
    )
    cleaned_analysis = post_process_text(cleaned_analysis)

    formatted_answer, answer_urls = format_answer(question_info.get("answer"))
    raw_solution = _first_value(
        question_info,
        ("solutionStep", "solution", "solvingIdea", "解题思路"),
        default="",
    )
    cleaned_solution, solution_urls = normalize_text_and_extract_imgs(raw_solution)

    images = {
        "stem": stem_urls,
        "options": options_urls,
        "analysis": analysis_urls,
        "answer": answer_urls,
        "solution": solution_urls,
    }
    # 兼容接口把图片放在独立字段而不是 HTML 中的情况。
    for field, names in QUESTION_IMAGE_FIELD_NAMES.items():
        for source in (question_info, raw_item or {}):
            for name in names:
                if name in source:
                    images[field].extend(_collect_image_urls(source[name]))
        images[field] = list(dict.fromkeys(images[field]))
    total_img_count = sum(len(urls) for urls in images.values())

    knowledge = _first_value(
        question_info,
        ("knwInfos", "knowledge", "knowledgePoints", "knowledgePoint", "knowledgePointName", "知识点"),
        default=[],
    )
    if not knowledge and raw_item:
        knowledge = _first_value(
            raw_item,
            ("knwInfos", "knowledge", "knowledgePoints", "knowledgePoint", "knowledgePointName", "知识点"),
            default=[],
        )

    return {
        "stem": cleaned_stem,
        "options": formatted_options,
        "analysis": cleaned_analysis,
        "answer": formatted_answer,
        "solution": post_process_text(cleaned_solution),
        "knowledge": knowledge,
        "images": images,
        "has_img": total_img_count > 0,
    }


# ============================================================================
# 字段、标签与题组
# ============================================================================

def _new_stats() -> dict[str, int]:
    return {
        "input_records": 0,
        "valid_input_records": 0,
        "json_decode_errors": 0,
        "processing_errors": 0,
        "questions_flattened": 0,
        "groups": 0,
        "group_records_seen": 0,
        "emitted_records": 0,
        "composite_groups": 0,
        "non_composite_groups": 0,
        "incomplete_groups": 0,
        "child_questions": 0,
        "duplicate_question_ids": 0,
        "groups_with_embedded_images": 0,
        "labels_refs_read": 0,
        "labels_unique_before_filter": 0,
        "labels_kept": 0,
        "labels_dropped": 0,
        "records_with_nonempty_labels": 0,
        "records_with_empty_labels": 0,
        "records_with_nonempty_normalized_labels": 0,
    }


def _merge_stats(target: dict[str, int], source: dict[str, int]) -> None:
    for key, value in source.items():
        target[key] = target.get(key, 0) + int(value)


def _raw_label_values(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _stable_unique_label_ids(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value is None:
            continue
        label_id = str(value).strip()
        if not label_id or label_id in seen:
            continue
        seen.add(label_id)
        result.append(label_id)
    return result


def _filter_label_ids(values: Iterable[Any]) -> tuple[list[str], dict[str, int]]:
    raw_values = list(values)
    unique_ids = _stable_unique_label_ids(raw_values)
    if _WORKER_FILTER_LABELS:
        kept = [label_id for label_id in unique_ids if label_id in _WORKER_LABEL_WHITELIST]
    else:
        kept = unique_ids

    stats = {
        "labels_refs_read": len(raw_values),
        "labels_unique_before_filter": len(unique_ids),
        "labels_kept": len(kept),
        "labels_dropped": len(unique_ids) - len(kept),
        "records_with_nonempty_labels": int(bool(kept)),
        "records_with_empty_labels": int(not kept),
    }
    return kept, stats


def _first_value(data: dict[str, Any], keys: tuple[str, ...], default: Any = "") -> Any:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    for key in keys:
        if key in data:
            return data[key]
    return default


def _extract_api_label_values(item: dict[str, Any]) -> list[Any]:
    has_api_label_fields = "knwIds" in item or "selfKnwIds" in item
    if has_api_label_fields:
        return _raw_label_values(item.get("knwIds")) + _raw_label_values(
            item.get("selfKnwIds")
        )

    # 兼容已经是 step_1 格式的数据；原规则仍以 origin_knw_id 为准。
    question_info = parse_question_info(_first_value(item, ("questionInfo", "question_info"), {}))
    if "origin_knw_id" in item:
        return _raw_label_values(item.get("origin_knw_id"))
    return _raw_label_values(question_info.get("origin_knw_id"))


def _extract_sql_label_values(
    item: dict[str, Any], question_info: dict[str, Any]
) -> list[Any]:
    if "origin_knw_id" in item:
        return _raw_label_values(item.get("origin_knw_id"))
    if "origin_knw_id" in question_info:
        return _raw_label_values(question_info.get("origin_knw_id"))

    # 部分 SQL 导出也可能保留 API 标签字段，仅在原字段完全不存在时兜底。
    return _raw_label_values(item.get("knwIds")) + _raw_label_values(
        item.get("selfKnwIds")
    )


def _extract_label_names(
    item: dict[str, Any], question_info: dict[str, Any]
) -> dict[str, str]:
    """读取 API knwInfos 中的原始标签名称，按 knwId 建映射。"""
    result: dict[str, str] = {}
    for source in (item.get("knwInfos"), question_info.get("knwInfos")):
        if not isinstance(source, list):
            continue
        for entry in source:
            if not isinstance(entry, dict):
                continue
            label_id = str(entry.get("knwId") or "").strip()
            label_name = str(entry.get("knwName") or "").strip()
            if label_id and label_name and label_id not in result:
                result[label_id] = label_name
    return result


def _label_names_for_ids(
    label_ids: list[str], label_names_by_id: dict[str, str]
) -> list[str]:
    return [
        _WORKER_ORIGIN_LABEL_NAME_MAP.get(label_id)
        or label_names_by_id.get(label_id, "")
        for label_id in label_ids
    ]


def _clean_single_record(
    item: dict[str, Any],
    input_type: str,
    parent_id_override: Optional[str],
    source_order: int,
) -> tuple[dict[str, Any], dict[str, int]]:
    question_id = str(
        _first_value(item, ("questionId", "question_id"), default="")
    ).strip()
    if not question_id:
        raise ValueError("缺少 questionId/question_id")

    parent_id = str(
        _first_value(item, ("parentId", "parent_id"), default="")
    ).strip()
    if parent_id_override is not None:
        parent_id = str(parent_id_override)
    if not parent_id:
        parent_id = question_id

    question_info_raw = _first_value(
        item, ("questionInfo", "question_info"), default={}
    )
    question_info = parse_question_info(question_info_raw)

    if input_type == "api":
        label_values = _extract_api_label_values(item)
    else:
        label_values = _extract_sql_label_values(item, question_info)
    raw_labels = _stable_unique_label_ids(label_values)
    origin_labels = (
        [
            label_id
            for label_id in raw_labels
            if label_id in _WORKER_ORIGIN_LABEL_ID_WHITELIST
        ]
        if _WORKER_ORIGIN_LABEL_ID_WHITELIST
        else raw_labels
    )
    labels, label_stats = _filter_label_ids(origin_labels)
    label_names_by_id = _extract_label_names(item, question_info)
    raw_label_names = _label_names_for_ids(origin_labels, label_names_by_id)
    normalized_label_names = _label_names_for_ids(labels, label_names_by_id)
    label_stats["records_with_nonempty_labels"] = int(bool(raw_labels))
    label_stats["records_with_empty_labels"] = int(not raw_labels)
    label_stats["records_with_nonempty_normalized_labels"] = int(bool(labels))

    cleaned_info = clean_question_info(question_info, item)
    business_type = _first_value(
        item, ("businessType", "business_type"), default=""
    )
    structure_type = _first_value(
        item, ("structureType", "structure_type"), default=""
    )
    business_type_name = _first_value(
        item, ("businessTypeName", "business_type_name"), default=""
    )
    structure_type_name = _first_value(
        item, ("structureTypeName", "structure_type_name"), default=""
    )
    subject = _first_value(
        item, ("subjectName", "subject_name", "subject"), default=""
    )
    difficulty = _first_value(item, ("difficulty",), default="")
    question_index = _first_value(item, ("questionIndex", "question_index"), default=0)

    record: dict[str, Any] = {
        "question_id": question_id,
        "parent_id": parent_id,
        "question_index": question_index,
        "subject": subject,
        "business_type": business_type,
        "business_type_name": business_type_name,
        "structure_type": structure_type,
        "structure_type_name": structure_type_name,
        "difficulty": difficulty,
        "question_info_cleaned": cleaned_info,
        "origin_knw_id": origin_labels,
        "origin_knw_name": raw_label_names,
        "normalized_origin_knw_id": labels,
        "normalized_origin_knw_name": normalized_label_names,
        "_source_order": source_order,
    }

    create_time = _first_value(item, ("create_time", "createTime"), default=None)
    if create_time is not None:
        record["create_time"] = create_time
    if input_type == "api":
        # 忠实缓存接口原文，供本地打标时复用统一的 normalize_question 逻辑。
        record["_source_item"] = _cache_source_item(item)

    return record, label_stats


def _question_index_for_sort(record: dict[str, Any]) -> tuple[int, int]:
    value = record.get("question_index")
    try:
        index = int(value)
    except (TypeError, ValueError):
        try:
            index = int(float(value))
        except (TypeError, ValueError):
            index = 999999
    return index, int(record.get("_source_order", 0))


def _group_outer_urls(root_item: dict[str, Any]) -> dict[str, str]:
    """只读取 API 最外层父题自身的三个全局 URL。"""
    return {
        field: str(root_item.get(field) or "").strip()
        for field in OUTER_URL_FIELDS
    }


def _build_group_outputs(
    parent_id: str,
    records: list[dict[str, Any]],
    global_outer_urls: dict[str, str],
) -> tuple[list[dict[str, Any]], dict[str, int], list[dict[str, Any]]]:
    """把一个 parent_id 下的全部清洗记录重组成原 step_3 的父子题结构。"""
    parent_candidates = [
        record for record in records if record.get("question_id") == parent_id
    ]
    errors: list[dict[str, Any]] = []

    if not parent_candidates:
        return (
            [],
            {
                "groups": 1,
                "group_records_seen": len(records),
                "emitted_records": 0,
                "child_questions": max(len(records), 0),
                "incomplete_groups": 1,
                "duplicate_question_ids": 0,
                "composite_groups": 0,
                "non_composite_groups": 0,
                "groups_with_embedded_images": 0,
            },
            [
                {
                    "stage": "missing_parent",
                    "parent_id": parent_id,
                    "record_count": len(records),
                    "message": "题组缺少 question_id == parent_id 的父题，已跳过",
                }
            ],
        )

    parent_record = parent_candidates[0]
    children = [record for record in records if record is not parent_record]
    children.sort(key=_question_index_for_sort)

    ordered_records = [parent_record] + children
    questions_map: dict[str, dict[str, Any]] = {}
    origin_knw_id_by_qid: dict[str, list[str]] = {}
    origin_knw_name_by_qid: dict[str, list[str]] = {}
    normalized_origin_knw_id_by_qid: dict[str, list[str]] = {}
    normalized_origin_knw_name_by_qid: dict[str, list[str]] = {}
    duplicate_question_ids = 0

    for record in ordered_records:
        question_id = str(record.get("question_id") or "").strip()
        if not question_id:
            continue
        if question_id in questions_map:
            duplicate_question_ids += 1
            continue

        info = dict(record.get("question_info_cleaned") or {})
        for field in MOVED_TO_QUESTION_INFO_FIELDS:
            if field in record:
                info[field] = record[field]
        for field in ("business_type_name", "structure_type_name"):
            if record.get(field):
                info[field] = record[field]
        if isinstance(record.get("_source_item"), dict):
            info["_source_item"] = record["_source_item"]
        questions_map[question_id] = info
        origin_knw_id_by_qid[question_id] = list(record.get("origin_knw_id") or [])
        origin_knw_name_by_qid[question_id] = list(record.get("origin_knw_name") or [])
        normalized_origin_knw_id_by_qid[question_id] = list(
            record.get("normalized_origin_knw_id") or []
        )
        normalized_origin_knw_name_by_qid[question_id] = list(
            record.get("normalized_origin_knw_name") or []
        )

    if parent_id not in questions_map:
        # 理论上不会发生；这里防御空 question_id 等异常数据。
        return (
            [],
            {
                "groups": 1,
                "group_records_seen": len(records),
                "emitted_records": 0,
                "child_questions": len(children),
                "incomplete_groups": 1,
                "duplicate_question_ids": duplicate_question_ids,
                "composite_groups": 0,
                "non_composite_groups": 0,
                "groups_with_embedded_images": 0,
            },
            [
                {
                    "stage": "invalid_parent",
                    "parent_id": parent_id,
                    "record_count": len(records),
                    "message": "父题字段无效，题组已跳过",
                }
            ],
        )

    child_qids = [qid for qid in questions_map if qid != parent_id]
    has_children = bool(child_qids)
    has_embedded_images = any(
        bool((info or {}).get("has_img")) for info in questions_map.values()
    )
    metadata = {
        "has_children": has_children,
        "has_images": has_embedded_images,
    }

    outputs: list[dict[str, Any]] = []
    for target_qid, target_info in questions_map.items():
        is_stem = target_qid == parent_id
        if len(questions_map) == 1:
            record_type = "非复合题"
        elif is_stem:
            record_type = "复合题-题干"
        else:
            child_index = target_info.get("question_index", 999)
            record_type = f"复合题-第{child_index}小题"

        output = {
            "question_id": target_qid,
            "parent_id": parent_id,
            "is_stem": is_stem,
            "type": record_type,
            "questions_map": questions_map,
            "metadata": metadata,
            "origin_knw_id": origin_knw_id_by_qid[target_qid],
            "origin_knw_name": origin_knw_name_by_qid[target_qid],
            "normalized_origin_knw_id": normalized_origin_knw_id_by_qid[target_qid],
            "normalized_origin_knw_name": normalized_origin_knw_name_by_qid[target_qid],
            "global_outer_urls": global_outer_urls,
        }
        outputs.append(output)

    stats = {
        "groups": 1,
        "group_records_seen": len(ordered_records),
        "emitted_records": len(outputs),
        "child_questions": len(child_qids),
        "incomplete_groups": 0,
        "duplicate_question_ids": duplicate_question_ids,
        "composite_groups": int(has_children),
        "non_composite_groups": int(not has_children),
        "groups_with_embedded_images": int(has_embedded_images),
    }
    return outputs, stats, errors


def _process_api_group(
    root_item: dict[str, Any], source_order: int
) -> tuple[
    list[dict[str, Any]], dict[str, str], list[dict[str, Any]], dict[str, int]
]:
    root_qid = str(
        _first_value(root_item, ("questionId", "question_id"), default="")
    ).strip()
    if not root_qid:
        raise ValueError("API 顶层记录缺少 questionId")

    outer_urls = _group_outer_urls(root_item)
    records: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    stats = _new_stats()

    parent_record, label_stats = _clean_single_record(
        root_item, "api", parent_id_override=root_qid, source_order=source_order
    )
    records.append(parent_record)
    _merge_stats(stats, label_stats)

    small_questions = root_item.get("smallQuestions")
    if small_questions is not None and not isinstance(small_questions, list):
        stats["processing_errors"] += 1
        errors.append(
            {
                "stage": "small_questions_type",
                "question_id": root_qid,
                "message": "smallQuestions 不是列表，仅处理父题",
            }
        )
        small_questions = []

    for child_order, child_item in enumerate(small_questions or [], start=1):
        if not isinstance(child_item, dict):
            stats["processing_errors"] += 1
            errors.append(
                {
                    "stage": "invalid_child",
                    "question_id": root_qid,
                    "message": f"smallQuestions 第 {child_order} 项不是对象，已跳过",
                }
            )
            continue
        try:
            child_record, child_label_stats = _clean_single_record(
                child_item,
                "api",
                parent_id_override=root_qid,
                source_order=source_order + child_order,
            )
        except Exception as exc:
            stats["processing_errors"] += 1
            errors.append(
                {
                    "stage": "child_process",
                    "question_id": root_qid,
                    "child_order": child_order,
                    "message": f"{type(exc).__name__}: {exc}",
                }
            )
            continue

        # 原 step_1 只从父题补全缺失科目；题型字段保持小题自身值。
        if not child_record.get("subject"):
            child_record["subject"] = parent_record.get("subject", "")
        records.append(child_record)
        _merge_stats(stats, child_label_stats)

    return records, outer_urls, errors, stats


# ============================================================================
# 并行处理
# ============================================================================

def _initialize_worker(
    label_whitelist: set[str],
    filter_labels: bool,
    input_type: str,
    origin_label_name_map: dict[str, str],
    origin_label_id_whitelist: set[str],
) -> None:
    global _WORKER_LABEL_WHITELIST, _WORKER_FILTER_LABELS, _WORKER_INPUT_TYPE
    global _WORKER_ORIGIN_LABEL_NAME_MAP, _WORKER_ORIGIN_LABEL_ID_WHITELIST
    _WORKER_LABEL_WHITELIST = label_whitelist
    _WORKER_FILTER_LABELS = filter_labels
    _WORKER_INPUT_TYPE = input_type
    _WORKER_ORIGIN_LABEL_NAME_MAP = origin_label_name_map
    _WORKER_ORIGIN_LABEL_ID_WHITELIST = origin_label_id_whitelist


def _process_api_chunk(
    chunk: list[tuple[int, bytes]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    outputs: list[dict[str, Any]] = []
    records_for_sql: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    stats = _new_stats()

    for line_number, raw_line in chunk:
        stats["input_records"] += 1
        try:
            item = orjson.loads(raw_line)
        except Exception as exc:
            stats["json_decode_errors"] += 1
            errors.append(
                {
                    "stage": "json_decode",
                    "line_number": line_number,
                    "message": f"{type(exc).__name__}: {exc}",
                }
            )
            continue

        if not isinstance(item, dict):
            stats["processing_errors"] += 1
            errors.append(
                {
                    "stage": "top_level_type",
                    "line_number": line_number,
                    "message": f"顶层记录不是对象: {type(item).__name__}",
                }
            )
            continue

        stats["valid_input_records"] += 1
        try:
            records, outer_urls, group_errors, group_stats = _process_api_group(
                item, source_order=line_number * 1000
            )
        except Exception as exc:
            stats["processing_errors"] += 1
            errors.append(
                {
                    "stage": "api_group_process",
                    "line_number": line_number,
                    "message": f"{type(exc).__name__}: {exc}",
                }
            )
            continue

        _merge_stats(stats, group_stats)
        stats["questions_flattened"] += len(records)
        for error in group_errors:
            error.setdefault("line_number", line_number)
        errors.extend(group_errors)

        parent_id = records[0]["parent_id"] if records else ""
        group_outputs, group_result_stats, build_errors = _build_group_outputs(
            parent_id=parent_id,
            records=records,
            global_outer_urls=outer_urls,
        )
        _merge_stats(stats, group_result_stats)
        outputs.extend(group_outputs)
        for error in build_errors:
            error.setdefault("line_number", line_number)
        errors.extend(build_errors)

    return outputs, records_for_sql, errors, stats


def _process_sql_chunk(
    chunk: list[tuple[int, bytes]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    outputs: list[dict[str, Any]] = []
    records_for_sql: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    stats = _new_stats()

    for line_number, raw_line in chunk:
        stats["input_records"] += 1
        try:
            item = orjson.loads(raw_line)
        except Exception as exc:
            stats["json_decode_errors"] += 1
            errors.append(
                {
                    "stage": "json_decode",
                    "line_number": line_number,
                    "message": f"{type(exc).__name__}: {exc}",
                }
            )
            continue

        if not isinstance(item, dict):
            stats["processing_errors"] += 1
            errors.append(
                {
                    "stage": "top_level_type",
                    "line_number": line_number,
                    "message": f"顶层记录不是对象: {type(item).__name__}",
                }
            )
            continue

        stats["valid_input_records"] += 1
        try:
            record, label_stats = _clean_single_record(
                item,
                "sql",
                parent_id_override=None,
                source_order=line_number,
            )
        except Exception as exc:
            stats["processing_errors"] += 1
            errors.append(
                {
                    "stage": "sql_record_process",
                    "line_number": line_number,
                    "message": f"{type(exc).__name__}: {exc}",
                }
            )
            continue

        records_for_sql.append(record)
        _merge_stats(stats, label_stats)
        stats["questions_flattened"] += 1

    return outputs, records_for_sql, errors, stats


def _process_chunk(
    chunk: list[tuple[int, bytes]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    if _WORKER_INPUT_TYPE == "api":
        return _process_api_chunk(chunk)
    if _WORKER_INPUT_TYPE == "sql":
        return _process_sql_chunk(chunk)
    raise ValueError(f"未知输入类型: {_WORKER_INPUT_TYPE}")


# ============================================================================
# 读写、检测与主流程
# ============================================================================

def _ensure_parent_dir(path: str) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)


def _load_label_rows(path: str) -> list[dict[str, Any]]:
    suffix = os.path.splitext(path)[1].lower()
    if suffix == ".json":
        with open(path, "rb") as file:
            data = orjson.loads(file.read())
        if not isinstance(data, list):
            raise ValueError("标签 JSON 文件顶层必须是列表")
        return [row for row in data if isinstance(row, dict)]
    if suffix == ".jsonl":
        rows = []
        with open(path, "rb") as file:
            for raw_line in file:
                if not raw_line.strip():
                    continue
                row = orjson.loads(raw_line)
                if isinstance(row, dict):
                    rows.append(row)
        return rows
    raise ValueError(f"不支持的标签文件格式: {suffix}")


def _load_label_whitelist(path: str) -> set[str]:
    whitelist: set[str] = set()
    if not path:
        raise ValueError("启用标签过滤时必须配置 label_catalog_path")

    for row in _load_label_rows(path):
        value = row.get("knw_id")
        if value is None:
            continue
        label_id = str(value).strip()
        if label_id:
            whitelist.add(label_id)
    return whitelist


def _load_label_name_map(path: str) -> dict[str, str]:
    result: dict[str, str] = {}
    if not path:
        return result
    for row in _load_label_rows(path):
        label_id = str(row.get("knw_id") or "").strip()
        label_name = str(row.get("old_name") or row.get("new_name") or "").strip()
        if label_id and label_name:
            result[label_id] = label_name
    return result


def _detect_input_type(item: dict[str, Any]) -> str:
    has_api_camel = any(
        key in item
        for key in ("questionInfo", "questionId", "smallQuestions", "knwIds", "selfKnwIds")
    )
    has_sql_snake = any(
        key in item
        for key in ("question_info", "question_id", "origin_knw_id", "parent_id")
    )

    if has_api_camel and not has_sql_snake:
        return "api"
    if has_sql_snake and not has_api_camel:
        return "sql"

    # 混合字段时，以正文对象和 ID 字段的实际形态优先判断。
    if "questionInfo" in item and "question_info" not in item:
        return "api"
    if "question_info" in item and "questionInfo" not in item:
        return "sql"
    if "questionId" in item and "question_id" not in item:
        return "api"
    if "question_id" in item and "questionId" not in item:
        return "sql"
    raise ValueError("无法判断输入类型，请显式设置 input_type 为 api 或 sql")


def _peek_first_item(path: str) -> dict[str, Any]:
    suffix = os.path.splitext(path)[1].lower()
    if suffix == ".jsonl":
        with open(path, "rb") as file:
            for raw_line in file:
                if not raw_line.strip():
                    continue
                item = orjson.loads(raw_line)
                if not isinstance(item, dict):
                    raise ValueError("JSONL 首条非空记录不是 JSON 对象")
                return item
        raise ValueError("输入文件为空")

    if suffix == ".json":
        with open(path, "rb") as file:
            data = orjson.loads(file.read())
        if isinstance(data, dict):
            return data
        if not isinstance(data, list) or not data:
            raise ValueError("JSON 输入必须是非空对象列表")
        if not isinstance(data[0], dict):
            raise ValueError("JSON 首条记录不是对象")
        return data[0]

    raise ValueError(f"不支持的输入文件格式: {suffix}")


def _resolve_input_type(path: str, configured_type: str) -> str:
    configured_type = str(configured_type or "auto").lower()
    if configured_type == "auto":
        return _detect_input_type(_peek_first_item(path))
    if configured_type not in {"api", "sql"}:
        raise ValueError("input_type 只能是 auto、api 或 sql")
    return configured_type


def _iter_input_chunks(
    path: str, chunk_size: int, max_input_records: int
) -> Iterator[list[tuple[int, bytes]]]:
    """按记录数分块；每个元素为 (原始行号, JSON bytes)。"""
    if chunk_size <= 0:
        raise ValueError("chunk_size 必须大于 0")

    suffix = os.path.splitext(path)[1].lower()
    chunk: list[tuple[int, bytes]] = []
    yielded_records = 0

    if suffix == ".jsonl":
        with open(path, "rb") as file:
            for line_number, raw_line in enumerate(file, start=1):
                if not raw_line.strip():
                    continue
                chunk.append((line_number, raw_line))
                yielded_records += 1
                if len(chunk) >= chunk_size:
                    yield chunk
                    chunk = []
                if max_input_records > 0 and yielded_records >= max_input_records:
                    break
        if chunk:
            yield chunk
        return

    if suffix == ".json":
        with open(path, "rb") as file:
            data = orjson.loads(file.read())
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            raise ValueError("JSON 输入顶层必须是对象或对象列表")

        for index, item in enumerate(data, start=1):
            if max_input_records > 0 and yielded_records >= max_input_records:
                break
            if not isinstance(item, dict):
                raise ValueError(f"JSON 第 {index} 条记录不是对象")
            chunk.append((index, orjson.dumps(item)))
            yielded_records += 1
            if len(chunk) >= chunk_size:
                yield chunk
                chunk = []
        if chunk:
            yield chunk
        return

    raise ValueError(f"不支持的输入文件格式: {suffix}")


class StreamingJsonWriter:
    """流式写 JSONL，或写 JSON 数组，避免把最终结果全部堆在内存。"""

    def __init__(self, target_path: str, temp_path: str):
        self.suffix = os.path.splitext(target_path)[1].lower()
        if self.suffix not in {".json", ".jsonl"}:
            raise ValueError("输出文件只支持 .jsonl 或 .json")
        self.file = open(temp_path, "wb")
        self.count = 0
        if self.suffix == ".json":
            self.file.write(b"[")

    def write(self, item: dict[str, Any]) -> None:
        payload = orjson.dumps(item)
        if self.suffix == ".jsonl":
            self.file.write(payload + b"\n")
        else:
            if self.count > 0:
                self.file.write(b",\n")
            self.file.write(payload)
        self.count += 1

    def close(self) -> None:
        if not self.file.closed:
            if self.suffix == ".json":
                self.file.write(b"\n]\n" if self.count else b"]\n")
            self.file.close()

    def __enter__(self) -> "StreamingJsonWriter":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


def _write_error(error_file: Any, error: dict[str, Any]) -> None:
    error_file.write(orjson.dumps(error) + b"\n")


def _write_report(path: str, report: dict[str, Any]) -> None:
    if not path:
        return
    _ensure_parent_dir(path)
    temp_path = path + f".tmp.{os.getpid()}"
    with open(temp_path, "wb") as file:
        file.write(orjson.dumps(report, option=orjson.OPT_INDENT_2))
    os.replace(temp_path, path)


def _check_output_paths(config: dict[str, Any]) -> None:
    paths = [
        config["output_path"],
        config.get("report_path", ""),
        config.get("error_path", ""),
    ]
    for path in paths:
        if path and os.path.exists(path) and not config.get("overwrite_output", True):
            raise FileExistsError(f"输出文件已存在且 overwrite_output=false: {path}")


def run(config: dict[str, Any]) -> dict[str, Any]:
    """执行完整流程。config 结构与文件底部 CONFIG 一致。"""
    started = time.time()
    started_at = time.strftime("%Y-%m-%d %H:%M:%S")

    input_path = str(config.get("input_path", "")).strip()
    output_path = str(config.get("output_path", "")).strip()
    report_path = str(config.get("report_path", "")).strip()
    error_path = str(config.get("error_path", "")).strip()
    label_catalog_path = str(config.get("label_catalog_path", "")).strip()
    origin_label_catalog_path = str(
        config.get("origin_label_catalog_path", "")
    ).strip()
    configured_input_type = str(config.get("input_type", "auto")).strip().lower()
    filter_labels = bool(config.get("filter_original_label_ids", True))
    num_processes = int(config.get("num_processes", 1))
    chunk_size = int(config.get("chunk_size", 5000))
    max_input_records = int(config.get("max_input_records", 0))
    overwrite_output = bool(config.get("overwrite_output", True))

    if not input_path or not os.path.exists(input_path):
        raise FileNotFoundError(f"输入文件不存在: {input_path}")
    if not output_path:
        raise ValueError("必须配置 output_path")
    if os.path.abspath(input_path) == os.path.abspath(output_path):
        raise ValueError("input_path 和 output_path 不能相同")
    if num_processes <= 0:
        raise ValueError("num_processes 必须大于 0")
    if max_input_records < 0:
        raise ValueError("max_input_records 不能小于 0")
    if not error_path:
        raise ValueError("必须配置 error_path")

    artifact_paths = [
        ("output_path", output_path),
        ("report_path", report_path),
        ("error_path", error_path),
    ]
    nonempty_artifact_paths = [(name, path) for name, path in artifact_paths if path]
    absolute_artifact_paths = [
        (name, os.path.abspath(path)) for name, path in nonempty_artifact_paths
    ]
    if len({path for _, path in absolute_artifact_paths}) != len(absolute_artifact_paths):
        raise ValueError("output_path、report_path、error_path 必须互不相同")
    input_abs_path = os.path.abspath(input_path)
    for name, path in absolute_artifact_paths:
        if path == input_abs_path:
            raise ValueError(f"{name} 不能与 input_path 相同")

    config = dict(config)
    config["overwrite_output"] = overwrite_output
    _check_output_paths(config)

    label_whitelist = (
        _load_label_whitelist(label_catalog_path) if filter_labels else set()
    )
    origin_label_name_map = _load_label_name_map(origin_label_catalog_path)
    origin_label_id_whitelist = set(origin_label_name_map)
    origin_label_name_map.update(_load_label_name_map(label_catalog_path))
    input_type = _resolve_input_type(input_path, configured_input_type)

    for path in (output_path, report_path, error_path):
        if path:
            _ensure_parent_dir(path)

    output_dir = os.path.dirname(os.path.abspath(output_path))
    output_name = os.path.basename(output_path)
    output_temp_path = os.path.join(
        output_dir, f".{output_name}.tmp.{os.getpid()}"
    )
    error_temp_path = error_path + f".tmp.{os.getpid()}"

    stats = _new_stats()
    sql_groups: dict[str, list[dict[str, Any]]] = {}
    error_count = 0

    print(f"输入文件: {input_path}")
    print(f"输入类型: {input_type}（配置值: {configured_input_type}）")
    print(f"知识点白名单: {len(label_whitelist)} 个")
    print(f"原标签展示名称: {len(origin_label_name_map)} 个")
    print(f"原标签 ID 白名单: {len(origin_label_id_whitelist)} 个")
    print(f"并行进程数: {num_processes}，分块大小: {chunk_size}")
    if max_input_records > 0:
        print(f"本次只处理前 {max_input_records} 条输入记录")

    chunks = _iter_input_chunks(input_path, chunk_size, max_input_records)

    try:
        with StreamingJsonWriter(output_path, output_temp_path) as writer, open(
            error_temp_path, "wb"
        ) as error_file:
            if num_processes == 1:
                _initialize_worker(
                    label_whitelist,
                    filter_labels,
                    input_type,
                    origin_label_name_map,
                    origin_label_id_whitelist,
                )
                result_iter = map(_process_chunk, chunks)
                pool_context = None
            else:
                pool_context = Pool(
                    processes=num_processes,
                    initializer=_initialize_worker,
                    initargs=(
                        label_whitelist,
                        filter_labels,
                        input_type,
                        origin_label_name_map,
                        origin_label_id_whitelist,
                    ),
                )
                result_iter = pool_context.imap(_process_chunk, chunks, chunksize=1)

            try:
                for outputs, records_for_sql, errors, chunk_stats in tqdm(
                    result_iter, desc="处理分块", unit="chunk"
                ):
                    _merge_stats(stats, chunk_stats)
                    for error in errors:
                        _write_error(error_file, error)
                        error_count += 1

                    if input_type == "api":
                        for output in outputs:
                            writer.write(output)
                    else:
                        # SQL 数据没有可靠的“最外层父题 URL”，统一输出空对象。
                        for record in records_for_sql:
                            sql_groups.setdefault(record["parent_id"], []).append(record)
            finally:
                if pool_context is not None:
                    pool_context.close()
                    pool_context.join()

            if input_type == "sql":
                print(f"SQL 按 parent_id 分组: {len(sql_groups)} 组，开始组装输出...")
                for parent_id, group_records in tqdm(
                    sql_groups.items(), desc="组装 SQL 题组", unit="组"
                ):
                    group_outputs, group_stats, group_errors = _build_group_outputs(
                        parent_id=parent_id,
                        records=group_records,
                        global_outer_urls={},
                    )
                    _merge_stats(stats, group_stats)
                    for error in group_errors:
                        _write_error(error_file, error)
                        error_count += 1
                    for output in group_outputs:
                        writer.write(output)

        os.replace(output_temp_path, output_path)
        os.replace(error_temp_path, error_path)
        output_record_count = writer.count
    finally:
        if os.path.exists(output_temp_path):
            os.remove(output_temp_path)
        if os.path.exists(error_temp_path):
            os.remove(error_temp_path)

    finished_at = time.strftime("%Y-%m-%d %H:%M:%S")
    duration_seconds = round(time.time() - started, 3)
    report = {
        "started_at": started_at,
        "finished_at": finished_at,
        "duration_seconds": duration_seconds,
        "input": {
            "path": input_path,
            "type": input_type,
            "configured_type": configured_input_type,
            "max_input_records": max_input_records,
        },
        "output": {
            "path": output_path,
            "records": output_record_count,
        },
        "label_filter": {
            "enabled": filter_labels,
            "catalog_path": label_catalog_path,
            "origin_label_catalog_path": origin_label_catalog_path,
            "whitelist_size": len(label_whitelist),
            "origin_label_name_count": len(origin_label_name_map),
            "origin_label_id_whitelist_size": len(origin_label_id_whitelist),
        },
        "counts": stats,
        "errors": {
            "records_written": error_count,
            "path": error_path,
        },
        "config": {
            "num_processes": num_processes,
            "chunk_size": chunk_size,
            "overwrite_output": overwrite_output,
        },
    }
    _write_report(report_path, report)

    print("\n处理完成")
    print(f"输入记录: {stats['input_records']}")
    print(f"清洗后题目: {stats['questions_flattened']}")
    print(f"题组: {stats['groups']}（复合题 {stats['composite_groups']}）")
    print(f"输出记录: {output_record_count}")
    print(f"标签引用读取: {stats['labels_refs_read']}")
    print(f"标签去重后: {stats['labels_unique_before_filter']}")
    print(f"标签保留: {stats['labels_kept']}，过滤: {stats['labels_dropped']}")
    print(f"原标签为空记录: {stats['records_with_empty_labels']}（已保留）")
    print(
        "规范化后空标签记录: "
        f"{stats['questions_flattened'] - stats['records_with_nonempty_normalized_labels']}"
        "（已保留）"
    )
    print(f"错误/警告记录: {error_count}，详见: {error_path}")
    print(f"输出文件: {output_path}")
    if report_path:
        print(f"处理报告: {report_path}")
    return report


# ============================================================================
# 配置区
# ============================================================================
CONFIG = {
    # API 导出样例；跑 SQL 时改成对应 question_info/question_id 格式的 JSONL。
    "input_path": "/Users/yexinyong/0_work_data/llm/question_bank_all_info_download/src/xkw_primary_math_central_parent_ids/output/questions.jsonl",
    "output_path": "/Users/yexinyong/0_work_data/llm/project_files/primary_school_tagging/train_model/math_tagging/data/question_bank/test_dataset_temp/output/xkw_primary_math_central_parent_ids_2026_11_06.jsonl",
    "report_path": "/Users/yexinyong/0_work_data/llm/project_files/primary_school_tagging/train_model/math_tagging/data/question_bank/test_dataset_temp/output/xkw_primary_math_central_parent_ids_2026_11_06_report.json",
    "error_path": "/Users/yexinyong/0_work_data/llm/project_files/primary_school_tagging/train_model/math_tagging/data/question_bank/test_dataset_temp/output/xkw_primary_math_central_parent_ids_2026_11_06_errors.jsonl",
    "label_catalog_path": "/Users/yexinyong/0_work_data/llm/project_files/primary_school_tagging/train_model/math_tagging/resource/merge/math_knw_2026_11_04_normalized.jsonl",
    # 原标签 ID 不在当前规范化目录时，用旧全量目录的 old_name 补缺。
    "origin_label_catalog_path": "/Users/yexinyong/0_work_data/llm/project_files/primary_school_tagging/train_model/math_tagging/resource/process/math_knw_2026_10_10_all.jsonl",
    # auto: 自动判断；api: 读取 smallQuestions 并扁平化；sql: 直接按 parent_id 分组。
    "input_type": "auto",
    # 原始标签始终保留；该开关只控制 normalized_origin_knw_id 是否按目录过滤。
    "filter_original_label_ids": True,
    "num_processes": 8,
    "chunk_size": 5000,
    # 0 表示全量；冒烟测试可设为 1000。
    "max_input_records": 0,
    "overwrite_output": True,
}
# ============================================================================


if __name__ == "__main__":
    run(CONFIG)
