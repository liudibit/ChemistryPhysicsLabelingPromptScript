#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按题目 ID 从题库接口下载原始题目对象，保存为可复用的 JSONL 缓存。

关键点：每行保存接口返回的原始根题目对象（questionId/questionInfo/smallQuestions），
不在这里清洗正文、知识点、答案或图片。打标脚本读取该文件时，会复用与在线取题
完全相同的 extract_api_questions + normalize_question 逻辑。

示例：
    python question_bank_api_download.py \
        --input "../初中数理化相似题标签/初中化学/化学500题.xlsx" \
        --limit 100 \
        --output "output/化学500题_前100_raw_api.jsonl" \
        --workers 5
"""
from __future__ import annotations

import argparse
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import requests
from openpyxl import load_workbook

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV = PROJECT_ROOT / "初中数理化相似题标签" / "初中数学" / ".env"
DEFAULT_INPUT = PROJECT_ROOT / "初中数理化相似题标签" / "初中化学" / "化学500题.xlsx"
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "output" / "question_bank_raw_api.jsonl"
API_URL = "https://tps-tiku-api.staff.xdf.cn/api/question/v2/query-by-questionIds"


def load_env_file(path: Path) -> None:
    """加载简单 KEY=VALUE 配置；不覆盖进程中已有的环境变量。"""
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
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


def read_question_ids(path: Path) -> list[str]:
    """读取题目 ID：支持 xlsx/xlsm 首列、txt 每行一个、JSONL 的题目对象。"""
    suffix = path.suffix.lower()
    ids: list[str] = []
    if suffix in {".xlsx", ".xlsm"}:
        wb = load_workbook(path, read_only=True, data_only=True)
        try:
            for row in wb.active.iter_rows(min_row=2, max_col=1, values_only=True):
                if row and row[0] is not None and str(row[0]).strip():
                    ids.append(str(row[0]).strip())
        finally:
            wb.close()
    elif suffix == ".txt":
        ids = [
            line.strip()
            for line in path.read_text(encoding="utf-8-sig").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
    elif suffix == ".jsonl":
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if not line.strip():
                continue
            obj = json.loads(line)
            value = obj.get("questionId") or obj.get("question_id") or obj.get("parent_id")
            if value is not None and str(value).strip():
                ids.append(str(value).strip())
    else:
        raise ValueError(f"不支持的题目 ID 文件类型：{suffix}")
    return list(dict.fromkeys(ids))


def read_downloaded_ids(path: Path) -> set[str]:
    """读取已有 JSONL 缓存中的根题目 ID，用于断点续传。"""
    result: set[str] = set()
    if not path.exists():
        return result
    with path.open(encoding="utf-8") as file:
        for line in file:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            value = obj.get("questionId") or obj.get("question_id")
            if value is not None and str(value).strip():
                result.add(str(value).strip())
    return result


def extract_roots(payload: Any) -> list[dict[str, Any]]:
    """从接口响应中取出原始根题目对象，不做任何字段清洗。"""
    data = payload.get("data") if isinstance(payload, dict) else None
    roots: Any = data
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        roots = data["data"]
    if isinstance(roots, dict):
        roots = [roots]
    if not isinstance(roots, list):
        return []
    return [item for item in roots if isinstance(item, dict)]


def api_headers() -> dict[str, str]:
    headers = {
        "appCode": os.getenv("TIKU_APP_CODE", "").strip(),
        "businessCode": os.getenv("TIKU_BUSINESS_CODE", "").strip(),
        "appToken": os.getenv("TIKU_APP_TOKEN", "").strip(),
        "timeStamp": os.getenv("TIKU_TIMESTAMP", "").strip() or str(int(time.time() * 1000)),
        "Content-Type": "application/json",
    }
    missing = [key for key in ("appCode", "businessCode", "appToken") if not headers[key]]
    if missing:
        raise RuntimeError(f"题库配置缺失：{', '.join(missing)}")
    return headers


def fetch_one(
    qid: str,
    headers: dict[str, str],
    timeout: int,
    retries: int,
) -> tuple[str, list[dict[str, Any]], list[str]]:
    """下载一道大题；返回原始根题目对象列表和错误信息。"""
    errors: list[str] = []
    for attempt in range(retries + 1):
        try:
            response = requests.post(
                API_URL,
                headers=headers,
                json={"questionList": [{"questionId": qid}]},
                timeout=(10, timeout),
            )
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict):
                code = payload.get("code")
                if payload.get("success") is False or code not in (None, 0, "0", 200, "200"):
                    raise RuntimeError(
                        f"接口业务错误 code={code}: {payload.get('message') or '未知错误'}"
                    )
            roots = extract_roots(payload)
            if not roots:
                raise RuntimeError("接口未返回题目对象")
            return qid, roots, errors
        except Exception as exc:  # noqa: BLE001
            errors.append(f"attempt={attempt + 1}: {type(exc).__name__}: {exc}")
            if attempt < retries:
                time.sleep(min(2**attempt, 8))
    return qid, [], errors


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="题目 ID 的 Excel/TXT/JSONL")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="原始接口题目 JSONL 输出路径")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 个 ID；0 表示全部")
    parser.add_argument("--workers", type=int, default=5, help="并发请求数，默认 5")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--no-resume", action="store_true", help="不读取已有输出，重新下载")
    args = parser.parse_args()

    if args.limit < 0 or args.workers <= 0:
        raise ValueError("limit 不能小于 0，workers 必须大于 0")
    if not args.input.is_file():
        raise FileNotFoundError(f"输入文件不存在：{args.input}")

    load_env_file(args.env_file)
    headers = api_headers()
    ids = read_question_ids(args.input)
    if args.limit > 0:
        ids = ids[: args.limit]
    if not ids:
        raise RuntimeError("没有读取到题目 ID")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    error_path = args.output.with_suffix(args.output.suffix + ".errors.jsonl")
    downloaded = set() if args.no_resume else read_downloaded_ids(args.output)
    todo = [qid for qid in ids if qid not in downloaded]

    print(f"题目总数：{len(ids):,}")
    print(f"已有缓存：{len(ids) - len(todo):,}")
    print(f"本次下载：{len(todo):,}")
    if not todo:
        print(f"无需下载，输出文件：{args.output}")
        return

    success = failed = objects_written = 0
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [
            executor.submit(fetch_one, qid, headers, args.timeout, args.retries)
            for qid in todo
        ]
        for index, future in enumerate(futures, 1):
            qid, roots, errors = future.result()
            if roots:
                with args.output.open("a", encoding="utf-8") as file:
                    for root in roots:
                        file.write(json.dumps(root, ensure_ascii=False) + "\n")
                        objects_written += 1
                success += 1
            else:
                failed += 1
                with error_path.open("a", encoding="utf-8") as file:
                    file.write(json.dumps({"questionId": qid, "errors": errors}, ensure_ascii=False) + "\n")
            print(f"[{index}/{len(todo)}] {qid} -> {'成功' if roots else '失败'}", flush=True)

    print(f"\n下载完成：成功 {success}，失败 {failed}，写入题目对象 {objects_written}")
    print(f"原始题目缓存：{args.output}")
    if failed:
        print(f"错误记录：{error_path}")


if __name__ == "__main__":
    main()
