#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""物理/化学复合题大题级打标：先取完整大题，再合并所有小题一次打标。"""
from __future__ import annotations
import argparse
import base64
from collections import OrderedDict
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import html
import json
import mimetypes
import os
from pathlib import Path
import re
import sys
import time

import requests
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from PIL import Image

# 脚本位于项目根目录；复用数学目录中已验证的题库请求、环境变量和 ID 读取逻辑。
ROOT = Path(__file__).resolve().parent
MATH = ROOT / '初中数学'
sys.path.insert(0, str(MATH))
from math_tag_common import fetch_questions, load_env_file, load_question_ids  # noqa: E402

# 延续旧版化学打标页 DEFAULT_PROMPT 的每维度数量上限。
CHEM_MAX_TAGS = {
    '题型特征聚类树': 3, '知识内容平行标签': 5, '物质五维分析标签': 5,
    '化学方程式环节标签': 5, '认知与素养维度': 3, '情境与方法维度': 3,
    '答案与信息载体': 3, '交叉组合标签': 3, '相似题专用标签': 5,
}

# 各学科的输入目录、标签库及最终输出维度。新增学科时应同时补充标签解析和提示词规则。
CONFIG = {
    'physics': {
        'directory': ROOT / '初中物理', 'ids': '初中物理题目ID.xlsx', 'labels': '物理相似题标签.xlsx',
        'dimensions': ['题型', '背景', '数学计算方法', '研究对象', '运动过程特征', '考点'],
    },
    'chemistry': {
        'directory': ROOT / '初中化学', 'ids': '化学新一轮200题.xlsx',
        'labels': '初中化学_相似题推荐标签分类树-终版.xlsx',
        'dimensions': ['题型特征聚类树', '知识内容平行标签', '物质五维分析标签', '化学方程式环节标签',
                       '认知与素养维度', '情境与方法维度', '答案与信息载体', '交叉组合标签', '相似题专用标签'],
    },
}


def unique(items):
    return list(dict.fromkeys(x for x in items if x))


def format_prompt_field(value):
    """将题库的富结构字段转为可读提示词文本，保留知识点名称等关键信息。"""
    if value is None or value == '':
        return ''
    if isinstance(value, list):
        return '、'.join(filter(None, (format_prompt_field(item) for item in value)))
    if isinstance(value, dict):
        for key in ('knwName', 'name', 'knowledgePoint', 'knowledgePointName', 'kpName', 'label', 'title'):
            if value.get(key):
                return format_prompt_field(value[key])
        return json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    return str(value).strip()


def label_catalog(subject):
    """从学科标签 Excel 提取可输出的末级标签及供模型判定的简要说明。"""
    conf = CONFIG[subject]
    book = load_workbook(conf['directory'] / conf['labels'], read_only=True, data_only=True)
    catalog = {d: [] for d in conf['dimensions']}
    guidance = {d: [] for d in conf['dimensions']}
    if subject == 'physics':
        # 物理标签分散在多个工作表，标签位于 D 列；“考点”另从知识树的 F 列读取。
        mapping = {'题型标签': '题型', '试题背景': '背景', '数学计算方法': '数学计算方法',
                   '研究对象': '研究对象', '运动过程特征': '运动过程特征'}
        for sheet_name, dim in mapping.items():
            for row in book[sheet_name].iter_rows(min_row=2, values_only=True):
                label = str(row[3] or '').strip() if len(row) > 3 else ''
                if label:
                    catalog[dim].append(label)
                    description = '；'.join(str(v).strip() for v in row[4:6] if v)
                    guidance[dim].append(f'{label}: {description[:160]}')
        # F 是考点名称，C 是知识点名称；历史结果 K 列使用考点名称。
        for row in book['知识树拓展'].iter_rows(min_row=2, values_only=True):
            name = str(row[5] or '').strip() if len(row) > 5 else ''
            if name:
                catalog['考点'].append(name)
        catalog['考点'] = unique(catalog['考点'])
    else:
        # 化学九维标签集中在同一张表：前四列依次为维度、一级、二级、末级标签。
        for row in book['全部标签合集'].iter_rows(min_row=2, values_only=True):
            if len(row) < 4:
                continue
            dim, top, second, tag = (str(v or '').strip() for v in row[:4])
            if dim not in catalog or not tag:
                continue
            catalog[dim].append(tag)
            # 实验模式：九个维度的每个末级标签都携带第五列完整说明。
            guidance[dim].append(f'{tag}：{str(row[4] or "").strip()}')
    book.close()
    catalog = {key: unique(value) for key, value in catalog.items()}
    for dim, tags in catalog.items():
        if not tags:
            raise RuntimeError(f'{dim} 标签为空')
    return catalog, guidance


# 本地 JSONL 导出中的题型/结构代码转中文；未收录的代码原样保留，避免向提示词注入猜测的名称。
JSONL_BUSINESS_TYPE_NAMES = {
    'CHX-SYT': '实验题', 'CHX-TKT': '填空题', 'CHX-JST': '计算题', 'CHX-TDT': '推断题',
    'CHX-GYLCT': '工艺流程题', 'CHX-KPYDT': '科普阅读题', 'CHX-ZONGHE': '综合题', 'CHX-QT': '其他',
}
JSONL_STRUCTURE_TYPE_NAMES = {
    'fuhe': '复合题-题干', 'fuhe_zhuguan': '复合题-主观小题', 'fuhe_fillblank': '复合题-填空小题',
    'fuhe_danxuan': '复合题-单选小题', 'fuhe_duoxuan': '复合题-多选小题', 'fuhe_panduan': '复合题-判断小题',
    'danxuan': '单选题', 'duoxuan': '多选题', 'fillblank': '填空题', 'zhuguan': '主观题', 'panduan': '判断题',
}


def load_jsonl_compound_questions(path):
    """从本地 JSONL 读取复合题，替代题库接口取题。

    每行含 questions_map（各小题全文与分字段图片）；同一 parent_id 的题干行/小题行
    合并为一份大题。返回 (按文件顺序的大题 ID 列表, {大题ID: 归一化小题列表})，
    小题结构与 normalize_question 的输出一致，可直接进入 make_prompt。
    """
    rows = {}
    with Path(path).open(encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            qmap = obj.get('questions_map')
            if not isinstance(qmap, dict) or not qmap:
                continue
            pid = str(obj.get('parent_id') or obj.get('question_id') or '').strip()
            if not pid:
                continue
            slot = rows.setdefault(pid, {'order': len(rows), 'top_images': [], 'map': {}})
            slot['map'].update({str(k): v for k, v in qmap.items()})
            top = (obj.get('metadata') or {}).get('stem_image_url') if isinstance(obj.get('metadata'), dict) else None
            slot['top_images'] = unique(slot['top_images'] + [u for u in (obj.get('stem_image_url'), obj.get('image_url'), top) if u])
    ordered, questions_by_id = [], {}
    for pid, slot in sorted(rows.items(), key=lambda kv: kv[1]['order']):
        qmap = slot['map']
        keys = list(qmap.keys())

        def sort_key(k):
            item = qmap.get(k) or {}
            try:
                index = int(item.get('question_index') or 0)
            except (TypeError, ValueError):
                index = 0
            return (0 if k == pid else 1, index, keys.index(k))

        questions = []
        for k in sorted(keys, key=sort_key):
            item = qmap.get(k) or {}
            images = {field: unique((item.get('images') or {}).get(field) or [])
                      for field in ('stem', 'options', 'analysis', 'answer')}
            questions.append({
                'questionId': k,
                'businessTypeName': JSONL_BUSINESS_TYPE_NAMES.get(item.get('business_type'), str(item.get('business_type') or '未知')),
                'structureTypeName': JSONL_STRUCTURE_TYPE_NAMES.get(item.get('structure_type'), str(item.get('structure_type') or '未知')),
                'stem': str(item.get('stem') or '').strip(),
                'options': str(item.get('options') or '').strip(),
                'answer': str(item.get('answer') or '').strip(),
                'analysis': str(item.get('analysis') or '').strip(),
                'knowledge': '', 'solution': '',
                'fieldImages': images,
                'imageUrls': unique(url for field in ('stem', 'options', 'analysis', 'answer') for url in images[field]),
                'raw': item,
            })
        # 整题在 questions_map 中没有任何原图时，用顶层整题截图兜底；已有分字段原图时不重复发送截图。
        if slot['top_images'] and not any(q['imageUrls'] for q in questions):
            questions[0]['fieldImages']['stem'] = unique(slot['top_images'] + questions[0]['fieldImages']['stem'])
            questions[0]['imageUrls'] = unique(questions[0]['fieldImages']['stem'] + questions[0]['imageUrls'])
        ordered.append(pid)
        questions_by_id[pid] = questions
    if not ordered:
        raise RuntimeError(f'未能从 JSONL 中读取到复合题：{path}')
    return ordered, questions_by_id


def make_prompt(subject, big_id, questions, catalog, guidance):
    """将同一大题的共用信息和全部小题合并为一次模型请求。"""
    name = '初中物理' if subject == 'physics' else '初中化学'
    blocks = []
    for i, q in enumerate(questions, 1):
        blocks.append(f"【第{i}部分，题目ID={q['questionId']}】\n题库题型：{q['businessTypeName']}；结构类型：{q['structureTypeName']}\n"
                      f"题干：{q['stem'] or '[文字缺失，参看图片]'}\n选项：{q['options'] or '[无]'}\n"
                      f"答案：{q['answer'] or '[无]'}\n解析：{q['analysis'] or '[无]'}\n"
                      f"题目知识点：{format_prompt_field(q.get('knowledge')) or '[无]'}\n"
                      f"解题思路参考：{q.get('solution') or '[无]'}")
    guidance_text = '\n'.join(f'{dim}：\n' + '\n'.join(lines) for dim, lines in guidance.items() if lines)
    rules = (f'你是{name}复合题相似题标注专家。大题ID={big_id}。下面所有部分属于同一道大题，'
             '包括大题共用题干及全部小题。必须合并评价，仅返回一份大题级标签，不能按小题分别输出。\n'
             '逐一核对题干、选项、答案、解析、题目知识点、解题思路参考和随消息附上的图片。"题目知识点"与"解题思路参考"是题库自动关联字段，可能误挂、可能是前置/背景知识、也可能对应错误物质，仅供对照，不得直接采信，更不得作为输出标签的来源；以题干、选项、答案、解析和题图为最终依据；；图片按小题顺序紧跟在文本之后，'
             '若无法看清图片则不要猜测。同一维度内去重；'
             '不为凑数选择不相关标签。每个正式标签必须是给定全集的完整原文；标签不适用或不确定时返回空数组，备注说明。\n')
    if subject == 'physics':
        rules += ('优先标解题实际使用的概念、对象、方法。题型以实际作答结构为准；背景和研究对象要与题目情境对应。'
                  '数学计算方法仅在解析实际进行对应计算时选。运动过程特征依据实际运动或变化，不因出现物体而强行选。'
                  '考点从知识树考点名称选择：对于题目实际考查且解析实际涉及的考点，可同时选多项；'
                  '不得把知识点名称冒充考点名称。\n')
    else:
        rules += (
            '【化学九维判定规则（承接旧版教研提示词，适配复合题）】\n'
            '1. 以大题共同题干和各小问实际考查内容为准；答案与解析用于确认考查点，'
            '不能凭解析提到的其他概念扩大标签范围。逐小问核查证据后合并为一份标签，'
            '保留各小问确实考查且有助于相似题检索的标签，去重；前问结论可作为后问条件。'
            '不严格与考察内容相关的标签不选，不确定的标签不选\n'
            '2. 只能原样复制标签全集中的末级标签，不能自创、同义改写、缩写、合并或补充括号。'
            '一级和二级分类仅用于理解边界，不能直接输出为标签；题库题型不是标签白名单。\n'
            '3. 数量上限是硬限制；没有充分证据时少选或留空，不要为了凑满数量猜测。'
            '九维上限：题型特征聚类树最多3个、知识内容平行标签最多5个、物质五维分析标签最多5个、'
            '化学方程式环节标签最多5个、认知与素养维度最多3个、情境与方法维度最多3个、'
            '答案与信息载体最多3个、交叉组合标签最多3个、相似题专用标签最多5个。\n'
            '4. 题型特征描述实际作答结构，优先选择最具体的末级标签；知识内容只选大题实际考查的核心化学知识。'
            '物质五维只在实际涉及具体物质及其性质、用途、制备、存在形式或检验时选；'
            '方程式环节只在题目实际涉及该反应或方程式时选；两者不能被知识内容维度替代或强行填满。\n'
                '【高风险排除规则，优先级高于标签名称相似性】\n'
            '1. “化学实验操作”仅用于实际考查具体操作步骤、规范或器材使用；'
            '药品存放、仪器名称识别不得仅因此标注。\n'
            '2. “装置图分析型”要求答案依赖装置结构、连接、功能或操作分析；'
            '题面有装置图但不分析装置时不得标注。\n'
            '3. “X-性质分析”要求题目直接考查该物质的物理或化学性质；'
            '分子运动、微粒间隔、相变微观解释不得据此标注。\n'
            '4. 化学方程式环节标签要求对应反应环节是实际作答或判分点；'
            '仅在题干、选项或背景材料中出现反应不得标注。\n'
            '5. 认知素养只选完成任务实际要求的能力；情境方法必须有明确素材情境或科学方法证据。'
            '答案信息可同时描述答案形式与信息载体，但必须有题面或图片证据。'
            '交叉组合只在确有跨学段、课标大概念或跨模块联系时选；'
            '相似题专用只在选项、解析或大题结构能证明干扰项模式、难度维度等属性时选。\n'
            '6. 图类标签必须与题图类型严格对应，不能"有图就套"：`装置图`类仅用于实验装置/仪器连接图，实验操作步骤示意图不算；`函数图像`类（如「图像分析法」）仅用于有坐标曲线/函数图像的题；`图表解读`仅用于数据表格或函数图像；纯操作示意图只标"实验操作/步骤"类载体，不标装置图、不标函数图像。图片类标签必须核对题图，不能只凭题库元数据猜测。\n'
            '7. 输出前逐项检查：所属维度、标签原文、题目证据、数量上限、是否重复表达。'
            '删除所有不确定标签；备注仅记录难以判断或无法匹配的原因，不得把备注当作正式标签。\n'
            '8. 文字表达式书写不是方程式书写。\n'
            '9. 「答案与信息载体」维度的「信息载体」类中，「纯文字信息型」与其他所有末级标签互斥，只能选一个：题干确为纯文字（无图、无表格、无坐标、无装置等）时才标「纯文字信息型」；只要题干含任何图、表格、坐标、装置等非纯文字载体，就不标「纯文字信息型」，改标对应载体标签。\n'
            '10. "变量控制法"仅当题面实际出现多组对照实验（有至少两组实验记录，各组之间只改变一个因素、其余条件保持相同，并据此对比得出结论）时才标；仅出现"探究 X 随 Y 的变化关系"这类字眼、或只有升温/蒸发等单组观察的，不算。\n'
            '11. 知识内容标签不可拆开、不可部分匹配：只考到标签内涵的一部分时不选。\n'
            '12. 题型特征聚类树里的"选择题"标签，一律以题干实际呈现的选项形式与作答方式判定，不得以题库题型字段（businessTypeName）为准：- 「常规四选一（单项选择）」仅当题干给出 4 个相互独立的选项、且从中选 1 个时使用；- 选项由"①②③④ 等多条陈述组合而成、需综合判断后选组合"的，标「组合式单选题」；- 有多个合理项、需选最优的，标「最佳选择题」；- 需选出两个及以上正确答案的，标「多项选择题」；- 选项数量不是 4 个时，不得标「常规四选一（单项选择）」；- 题库题型与题干实际作答结构不一致时，以题干实际呈现为准。\n'
            '13.「认知与素养维度」只选完成作答实际必需的认知能力与核心素养，每个核心素养标签都要有题干/解析中的明确证据：其中「证据推理」仅限题目实际基于实验证据（实验现象、实验数据、实验事实）进行逻辑推理时才标；仅书写或配平化学方程式、回忆概念符号、直接计算而不涉及实验证据的题目不能标「证据推理」。\n'
            '14. 知识内容只选"设问直接要求作答、判断或解释"的知识点；仅为作答所依赖的前置/工具性知识一律不选。选择的知识内容维度的标签以题干实际考察内容为主，解析为辅。题干未提及而解析提及时不选。'
            '15.「记忆-符号识别」仅当题面给出符号、化学式或图标并要求解读其含义时使用。'
        )
    rules += ('以下是官方候选标签全集；\n标签全集：'
              + json.dumps(catalog, ensure_ascii=False, separators=(',', ':')) + '\n标签判定说明：\n' + guidance_text
              + '\n待标注大题：\n' + '\n\n'.join(blocks) + '\n\n只返回 JSON 对象，不要 Markdown 或额外字段。格式：'
              + json.dumps({**{d: [] for d in catalog}, '备注': []}, ensure_ascii=False))
    return rules


def image_urls(questions):
    # 图片按题干、选项、解析、答案的字段顺序去重；最多发送 28 张以控制请求体大小。
    return unique(url for q in questions for field in ('stem', 'options', 'analysis', 'answer')
                  for url in q.get('fieldImages', {}).get(field, []))[:28]


# 仿照 physics-knowledge-points-tagging 的图片处理：超出像素上限的图等比缩到上限内，并统一转 PNG。
IMG_MAX_PIXELS = 262144  # 512×512；视觉模型默认最大像素数


def compress_image_bytes(img_bytes, max_pixels=IMG_MAX_PIXELS):
    """等比缩图并统一转 PNG，与 physics-knowledge-points-tagging 的 _process_image_bytes 对齐。"""
    img = Image.open(BytesIO(img_bytes))
    img.load()
    is_png = (img.format or '').upper() == 'PNG'
    must_encode = not is_png
    if is_png:
        if img.mode == 'P':
            img = img.convert('RGBA')
            must_encode = True
    elif img.mode in ('RGBA', 'LA', 'PA'):
        img = img.convert('RGBA')
    else:
        img = img.convert('RGB')
    width, height = img.size
    if width * height > max_pixels:
        ratio = (max_pixels / (width * height)) ** 0.5
        new_w, new_h = int(width * ratio), int(height * ratio)
        img = img.resize((new_w, new_h), Image.LANCZOS)
        must_encode = True
    if is_png and not must_encode:
        return img_bytes
    buf = BytesIO()
    img.save(buf, format='PNG')
    return buf.getvalue()


def create_images(session, urls, directory, timeout):
    """下载复核原图，同时生成模型请求需要的 data URL；单张失败不阻断整题打标。"""
    items = []
    directory.mkdir(parents=True, exist_ok=True)
    for index, url in enumerate(urls, 1):
        item = {'url': url, 'local': '', 'error': ''}
        try:
            response = session.get(url, timeout=(10, timeout))
            response.raise_for_status()
            content = response.content
            if content.startswith(b'\xff\xd8'):
                mime, suffix = 'image/jpeg', '.jpg'
            elif content.startswith(b'\x89PNG'):
                mime, suffix = 'image/png', '.png'
            elif content.startswith(b'GIF'):
                mime, suffix = 'image/gif', '.gif'
            elif content.startswith(b'RIFF') and content[8:12] == b'WEBP':
                mime, suffix = 'image/webp', '.webp'
            else:
                raise RuntimeError('不支持的图片类型')
            dest = directory / f'{index:02d}{suffix}'
            dest.write_bytes(content)
            item['local'] = f'../images/{directory.name}/{dest.name}'
            # 豆包视觉接口要求图片宽、高均不低于 14 像素。小图通常是分隔线等
            # 装饰资源：保留在复核页，但不作为模型输入，以免整题请求被 400 拒绝。
            with Image.open(BytesIO(content)) as image:
                width, height = image.size
            item['width'], item['height'] = width, height
            if width < 14 or height < 14:
                item['error'] = f'图片尺寸过小（{width}x{height}），未发送模型'
            else:
                compressed = compress_image_bytes(content)
                item['data_url'] = 'data:image/png;base64,' + base64.b64encode(compressed).decode('ascii')
        except Exception as e:
            item['error'] = f'{type(e).__name__}: {e}'
        items.append(item)
    return items


def model_call(session, prompt, images, model, token, timeout, retries):
    """调用门神兼容接口，并保留实际请求配置供审计与人工复核。"""
    content = [{'type': 'text', 'text': prompt}]
    for item in images:
        if item.get('data_url'):
            content.append({'type': 'image_url', 'image_url': {'url': item['data_url']}})
    options = {'temperature': 0.1, 'max_tokens': 4096, 'enable_thinking': True, 'thinking': {'type': 'enabled'}}
    if model.startswith('claude'):
        # Claude 的思考参数格式与豆包兼容模型不同，不能混用 enable_thinking。
        options = {'max_tokens': 8192, 'thinking': {'type': 'enabled', 'budget_tokens': 4096}}
    elif model.startswith('deepseek'):
        # deepseek 自带推理（返回 reasoning_content），门神不认 enable_thinking/thinking，也不认 reasoning_effort；传最小参数即可。
        options = {'temperature': 0.1, 'max_tokens': 16384}
    body = {'model': model, 'messages': [{'role': 'user', 'content': content}], 'stream': True, **options}
    errors = []
    call_started = time.perf_counter()
    for attempt in range(retries + 1):
        try:
            response = session.post('http://menshen.xdf.cn/v1/chat/completions',
                                    headers={'Authorization': token, 'Content-Type': 'application/json'},
                                    json=body, timeout=(10, timeout), stream=True)
            if response.status_code >= 400:
                raise RuntimeError(f'HTTP {response.status_code}: {response.text[:900]}')
            raw_parts = []
            usage = None
            finish_reason = None
            for raw_line in response.iter_lines(decode_unicode=True):
                line = (raw_line or '').strip()
                if not line.startswith('data:'):
                    continue
                data = line[5:].strip()
                if data == '[DONE]':
                    break
                try:
                    chunk = json.loads(data)
                except Exception:
                    continue
                choices = chunk.get('choices') or [{}]
                delta = (choices[0].get('delta') or {}) if choices else {}
                if delta.get('content'):
                    raw_parts.append(delta['content'])
                if choices and choices[0].get('finish_reason'):
                    finish_reason = choices[0]['finish_reason']
                if chunk.get('usage'):
                    usage = chunk['usage']
            raw = ''.join(raw_parts)
            return raw, errors, {'usage': usage, 'finish_reason': finish_reason, 'options': options,
                                 'sent_images': sum(bool(x.get('data_url')) for x in images),
                                 '模型耗时秒': round(time.perf_counter() - call_started, 2), '尝试次数': attempt + 1}
        except Exception as e:
            errors.append(f'attempt={attempt+1}: {type(e).__name__}: {e}')
            if attempt < retries:
                time.sleep(min(2 ** attempt, 8))
    return '', errors, {'options': options, 'sent_images': sum(bool(x.get('data_url')) for x in images),
                        '模型耗时秒': round(time.perf_counter() - call_started, 2), '尝试次数': retries + 1}


USAGE_FIELDS = ('输入tokens', '输出tokens', '思考tokens', '正式回答tokens', '总tokens')


def empty_usage():
    """返回一份全零的 token 用量，供累加与异常兜底使用。"""
    return {k: 0 for k in USAGE_FIELDS}


def extract_usage(meta):
    """拆出门神返回的 usage。

    豆包的 completion_tokens 已经包含思考过程，故正式回答量需要相减；
    实测样本：输出 427 字符对应 completion=2582、reasoning=2356，差值 226 才与正文字数吻合。
    """
    usage = (meta or {}).get('usage') or {}
    details = usage.get('completion_tokens_details') or {}
    prompt = int(usage.get('prompt_tokens') or 0)
    completion = int(usage.get('completion_tokens') or 0)
    reasoning = int(details.get('reasoning_tokens') or 0)
    return {'输入tokens': prompt, '输出tokens': completion, '思考tokens': reasoning,
            '正式回答tokens': completion - reasoning,
            '总tokens': int(usage.get('total_tokens') or prompt + completion)}


def add_usage(base, extra):
    """按字段累加两份 token 用量。"""
    return {k: int(base.get(k, 0)) + int(extra.get(k, 0)) for k in USAGE_FIELDS}


def audit_usage_by_id(path):
    """按大题 ID 汇总审计文件中的用量与模型耗时。

    同一道题重跑会追加新记录，这里只保留最后一条，避免断点续跑时重复计数。
    """
    latest = {}
    path = Path(path)
    if path.exists():
        with path.open(encoding='utf-8') as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                meta = item.get('模型元数据') or {}
                latest[str(item.get('大题ID'))] = (extract_usage(meta), float(meta.get('模型耗时秒') or 0))
    return latest


def format_duration(seconds):
    """把秒数格式化成中文可读时长。"""
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    parts = []
    if hours:
        parts.append(f'{hours}小时')
    if hours or minutes:
        parts.append(f'{minutes}分')
    parts.append(f'{secs}秒')
    return ''.join(parts)


def print_summary(summary):
    """在控制台打印本次运行的耗时与 token 统计。"""
    batch = summary['本批token']
    total = summary['累计token']
    average = summary['本批单题平均token']
    bar = '─' * 36
    ratio = f'，思考占输出 {batch["思考tokens"] / batch["输出tokens"]:.1%}' if batch['输出tokens'] else ''
    print(f'\n{bar}\n本次运行统计 · {summary["学科"]} · {summary["模型"]}\n{bar}', flush=True)
    print(f'开始 {summary["开始时间"]}    结束 {summary["结束时间"]}', flush=True)
    print(f'墙钟耗时：{summary["墙钟耗时"]}（{summary["墙钟耗时秒"]} 秒），并发 {summary["并发数"]}', flush=True)
    status_text = '，'.join(f'{k} {v}' for k, v in summary['本批状态分布'].items()) or '无'
    print(f'本批处理：{summary["本批处理题数"]} 题　状态：{status_text}', flush=True)
    print(f'本批 token：输入 {batch["输入tokens"]:,}｜输出 {batch["输出tokens"]:,}'
          f'（思考 {batch["思考tokens"]:,}／正式回答 {batch["正式回答tokens"]:,}）｜合计 {batch["总tokens"]:,}{ratio}', flush=True)
    if summary['本批处理题数']:
        print(f'单题平均：墙钟 {summary["本批单题平均墙钟秒"]} 秒，其中模型调用 {summary["本批单题平均模型秒"]} 秒；'
              f'token 输入 {average["输入tokens"]:,.0f}｜输出 {average["输出tokens"]:,.0f}'
              f'（思考 {average["思考tokens"]:,.0f}）', flush=True)
    else:
        print('本批没有待处理题目（抽样范围内均已完成），仅输出累计口径。', flush=True)
    print(f'累计（含历史断点，{summary["累计题数（有审计记录）"]}/{summary["抽样总题数"]} 题）：'
          f'输入 {total["输入tokens"]:,}｜输出 {total["输出tokens"]:,}（思考 {total["思考tokens"]:,}）'
          f'｜合计 {total["总tokens"]:,}，模型耗时 {format_duration(summary["累计模型耗时秒"])}', flush=True)
    print(bar, flush=True)


def normalize_physics_exam_point(tag):
    """返回考点的比较内容；仅忽略官方格式中的外层【】。"""
    tag = str(tag).strip()
    if tag.startswith('【') and tag.endswith('】'):
        return tag[1:-1].strip()
    return tag


def parse_result(raw, catalog):
    """解析模型 JSON，并用标签全集过滤未知、重复或跨维度标签。"""
    clean = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw.strip(), flags=re.I | re.S)
    parsed = json.loads(clean)
    if not isinstance(parsed, dict):
        raise ValueError('模型输出不是 JSON 对象')
    tags = {}
    unknown = []
    for dim, allowed in catalog.items():
        source = parsed.get(dim, [])
        if not isinstance(source, list):
            source = [source] if source else []
        # 物理考点的官方格式带【】。复核只比较标签内容，命中后始终采用
        # 标签表中的标准原文；不进行同义词、缩写或模糊匹配。
        exam_point_lookup = ({normalize_physics_exam_point(item): item for item in allowed}
                             if dim == '考点' else {})
        vals = []
        for tag in source:
            tag = str(tag).strip()
            standard_tag = exam_point_lookup.get(normalize_physics_exam_point(tag)) if dim == '考点' else tag
            if standard_tag in allowed and standard_tag not in vals:
                vals.append(standard_tag)
            elif tag:
                unknown.append(f'{dim}: {tag}')
        tags[dim] = vals
    remarks = parsed.get('备注', [])
    if not isinstance(remarks, list):
        remarks = [remarks]
    return tags, [str(x) for x in remarks if x], unknown


def output_excel(path, rows, dims):
    wb = Workbook()
    ws = wb.active
    ws.title = '大题标签'
    columns = ['大题ID', '小题ID', '题干图片', '解析图片', '答案图片', *dims, '备注', '状态', '错误信息',
               '耗时秒', '输入tokens', '输出tokens', '思考tokens']
    ws.append(columns)
    for c in ws[1]:
        c.fill = PatternFill('solid', fgColor='23544B')
        c.font = Font(color='FFFFFF', bold=True)
    for row in rows:
        ws.append([row.get(k, '') for k in columns])
    ws.freeze_panes = 'A2'
    ws.auto_filter.ref = ws.dimensions
    for col in ws.columns:
        ws.column_dimensions[col[0].column_letter].width = min(62, max(18, len(str(col[0].value)) * 2 + 4))
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(vertical='top', wrap_text=True)
    wb.save(path)


def output_review(path, records, dims):
    # HTML 用本地题目文字与图片；在离线 file:// 环境中也可以打开。
    path.parent.mkdir(parents=True, exist_ok=True)
    # 审计 JSONL 记录每道大题实际发送的完整提示词和未经处理的模型输出。
    # 以大题 ID 关联到复核页，避免把体积较大的文本重复存入断点文件。
    audit_path = path.parent.parent / '模型审计.jsonl'
    audit_by_id = {}
    if audit_path.exists():
        with audit_path.open(encoding='utf-8') as audit_file:
            for line in audit_file:
                if line.strip():
                    item = json.loads(line)
                    audit_by_id[str(item['大题ID'])] = item
    records = [{**r, 'images': [{k: v for k, v in image.items() if k != 'data_url'} for image in r.get('images', [])],
                'model_prompt': audit_by_id.get(str(r['id']), {}).get('模型输入', ''),
                'model_raw': audit_by_id.get(str(r['id']), {}).get('模型输出', ''),
                'model_meta': audit_by_id.get(str(r['id']), {}).get('模型元数据', {}),
                'model_name': audit_by_id.get(str(r['id']), {}).get('模型', '')}
               for r in records]
    data = json.dumps(records, ensure_ascii=False).replace('</', '<\\/')
    page = r'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>复合题大题级人工复核</title><style>body{margin:0;background:#f5f7f4;color:#22342e;font:14px/1.6 system-ui,'Microsoft YaHei',sans-serif}header{padding:20px;background:#193b30;color:white;position:sticky;top:0}header input{padding:8px;width:260px}main{max-width:1250px;margin:auto;padding:16px}article{background:white;border:1px solid #d9e2d9;padding:18px;margin:18px 0;border-radius:8px}.q{padding:12px;border:1px solid #d9e2d9;margin:10px 0}p{white-space:pre-wrap}img{max-width:460px;max-height:380px;margin:8px;border:1px solid #ddd}table{border-collapse:collapse;width:100%}th,td{padding:8px;border:1px solid #d9e2d9;text-align:left;vertical-align:top}th{width:190px;background:#eff5ef}details{margin:10px 0}summary{cursor:pointer}textarea{width:98%;min-height:60px}pre{white-space:pre-wrap;overflow-wrap:anywhere;max-height:520px;overflow:auto;background:#f1f5f1;padding:12px;border-radius:6px}button{padding:9px;cursor:pointer}</style><header><h2>__TITLE__ · 复合题大题级人工复核</h2><input id="search" placeholder="搜索大题 ID 或题干"> <button id="export">导出人工复核意见</button></header><main><p>每张大题卡包含全部小题。人工填写后请在离开页面前导出 JSON。</p><div id="app"></div></main><script>const DATA=__DATA__,DIMS=__DIMS__,saved={};const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));function imgHtml(im,label){return im.local?`<a href="${esc(im.url)}" target="_blank"><img loading="lazy" src="${esc(im.local)}" alt="${label}图片"></a>`:`<span>图片下载失败：${esc(im.error||'未知原因')}</span>`}function fieldHtml(q,k,label){const imgs=(q.fieldImages||{})[k]||[];const parts=String(q[k]??'').split('<image>');let out='';if(parts.length===1){out+=esc(parts[0]);imgs.forEach(im=>{out+=imgHtml(im,label)})}else{parts.forEach((seg,i)=>{out+=esc(seg);if(i<imgs.length){out+=imgHtml(imgs[i],label)}});for(let i=parts.length-1;i<imgs.length;i++){out+=imgHtml(imgs[i],label)}}return `<p><b>${label}：</b>${out||'—'}</p>`}function collect(){document.querySelectorAll('article[data-id]').forEach(card=>saved[card.dataset.id]={decision:card.querySelector('select').value,note:card.querySelector('textarea').value})}function render(){collect();let term=document.querySelector('#search').value.trim().toLowerCase();document.querySelector('#app').innerHTML=DATA.filter(r=>!term||JSON.stringify(r.questions).toLowerCase().includes(term)||r.id.includes(term)).map(r=>`<article data-id="${esc(r.id)}"><h2>大题 ${esc(r.id)} · ${r.questions.length} 部分 · ${esc(r.status)}</h2><details open><summary>完整原题与图片</summary>${r.questions.map((q,i)=>`<div class="q"><b>第 ${i+1} 部分：${esc(q.questionId)}</b><p>题型：${esc(q.businessTypeName)} / ${esc(q.structureTypeName)}</p>${['stem','options','answer','analysis'].map((k,j)=>fieldHtml(q,k,['题干','选项','答案','解析'][j])).join('')}</div>`).join('')}${r.questions.some(q=>q.fieldImages)?'':r.images.map(x=>x.local?`<a href="${esc(x.url)}" target="_blank"><img loading="lazy" src="${esc(x.local)}" alt="题目图片"></a>`:`<span>图片下载失败：${esc(x.error)}</span>`).join('')}</details><h3>大题级标签</h3><table>${DIMS.map(d=>`<tr><th>${esc(d)}</th><td>${esc((r.tags[d]||[]).join('｜'))||'—'}</td></tr>`).join('')}</table><p><b>备注：</b>${esc(r.remarks.join('；'))||'—'}　<b>错误：</b>${esc(r.errors.join('；'))||'—'}</p><p><b>耗时：</b>${r.elapsed??'—'} 秒　<b>tokens：</b>入 ${(r.usage||{})['输入tokens']??'—'}／出 ${(r.usage||{})['输出tokens']??'—'}（思考 ${(r.usage||{})['思考tokens']??'—'}）</p><details><summary>查看发送的提示词（${esc(r.model_name||'未记录模型')}）</summary><pre>${esc(r.model_prompt)||'审计记录未找到提示词'}</pre></details><details><summary>查看模型原始输出</summary><p><b>模型调用信息：</b>${esc(JSON.stringify(r.model_meta||{}))}</p><pre>${esc(r.model_raw)||'审计记录未找到模型输出'}</pre></details><label>人工结论 <select><option value="">未复核</option>${['准确','部分准确','不准确','无法判断'].map(v=>`<option ${saved[r.id]?.decision===v?'selected':''}>${v}</option>`).join('')}</select></label><p><textarea placeholder="记录应修改的标签及依据">${esc(saved[r.id]?.note||'')}</textarea></p></article>`).join('')};document.querySelector('#search').oninput=render;document.querySelector('#export').onclick=()=>{collect();let blob=new Blob([JSON.stringify(DATA.map(r=>({大题ID:r.id,...(saved[r.id]||{})})),null,2)],{type:'application/json'}),a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='人工复核意见.json';a.click()};render()</script>'''
    page = page.replace('__TITLE__', html.escape(path.parent.parent.name)).replace('__DIMS__', json.dumps(dims, ensure_ascii=False)).replace('__DATA__', data)
    path.write_text(page, encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('subject', choices=CONFIG)
    parser.add_argument('--limit', type=int, default=200)
    parser.add_argument('--workers', type=int, default=3, help='同一学科的并发题数，默认 3')
    parser.add_argument('--max-new', type=int, help='每次最多处理多少道待处理题，方便分批断点续跑')
    parser.add_argument('--retry-missing-images', action='store_true', help='重新处理图片缺失的成功记录')
    parser.add_argument('--compound-id', help='指定必须包含在样本中的复合题大题 ID')
    parser.add_argument('--model', default='deepseek-v4.1-flash')
    parser.add_argument('--timeout', type=int, default=300)
    parser.add_argument('--retries', type=int, default=1)
    parser.add_argument('--output-dir', type=Path)
    parser.add_argument('--input', type=Path, help='题目 ID Excel/txt 路径，或含 questions_map 的 JSONL 题目文件；默认使用学科目录中的题目 ID 文件')
    args = parser.parse_args()
    if args.limit <= 0 or args.workers <= 0 or (args.max_new is not None and args.max_new <= 0):
        raise ValueError('--limit、--workers 和 --max-new 必须大于 0')
    conf = CONFIG[args.subject]
    load_env_file(MATH / '.env')
    token = os.getenv('MENSHEN_TOKEN', '').strip()
    if not token:
        raise RuntimeError('MENSHEN_TOKEN 缺失')
    catalog, guidance = label_catalog(args.subject)
    input_file = args.input or (conf['directory'] / conf['ids'])
    if not input_file.is_file():
        raise FileNotFoundError(f'题目 ID 文件不存在：{input_file}')
    local_questions = {}
    if input_file.suffix.lower() == '.jsonl':
        # JSONL 自带小题全文与图片，直接本地取题，不再请求题库接口。
        ids, local_questions = load_jsonl_compound_questions(input_file)
    else:
        ids = load_question_ids(input_file)
    # 默认只取前 limit 道作抽样；全量执行时需显式传入不小于 ID 总数的 --limit。
    selected = ids[:args.limit]
    if args.compound_id:
        if args.compound_id not in ids:
            raise RuntimeError('指定的复合题 ID 不在题目 ID 表中')
        if args.compound_id not in selected:
            selected[-1] = args.compound_id
    folder = args.output_dir or conf['directory'] / 'outputs' / ('复合题打标测试_' + datetime.now().strftime('%Y%m%d_%H%M%S'))
    folder.mkdir(parents=True, exist_ok=True)
    if any(qid not in local_questions for qid in selected):
        keys = {k: os.getenv(v, '') for k, v in [('appCode','TIKU_APP_CODE'),('businessCode','TIKU_BUSINESS_CODE'),('appToken','TIKU_APP_TOKEN')]}
        if any(not v for v in keys.values()):
            raise RuntimeError('题库接口环境变量缺失')
        timestamp = os.getenv('TIKU_TIMESTAMP', '')
        if not timestamp:
            raise RuntimeError('当前题库账号需 .env 中的 TIKU_TIMESTAMP')
    else:
        keys, timestamp = {}, ''
    selection_file = folder / '抽样题目ID.json'
    if selection_file.exists() and json.loads(selection_file.read_text(encoding='utf-8')) != selected:
        raise RuntimeError('该输出目录属于不同题目列表，请另选输出目录，避免断点结果混淆')
    selection_file.write_text(json.dumps(selected, ensure_ascii=False, indent=2), encoding='utf-8')
    checkpoint = folder / '中间结果.json'
    results = json.loads(checkpoint.read_text(encoding='utf-8')) if checkpoint.exists() else {}
    audit = folder / '模型审计.jsonl'
    run_started_at = datetime.now()
    run_started = time.perf_counter()
    batch_usage = empty_usage()
    batch_elapsed = 0.0
    batch_model_elapsed = 0.0
    batch_status = OrderedDict()

    def process(qid):
        # requests.Session 不跨线程共享；每道题独立取题、下载图片并调用模型。
        # 函数只返回结果，主线程负责落盘，避免并发写入 JSONL/断点文件造成文件损坏。
        question_started = time.perf_counter()
        session = requests.Session()
        if qid in local_questions:
            payload, questions, fetch_errors = None, local_questions[qid], []
        else:
            payload, questions, fetch_errors = fetch_questions(session, 'https://tps-tiku-api.staff.xdf.cn/api/question/v2/query-by-questionIds',
                                                                keys, qid, (10, args.timeout), args.retries, timestamp)
        prompt = ''
        images = []
        raw = ''
        meta = {}
        errors = list(fetch_errors)
        tags = {d: [] for d in conf['dimensions']}
        remarks = []
        status = '失败'
        if questions:
            prompt = make_prompt(args.subject, qid, questions, catalog, guidance)
            images = create_images(session, image_urls(questions), folder / 'images' / qid, min(args.timeout, 30))
            errors += [f"图片：{x['url']} {x['error']}" for x in images if x.get('error')]
            raw, call_errors, meta = model_call(session, prompt, images, args.model, token, args.timeout, args.retries)
            errors += call_errors
            if raw:
                try:
                    tags, remarks, unknown = parse_result(raw, catalog)
                    if unknown:
                        remarks += ['未采用标签：' + v for v in unknown]
                    if meta.get('finish_reason') == 'length':
                        raise ValueError('模型响应被 max_tokens 截断')
                    status = '成功（图片缺失）' if any(x.get('error') for x in images) else '成功'
                    if args.subject == 'chemistry':
                        exceeded = [f'{dim}: {len(tags[dim])}>{limit}' for dim, limit in CHEM_MAX_TAGS.items()
                                    if len(tags[dim]) > limit]
                        if exceeded:
                            remarks.append('超出旧版标签数量上限，需人工选择：' + '；'.join(exceeded))
                            status = '成功（需人工复核）'
                except Exception as e:
                    errors.append(f'模型结果解析失败：{type(e).__name__}: {e}')
        else:
            errors.append('题库接口/本地文件没有返回大题和小题')
        img_fields = {field: unique(url for q in questions for url in q.get('fieldImages', {}).get(field, []))
                      for field in ('stem', 'analysis', 'answer')}
        elapsed = round(time.perf_counter() - question_started, 2)
        usage = extract_usage(meta)
        row = {'大题ID': qid, '小题ID': '|'.join(str(q['questionId']) for q in questions),
               '题干图片': '\n'.join(img_fields['stem']), '解析图片': '\n'.join(img_fields['analysis']),
               '答案图片': '\n'.join(img_fields['answer']), **{d: '|'.join(tags[d]) for d in conf['dimensions']},
               '耗时秒': elapsed, '输入tokens': usage['输入tokens'], '输出tokens': usage['输出tokens'],
               '思考tokens': usage['思考tokens'],
               '备注': '\n'.join(remarks), '状态': status, '错误信息': '\n'.join(errors)}
        image_by_url = {item['url']: item for item in images}

        def question_view(q):
            view = {k: q.get(k, '') for k in ('questionId','businessTypeName','structureTypeName','stem','options','answer','analysis','knowledge','solution')}
            # 每个字段的图片附上下载结果（本地路径/失败原因），复核页按字段内联显示。
            view['fieldImages'] = {field: [{k2: v2 for k2, v2 in image_by_url.get(url, {'url': url, 'error': '未下载'}).items() if k2 != 'data_url'}
                                           for url in q.get('fieldImages', {}).get(field, [])]
                                   for field in ('stem', 'options', 'answer', 'analysis')}
            return view

        record = {'id': qid, 'questions': [question_view(q) for q in questions],
                  'tags': tags, 'remarks': remarks, 'status': status, 'errors': errors,
                  'elapsed': elapsed, 'usage': usage,
                  'images': [{k: v for k, v in item.items() if k != 'data_url'} for item in images], 'row': row}
        session.close()
        audit_record = {'大题ID': qid, '题目来源': '本地JSONL' if qid in local_questions else '题库接口',
                        '题库错误': fetch_errors, '题目': record['questions'],
                        '图片': record['images'], '模型': args.model, '模型输入': prompt, '模型输出': raw,
                        '模型元数据': meta, '耗时秒': elapsed, 'token用量': usage,
                        '标签': tags, '备注': remarks, '状态': status, '错误': errors}
        return record, audit_record, len(questions), meta.get('sent_images', 0)

    # 先处理从未完成的题，再重试失败题；可选地补跑“成功但图片缺失”的记录。
    pending = [qid for qid in selected if qid not in results]
    # 已失败题目放到末尾重试，避免每批都占用新题处理名额。
    pending += [qid for qid in selected if qid in results and not str(results[qid].get('status', '')).startswith('成功')]
    if args.retry_missing_images:
        pending += [qid for qid in selected if qid in results and results[qid].get('status') == '成功（图片缺失）']
    if args.max_new is not None:
        pending = pending[:args.max_new]
    print(f'{args.subject}：共 {len(selected)} 道，本批待处理 {len(pending)} 道，并发 {args.workers}', flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {executor.submit(process, qid): qid for qid in pending}
        for future in as_completed(futures):
            qid = futures[future]
            try:
                record, audit_record, part_count, sent_images = future.result()
            except Exception as exc:
                error = f'处理异常：{type(exc).__name__}: {exc}'
                row = {'大题ID': qid, '状态': '失败', '错误信息': error}
                record = {'id': qid, 'questions': [], 'tags': {d: [] for d in conf['dimensions']},
                          'remarks': [], 'images': [], 'status': '失败', 'errors': [error], 'row': row}
                audit_record = {'大题ID': qid, '状态': '失败', '错误': [error]}
                part_count = sent_images = 0
            usage = record.get('usage') or empty_usage()
            elapsed = float(record.get('elapsed') or 0)
            batch_usage = add_usage(batch_usage, usage)
            batch_elapsed += elapsed
            batch_model_elapsed += float((audit_record.get('模型元数据') or {}).get('模型耗时秒') or 0)
            batch_status[record['status']] = batch_status.get(record['status'], 0) + 1
            results[qid] = record
            with audit.open('a', encoding='utf-8') as f:
                f.write(json.dumps(audit_record, ensure_ascii=False) + '\n')
            temp = checkpoint.with_suffix('.tmp')
            temp.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
            temp.replace(checkpoint)
            completed = sum(q in results for q in selected)
            print(f'[{args.subject} {completed}/{len(selected)}] {qid} -> {record["status"]}，'
                  f'部分数={part_count}，已发送图片={sent_images}，错误={len(record["errors"])}，'
                  f'耗时={elapsed:.1f}s，tokens=入{usage["输入tokens"]}/出{usage["输出tokens"]}（思考{usage["思考tokens"]}）', flush=True)
            if completed % 10 == 0:
                output_excel(folder / '大题级标签.xlsx', [results[i]['row'] for i in selected if i in results], conf['dimensions'])
                output_review(folder / 'review' / '人工复核.html', [results[i] for i in selected if i in results], conf['dimensions'])
    # 断点续跑时即便所有题目已完成，也重新生成页面，确保页面模板更新生效。
    output_excel(folder / '大题级标签.xlsx', [results[i]['row'] for i in selected if i in results], conf['dimensions'])
    output_review(folder / 'review' / '人工复核.html', [results[i] for i in selected if i in results], conf['dimensions'])
    # 汇总本次运行的耗时与 token；累计口径按大题 ID 去重后从审计文件回算，断点续跑不会重复计数。
    wall_seconds = time.perf_counter() - run_started
    per_id = audit_usage_by_id(audit)
    cumulative_usage = empty_usage()
    cumulative_model_seconds = 0.0
    for qid in selected:
        if qid in per_id:
            cumulative_usage = add_usage(cumulative_usage, per_id[qid][0])
            cumulative_model_seconds += per_id[qid][1]
    processed = len(pending)
    summary = OrderedDict([
        ('学科', args.subject), ('模型', args.model), ('并发数', args.workers),
        ('开始时间', run_started_at.strftime('%Y-%m-%d %H:%M:%S')),
        ('结束时间', datetime.now().strftime('%Y-%m-%d %H:%M:%S')),
        ('墙钟耗时秒', round(wall_seconds, 1)), ('墙钟耗时', format_duration(wall_seconds)),
        ('本批处理题数', processed), ('本批状态分布', dict(batch_status)),
        ('本批单题平均墙钟秒', round(batch_elapsed / processed, 1) if processed else 0),
        ('本批单题平均模型秒', round(batch_model_elapsed / processed, 1) if processed else 0),
        ('本批token', batch_usage),
        ('本批单题平均token', {k: round(v / processed, 1) for k, v in batch_usage.items()} if processed else empty_usage()),
        ('抽样总题数', len(selected)),
        ('累计已完成题数', sum(1 for q in selected if q in results)),
        ('累计题数（有审计记录）', sum(1 for q in selected if q in per_id)),
        ('累计token', cumulative_usage),
        ('累计模型耗时秒', round(cumulative_model_seconds, 1)),
    ])
    stats_file = folder / '运行统计.json'
    history = []
    if stats_file.exists():
        try:
            previous = json.loads(stats_file.read_text(encoding='utf-8'))
            history = previous.get('历次运行', []) if isinstance(previous, dict) else list(previous)
        except (json.JSONDecodeError, OSError):
            history = []
    history.append(summary)
    stats_file.write_text(json.dumps({'当前汇总': summary, '历次运行': history}, ensure_ascii=False, indent=2),
                          encoding='utf-8')
    print_summary(summary)
    print('运行统计：', stats_file, flush=True)
    print('结果目录：', folder, flush=True)


if __name__ == '__main__':
    main()
