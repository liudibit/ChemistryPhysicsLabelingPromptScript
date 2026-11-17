import asyncio
import aiohttp
import aiofiles
import base64
import json
import mimetypes
import os
import random
import re
import multiprocessing
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import *
from xml.etree import ElementTree
from tqdm import tqdm
from asyncio import Semaphore
import time
import math
import orjson

leaf_name_to_id = {}
id_to_leaf_name = {}
label_type_by_name = {}
leaf_name_to_explanation = {}
explanation_references_by_name = {}

# ===== 日常修改导航 =====
# 1. 改标注规则：「提示词（精简版）」一节的 PROMPT_RULES_TEXT、PROMPT_OUTPUT_TEXT（与业务规则文档保持一致）。
# 2. 改候选展示：CANDIDATE_ITEM_TEMPLATE（只有名称和释义，不加编号）。
# 3. 改候选来源：文件底部「配置区」的 candidate_source_fields；T1/T2 配对表 CONFIRMED_T1_T2_PAIRS。
# 4. 改手动补标签/路径/并发/上下文预算：文件底部「配置区」。
# 5. 改自动补漏条件：build_candidate_hints；不需要时在配置区关掉即可。
# analysis 三个标题与 parse_rerank_result 中的 headings 保持一致。


# 修改每个候选的展示只需改这里；可用变量：label、explanation。
# 说明：解释中的 differences_from_other_examination_points（边界及与其他标签区别）
# 里用「」写出的标签，同候选标题、提示词规则中的「」一样都是正式可选名称；
# 三类都可以选，不要求边界标签重复成为候选标题。
CANDIDATE_ITEM_TEMPLATE = "### {label}\n{explanation}"

# 业务规则第 7 节已确认的 T1/T2 对：(T1 列表, T2 列表)。候选池里出现任一方时补入另一方。
# “实验探究-方案设计与评价”按题型只补对应的 T2，见 pair_partners_for。
CONFIRMED_T1_T2_PAIRS = (
    (("物质变化-物理与化学变化辨析", "物质性质-物理与化学性质的概念及辨析"), ("【T2】物理变化、化学变化与性质",)),
    (("化学方程式-配平",), ("【T2】化学方程式-配平",)),
    (("计算-化学式-元素质量分数",), ("【T2】计算-化学式-纯净物的元素质量分数",)),
    (("计算-化学式-混合物中元素质量",), ("【T2】计算-化学式-混合物的元素质量分数",)),
    (("计算-化学方程式-纯净物",), ("【T2】计算-化学方程式-纯净物",)),
    (("计算-化学方程式-溶液",), ("【T2】计算-化学方程式-溶液",)),
    (("实验探究-金属活动性验证(大题)",), ("【T2】科学探究题-金属活动性(大题)",)),
    (("金属化学性质-活动性顺序及其应用",), ("【T2】金属活动性",)),
    (("实验探究-方案设计与评价",), ("【T2】实验-设计与操作-科学探究题", "【T2】科学探究环节-实验评价", "【T2】实验-设计与评价-选择题")),
    (("实验探究-物质成分的定性鉴定、定量测定(大题)",), ("【T2】科学探究题-食品包装与保鲜成分(大题)",)),
    (("微观解释-反应示意图分析",), ("【T2】微观示意图分析",)),
    (("微观粒子-原子、离子结构示意图",), ("【T2】原子、离子结构示意图",)),
    (("实验-压强变化分析",), ("【T2】实验-压强变化分析",)),
    (("图像-金属与酸反应",), ("【T2】图像-金属与酸反应",)),
)
CHOICE_ONLY_T2 = {"【T2】实验-设计与评价-选择题"}
NON_CHOICE_T2 = {"【T2】实验-设计与操作-科学探究题", "【T2】科学探究环节-实验评价"}
# 这两个 T2 只是“特定情境 + 父标签”，由 T2 反推 T1 不可靠，只从 T1 补 T2。
ONE_WAY_T2 = {"【T2】科学探究题-食品包装与保鲜成分(大题)"}


def is_choice_question(item: Dict[str, Any]) -> bool:
    """当前标注范围是否为选择题：小问看小问自身，整题看总题干。"""
    questions = {str(key): value for key, value in item.get("questions_map", {}).items()}
    source = questions.get(str(item.get("question_id"))) or questions.get(str(item.get("parent_id"))) or {}
    return str(source.get("structure_type") or "") in {"danxuan", "duoxuan"}


def pair_partners_for(item: Dict[str, Any], pool_names: List[str]) -> List[Tuple[str, str]]:
    """返回 (要补入的标签, 触发它的已有标签)；只补一层，不沿配对连锁扩展。"""
    pool = set(pool_names)
    choice = is_choice_question(item)
    partners = []
    for t1_names, t2_names in CONFIRMED_T1_T2_PAIRS:
        allowed_t2 = [
            name for name in t2_names
            if not (choice and name in NON_CHOICE_T2) and not (not choice and name in CHOICE_ONLY_T2)
        ]
        for t1 in t1_names:
            if t1 in pool:
                partners += [(t2, t1) for t2 in allowed_t2 if t2 not in pool]
        for t2 in allowed_t2:
            # 一个 T2 对多个 T1（物理/化学变化与性质）时，由 T2 反推不知道该补哪一个，不反推。
            if t2 in pool and t2 not in ONE_WAY_T2 and len(t1_names) == 1:
                partners += [(t1, t2) for t1 in t1_names if t1 not in pool]
    return list(dict.fromkeys(partners))

def _parse_jsonl_worker(lines_chunk: List[bytes]) -> List[Dict[str, Any]]:
    """
    子进程工作函数：接收字节行列表，解析为 JSON 对象。
    遇到乱码或截断导致解析失败的行，直接跳过。
    """
    parsed_items = []
    for line in lines_chunk:
        stripped_line = line.strip()
        if not stripped_line:
            continue
        try:
            # orjson 原生支持解析 bytes
            parsed_items.append(orjson.loads(stripped_line))
        except orjson.JSONDecodeError:
            # 捕获 orjson 的解析错误（如磁盘满导致的乱码/截断），直接丢弃该行
            continue
    return parsed_items

def read_json_and_jsonl(input_path: str, num_workers: int = 64) -> List[Dict[str, Any]]:
    """
    快速读取 JSON 或 JSONL 文件（多进程版本）。
    利用大内存优势，直接以 rb 模式读取整个文件内容，避免 utf-8 解码报错。
    对于 JSONL，使用多进程并发解析，并根据数据量动态调整进程数。
    """
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"输入文件不存在: {input_path}")

    if num_workers is None:
        num_workers = os.cpu_count() or 1

    print(f"正在将文件 '{input_path}' 读入内存...")
    
    # 核心优势：rb 模式读取，绝对不会报 UnicodeDecodeError
    with open(input_path, "rb") as f:
        content = f.read()

    data = []
    if input_path.endswith(".json"):
        try:
            parsed_json = orjson.loads(content)
            data = [parsed_json] if isinstance(parsed_json, dict) else parsed_json
        except orjson.JSONDecodeError as e:
            print(f"JSON文件整体解析错误（可能已损坏） {input_path}: {str(e)}")
            return []
    else:
        # 如果是 .jsonl 文件，按行分割并使用多进程
        lines = content.splitlines()
        total_lines = len(lines)
        
        if total_lines == 0:
            return []

        # 设定每个进程至少需要处理的行数，避免小文件开太多进程导致开销过大
        min_lines_per_worker = 10000 
        
        # 计算理论上需要的进程数（至少为 1）
        needed_workers = max(1, math.ceil(total_lines / min_lines_per_worker))
        
        # 实际使用的进程数：不能超过用户设定的最大进程数，也不能超过总行数
        actual_workers = min(num_workers, needed_workers, total_lines)
        
        print(f"文件读取完成，共 {total_lines:,} 行。动态调整并发数：分配给 {actual_workers} 个进程并发解析...")

        # 将字节行列表切分成块
        chunk_size = (total_lines + actual_workers - 1) // actual_workers
        line_chunks = [lines[i * chunk_size : (i + 1) * chunk_size] for i in range(actual_workers)]

        del lines 

        # 如果只有 1 个进程，直接在主进程执行，省去多进程开销
        if actual_workers == 1:
            data = _parse_jsonl_worker(line_chunks[0])
        else:
            with ProcessPoolExecutor(max_workers=actual_workers) as executor:
                # 提交所有解析任务
                futures = [executor.submit(_parse_jsonl_worker, chunk) for chunk in line_chunks]
                
                # 使用 tqdm 显示多进程收集进度
                for future in tqdm(as_completed(futures), total=len(futures), desc="多进程解析进度"):
                    data.extend(future.result())

    print(f"解析完成！成功读取了 {len(data):,} 条有效数据。")
    return data


def write_json_and_jsonl(data, output_path):
    """
    快速将数据写入 JSON 或 JSONL 文件。
    JSONL 逐条序列化写入，避免创建整份数据的内存缓冲。
    """
    print(f"要写入{len(data)}条数据到{output_path}")

    # 确保目录存在
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    if output_path.endswith(".json"):
        json_bytes = orjson.dumps(data, option=orjson.OPT_INDENT_2)

        with open(output_path, "wb") as f:
            f.write(json_bytes)

    else:
        with open(output_path, "wb") as f:
            for item in tqdm(data, mininterval=1.0, desc="Serializing"):
                f.write(orjson.dumps(item, option=orjson.OPT_APPEND_NEWLINE))

    print(f"写入了{len(data)}条数据到{output_path}")

def _extract_last_result_xml(text: str) -> str:
    """最终回答须只有一个result块；不从思考文本或多个结果中猜选答案。"""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("模型输出为空")

    clean_text = text.strip()
    clean_text = re.sub(r"^```(?:xml)?\s*", "", clean_text, flags=re.IGNORECASE)
    clean_text = re.sub(r"\s*```$", "", clean_text)
    if not re.fullmatch(r"<result>.*</result>", clean_text, flags=re.DOTALL):
        raise ValueError("最终回答必须是完整且唯一的<result> XML块")
    return clean_text


def parse_rerank_result(
    text: str,
    label_field_name: str,
    analysis_field_name: str,
) -> Dict[str, Any]:
    """解析精标XML；校验目录合法性，允许贴切的T2-only结果。"""
    xml_text = _extract_last_result_xml(text)
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as exc:
        raise ValueError(f"模型XML解析失败: {exc}") from exc
    if root.tag.lower() != "result":
        raise ValueError("模型XML根节点必须是result")
    if [child.tag for child in root] != ["analysis", "labels"]:
        raise ValueError("result必须依次包含唯一analysis和labels，不能有其他节点")

    analysis_element = root.find("analysis")
    labels_element = root.find("labels")
    if analysis_element is None or labels_element is None:
        raise ValueError("模型XML必须同时包含analysis和labels节点")
    if len(analysis_element) or len(labels_element):
        raise ValueError("analysis和labels内部只能有文本，不能嵌套XML节点")
    analysis = "".join(analysis_element.itertext()).strip()
    if not analysis:
        raise ValueError("模型未返回有效analysis")
    headings = ("【题目总览】", "【得分点分析】", "【最终决策】")
    positions = [analysis.find(heading) for heading in headings]
    if any(analysis.count(heading) != 1 for heading in headings) or positions != sorted(positions):
        raise ValueError("analysis必须依次包含三个标注标题")

    raw_labels = "".join(labels_element.itertext()).strip()
    if not raw_labels:
        raise ValueError("模型未返回有效labels")
    if raw_labels == "无":
        return {
            analysis_field_name: analysis,
            label_field_name: [],
            f"{label_field_name}_id": [],
        }

    labels = [part.strip() for part in re.split(r"[;；]", raw_labels) if part.strip()]
    labels = list(dict.fromkeys(labels))
    if not labels:
        raise ValueError("模型精排标签为空")
    if len(labels) > CONFIG["max_output_labels"]:
        raise ValueError(f"输出标签数{len(labels)}超过上限{CONFIG['max_output_labels']}")

    unknown = [label for label in labels if label not in leaf_name_to_id]
    forbidden = [label for label in labels if "(勿打)" in label or "（勿打）" in label]
    if unknown or forbidden:
        raise ValueError(f"未知标签={unknown}；禁用标签={forbidden}")
    labels = [label for label in labels if label_type_by_name[label] == "T1"] + [
        label for label in labels if label_type_by_name[label] == "T2"
    ]
    return {
        analysis_field_name: analysis.strip(),
        label_field_name: labels,
        f"{label_field_name}_id": [leaf_name_to_id[label] for label in labels],
    }


def load_tree(tree_path: str):
    global result_list, leaf_name_to_id, id_to_leaf_name, new_name_to_old_name, old_name_to_new_name, old_leaf_name_to_id
    global label_type_by_name
    label_type_by_name = {}
    leaf_name_to_id = {}
    id_to_leaf_name = {}
    new_name_to_old_name = {}
    old_name_to_new_name = {}
    old_leaf_name_to_id = {}

    with open(tree_path, encoding="utf-8") as handle:
        tree_dict = [json.loads(line) for line in handle if line.strip()]
    if not tree_dict:
        raise ValueError("标签目录为空")

    for item in tree_dict:
        key = str(item['knw_id'])
        leaf_name = item['new_name']
        label_type = item['label_type']
        if label_type == "T2" and not leaf_name.startswith("【T2】"):
            leaf_name = "【T2】" + leaf_name
        if key in id_to_leaf_name or leaf_name in leaf_name_to_id:
            raise ValueError(f"标签ID或名称重复: {key} / {leaf_name}")
        if label_type not in {"T1", "T2"} or leaf_name.startswith("【T2】") != (label_type == "T2"):
            raise ValueError(f"标签类型或T2前缀不一致: {leaf_name}")
        if label_type == "T1" and leaf_name.startswith(("T1", "【T1】")):
            raise ValueError(f"T1名称不应包含类型前缀: {leaf_name}")
        label_type_by_name[leaf_name] = label_type
        old_name = '@'.join(item.get('old_name', leaf_name).split('@')[-2:])

        new_name_to_old_name[leaf_name] = old_name
        old_name_to_new_name[old_name] = leaf_name

        leaf_name_to_id[leaf_name] = key
        id_to_leaf_name[key] = leaf_name
        
        old_leaf_name_to_id[old_name] = key
        

    result_list = list(leaf_name_to_id.keys())
    return result_list, leaf_name_to_id, id_to_leaf_name, new_name_to_old_name, old_name_to_new_name, old_leaf_name_to_id


def load_explanations(explanations_path: str) -> Dict[str, str]:
    """加载解释文件；名称和ID必须与当前标签名称目录完全一致。"""
    global leaf_name_to_explanation
    explanation_name_by_id = {}
    leaf_name_to_explanation = {}
    field_titles = (
        ("core_concept", "核心与适用范围"),
        ("frequently_tested_scenarios", "常考场景"),
        ("differences_from_other_examination_points", "边界及与其他标签区别"),
        ("matters_needing_attention", "注意事项"),
        ("example_question", "例题"),
    )

    with open(explanations_path, encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            knw_id = str(row["knw_id"])
            name = row["new_name"]
            if knw_id in explanation_name_by_id or name in leaf_name_to_explanation:
                raise ValueError(f"解释文件ID或名称重复: {explanations_path}:{line_number}")
            explanation_name_by_id[knw_id] = name
            change = row.get("explanation_change") or {}
            parts = []
            for field_name, title in field_titles:
                if field_name not in CONFIG.get("explanation_fields", [f for f, _ in field_titles]):
                    continue
                value = str(change.get(field_name) or "").strip()
                if value:
                    parts.append(f"{title}：{value}")
            leaf_name_to_explanation[name] = "\n".join(parts) or "当前解释未填写。"

    if explanation_name_by_id != id_to_leaf_name:
        raise ValueError("解释文件与名称目录的knw_id/new_name不完全一致")
    rebuild_explanation_reference_index()
    return leaf_name_to_explanation


def rebuild_explanation_reference_index() -> None:
    """预计算直接引用；引用用于边界说明，不直接当作共标结论，也不递归扩展。"""
    global explanation_references_by_name
    explanation_references_by_name = {
        name: tuple(dict.fromkeys(
            reference for reference in re.findall(r"「([^」]+)」", explanation)
            if reference in leaf_name_to_id and reference != name
            and "(勿打)" not in reference and "（勿打）" not in reference
        ))
        for name, explanation in leaf_name_to_explanation.items()
    }


def _as_list(value: Any, field_name: str, question_id: Any) -> List[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise TypeError(
            f"候选字段必须是列表: question_id={question_id}, field={field_name}, "
            f"type={type(value).__name__}"
        )
    return value


def build_candidate_hints(item: Dict[str, Any]) -> List[Dict[str, str]]:
    """依据当前待标范围的明确文本证据补充候选，不扫描其他小问任务。"""
    if not CONFIG.get("enable_candidate_hints", True):
        return []

    questions = {str(key): value for key, value in item.get("questions_map", {}).items()}
    if str(item.get("question_id")) == str(item.get("parent_id")):
        focused_questions = list(questions.values())
    else:
        focused_questions = [questions.get(str(item.get("question_id")), {})]
    question_text = "\n".join(
        str(question.get(field) or "")
        for question in focused_questions
        for field in ("stem", "options", "analysis", "answer")
    ).lower().translate(str.maketrans("₀₁₂₃₄₅₆₇₈₉", "0123456789"))
    # 复杂标签的新增召回优先要求题面/选项有线索；解析只能辅助，不能单独触发。
    task_text = "\n".join(
        str(question.get(field) or "")
        for question in focused_questions
        for field in ("stem", "options")
    ).lower().translate(str.maketrans("₀₁₂₃₄₅₆₇₈₉", "0123456789"))

    hints: List[Dict[str, str]] = []
    seen = set()

    def evidence(*terms: str) -> List[str]:
        return [term for term in terms if term in question_text]

    def task_evidence(*terms: str) -> List[str]:
        return [term for term in terms if term in task_text]

    def append_candidate(
        label: str,
        condition: Any,
        signals: List[str],
        guidance: str,
    ) -> None:
        # 名称必须在当前目录中；不论条件是否命中都检查，改名后旧名称会在第一条数据上直接报错。
        if label not in leaf_name_to_id:
            raise ValueError(f"召回规则中的标签不在当前名称目录中: {label}")
        if condition and label not in seen:
            seen.add(label)
            hints.append(
                {
                    "label": label,
                    "evidence": "、".join(dict.fromkeys(signals)),
                    "guidance": guidance,
                }
            )

    writing = re.search(
        r"(?:写出|书写|补全|完成|填写).{0,40}(?:化学方程式|反应方程式)"
        r"|(?:化学方程式|反应方程式).{0,20}(?:为|是|__|＿|填写)",
        question_text,
        flags=re.DOTALL,
    )
    append_candidate(
        "化学方程式-书写、正误判断", writing, [writing.group(0)] if writing else [],
        "当前任务要求书写或补全化学方程式时必须保留，即使是强主题实验中的常规填空；"
        "若方程式仅作条件或仅在解析出现，不因此标书写。只问配平/文字表达式按专项标签判断。",
    )

    graph = evidence("曲线", "图像", "图象", "横坐标", "纵坐标", "横轴", "纵轴", "坐标图")
    solubility = evidence("溶解度")
    append_candidate(
        "溶解度-固体-曲线应用",
        graph and solubility,
        graph + solubility,
        "需实际使用固体溶解度曲线；气体曲线或仅定义溶解度不选。",
    )
    append_candidate(
        "【T2】溶解度曲线",
        graph and solubility,
        graph + solubility,
        "核对是否真正读图分析，不因背景出现曲线名称补标。",
    )

    # 以下图像、压强规则原先因旧名称一直未生效，改名后按题面和选项判断，不让解析里的词单独触发。
    task_graph_early = task_evidence("曲线", "图像", "图象", "横坐标", "纵坐标", "横轴", "纵轴", "坐标图")
    metals = task_evidence("金属", "镁", "锌", "铁", "铝")
    acid = task_evidence("盐酸", "硫酸", "与酸", "加酸")
    for label in ("图像-金属与酸反应", "【T2】图像-金属与酸反应"):
        append_candidate(
            label,
            task_graph_early and metals and acid,
            task_graph_early + metals + acid,
            "需利用反应曲线判断质量、速率或反应阶段；普通装置图不属于反应图像。",
        )
    ph_signal = task_evidence("ph")
    append_candidate(
        "【T2】图像-pH变化",
        task_graph_early and ph_signal,
        task_graph_early + ph_signal,
        "核对pH坐标曲线是否承担实际分析任务，单独pH数值不触发。",
    )

    sensor = task_evidence("传感器", "数字化")
    measured = task_evidence("压强", "浓度", "温度", "含量", "ph", "曲线")
    analysis_signal = evidence("分析", "判断", "说明", "推断", "比较", "曲线", "数据")
    pressure = task_evidence("压强", "气压")
    pressure_task = task_evidence("压强变化", "压强增大", "压强减小", "压强变大", "压强变小", "气压", "压强传感器", "压强曲线")
    for label in ("实验-压强变化分析", "【T2】实验-压强变化分析"):
        append_candidate(
            label,
            pressure and pressure_task,
            pressure + pressure_task,
            "实际分析装置内压强变化或压强传感器曲线才选；仅出现气球、液面变化而不分析压强不选。",
        )
    append_candidate(
        "【T2】实验-数字化实验",
        sensor and measured and analysis_signal,
        sensor + measured + analysis_signal,
        "实际分析传感器采集的数据或曲线才选；仅出现仪器名称不选。",
    )

    carbon = evidence("木炭", "炭粉", "碳粉")
    copper_oxide = evidence("氧化铜", "cuo")
    experiment = evidence("实验", "装置", "加热")
    append_candidate(
        "碳单质-实验-碳还原氧化铜",
        carbon and copper_oxide and experiment,
        carbon + copper_oxide + experiment,
        "得分点围绕该实验的现象、原理、装置或操作时选择；同一得分点的方程式书写、现象描述、计算、图像分析照常另标。",
    )
    carbon_monoxide = evidence("一氧化碳")
    if re.search(r"(?<![a-z])co(?![a-z0-9])", question_text):
        carbon_monoxide.append("CO")
    append_candidate(
        "碳氧化物-实验-CO还原氧化铜",
        carbon_monoxide and copper_oxide and experiment,
        carbon_monoxide + copper_oxide + experiment,
        "得分点围绕CO还原氧化铜实验时选择；CO2或碳粉不能触发；同一得分点的方程式书写、现象描述、计算、图像分析照常另标。",
    )

    crude_salt = evidence("粗盐")
    flow = evidence("流程", "步骤顺序", "加入顺序", "先后顺序", "→", "->")
    soluble = evidence("可溶性杂质", "氯化钡", "碳酸钠", "钡离子", "镁离子", "钙离子")
    physical = evidence("溶解", "过滤", "蒸发", "结晶")
    append_candidate(
        "粗盐提纯-可溶性杂质去除",
        crude_salt and soluble,
        crude_salt + soluble,
        "确认任务是去除可溶杂质，不因试剂名称只出现在干扰项就标。",
    )
    append_candidate(
        "【T2】流程题-粗盐可溶性杂质去除",
        crude_salt and soluble and flow,
        crude_salt + soluble + flow,
        "必须分析流程节点或衔接；孤立操作不补流程标签。",
    )
    append_candidate(
        "【T2】流程题-粗盐难溶性杂质去除与结晶",
        crude_salt and flow and len(physical) >= 2 and not soluble,
        crude_salt + flow + physical,
        "需实际分析难溶杂质去除与结晶流程，不能只凭粗盐背景选择。",
    )

    removal = evidence("除杂", "除去", "提纯", "净化")
    evaluation = evidence("方案", "达到目的", "达到预期", "能否", "合理", "可行")
    for label in ("实验探究-方案设计与评价", "实验探究-物质除杂与提纯"):
        append_candidate(
            label,
            removal and evaluation,
            removal + evaluation,
            "若核心为除杂方案可行性，可保留评价与除杂任务；具体反应性质只是手段。",
        )

    balance = evidence("配平", "最简整数系数")
    for label in ("化学方程式-配平", "【T2】化学方程式-配平"):
        append_candidate(
            label,
            balance,
            balance,
            "必须实际考查配平；已配平方程式作为计算背景不触发，T1/T2可共同保留。",
        )

    table = evidence("表格", "下表", "表中", "表1", "表一", "表内")
    calculation = evidence("计算", "求", "多少")
    stoichiometry = evidence("化学方程式", "纯度", "有效成分", "质量分数", "化学计量")
    append_candidate(
        "【T2】计算-表格数据计算",
        table and calculation and stoichiometry,
        table + calculation + stoichiometry,
        "必须实际利用表格数据完成方程式计量；纯质量守恒加减不足以触发。",
    )

    # ===== 复杂标签定向召回：多条件相交，不扩充简单概念标签 =====
    task_graph = task_evidence("曲线", "图像", "图象", "横坐标", "纵坐标", "横轴", "纵轴", "坐标图")
    task_calculation = task_evidence("计算", "求出", "求得", "求解", "多少", "质量为", "质量分数为", "数值可能")
    calculate_match = re.search(r"求.{0,16}(?:质量|体积|含量|纯度|产率|范围|质量分数)", task_text)
    if calculate_match:
        task_calculation.append(calculate_match.group(0))

    oxygen_source = task_evidence("高锰酸钾", "氯酸钾", "过氧化氢", "双氧水", "kmno4", "kclo3", "h2o2")
    oxygen_preparation = task_evidence("制取氧气", "制氧", "分解", "加热")
    append_candidate(
        "图像-制取氧气",
        task_graph and oxygen_source and oxygen_preparation,
        task_graph + oxygen_source + oxygen_preparation,
        "须实际分析制氧过程的曲线，而非装置图；仅有质量坐标不自动共标质量守恒，"
        "实际提取定量数据计算时再核验图像数据计算标签。",
    )

    adding = task_evidence("逐滴", "滴加", "逐渐加入", "加入量", "加入溶液的质量", "加入氢氧化", "加入盐酸")
    ionic_reaction = task_evidence("酸", "碱", "盐", "氢氧化", "氯化", "硫酸", "碳酸", "naoh", "hcl", "cacl2", "cucl2")
    reaction_stage = task_evidence("沉淀", "气体", "拐点", "转折", "平台", "分段", "过量", "先后顺序")
    append_candidate(
        "【T2】图像-金属、酸、碱、盐反应(单纯酸碱中和除外)",
        task_graph and adding and ionic_reaction and reaction_stage,
        task_graph + adding + ionic_reaction + reaction_stage,
        "核验加入量与沉淀/气体/溶液质量曲线是否用于判断反应先后、终点或过量物质；"
        "不因出现酸碱盐名称就选，也不与所有金属酸图像标签无条件共标。",
    )

    solution_quantity = task_evidence("溶质", "溶剂", "溶液质量", "溶液的质量", "质量分数")
    solution_operation = task_evidence("蒸发", "结晶", "稀释", "增浓", "加水", "加入水", "溶解")
    append_candidate(
        "【T2】图像-溶液组成变化",
        task_graph and solution_quantity and solution_operation and not task_evidence("溶解度"),
        task_graph + solution_quantity + solution_operation,
        "须利用操作过程中溶质/溶剂/溶液质量或浓度的曲线判断组成变化；"
        "单纯溶解度随温度曲线不自动等同于溶液组成变化。",
    )
    append_candidate(
        "【T2】计算-图像数据计算",
        task_graph and task_calculation,
        task_graph + task_calculation,
        "图像中的具体数据或平台、交点等须成为定量计算输入；"
        "纯趋势辨析或图像与计算分别属于无关任务时，不据此共标。",
    )

    reaction_context = task_evidence("反应", "分解", "还原", "产气", "生成气体")
    difference = task_evidence("差量法", "质量差", "质量减少", "质量减小", "质量增加", "增重", "失重", "减重")
    append_candidate(
        "【T2】计算-差量法",
        task_calculation and reaction_context and difference,
        task_calculation + reaction_context + difference,
        "必须把反应前后可测差值对应到物质消耗或生成，建立未知量关系；"
        "普通数字相减、天平读数作比较不等于差量法。",
    )
    boundary = task_evidence("极值法", "最大", "最小", "至少", "至多", "取值范围", "数值可能", "质量范围")
    mixture_or_limit = task_evidence("混合物", "混合", "合金", "过量", "不足量", "足量", "限量")
    append_candidate(
        "【T2】计算-极值法",
        task_calculation and reaction_context and boundary and mixture_or_limit,
        task_calculation + reaction_context + boundary + mixture_or_limit,
        "须由混合物组成、限量或配比的边界求最大/最小值或可行范围；"
        "不能因出现足量或图像平台最大值就认定用了极值法。",
    )
    multi_step = task_evidence("多步反应", "连续反应", "依次反应", "多步化学计量")
    intermediate = re.search(r"(?:生成的|得到的|所得的|所得).{0,30}(?:再与|再通入|继续反应|进一步反应)", task_text)
    if intermediate:
        multi_step.append(intermediate.group(0))
    append_candidate(
        "【T2】计算-多步化学计量关系",
        task_calculation and multi_step,
        task_calculation + multi_step,
        "须沿至少两步连续反应传递中间物质并完成计量；两个独立方程式、"
        "多次加入同一试剂或单纯过滤/蒸发操作链不属于多步化学计量。",
    )

    sample = task_evidence("样品", "试样", "矿石", "合金", "石灰石", "黄铜")
    content_target = task_evidence("含量", "纯度", "质量分数")
    measurement = task_evidence("称量", "称得", "测得", "测定", "实验数据", "装置总质量", "吸收管", "产气量", "失重", "增重")
    append_candidate(
        "【T2】计算-实验数据中的物质含量",
        task_calculation and sample and content_target and measurement,
        task_calculation + sample + content_target + measurement,
        "须由称量、体积、浓度或产气等实验数据反推样品含量；"
        "只求生成物质量或产率、只由化学式计算元素质量分数不选。",
    )

    phases = task_evidence("滤渣", "滤液")
    composition_question = task_evidence("成分", "组成", "含有", "含哪些", "一定有", "可能有", "一定存在", "可能存在", "一定含", "可能含")
    append_candidate(
        "【T2】滤渣、滤液与固体成分推断",
        phases and composition_question,
        phases + composition_question,
        "须根据反应/分离结果判断滤渣、滤液或固体的成分与可能性；"
        "只问过滤操作、滤纸作用或题面直接给出的成分不是成分推断。",
    )
    # ===== 专属题型 T2：流程题、推断题、科学探究题 =====
    # 题型先看题库题型字段 business_type（CHX-GYLCT 工艺流程题、CHX-TDT 推断题、CHX-KXTJT 科学探究题），
    # 再用题面关键词兜底。整题扫描全部题面；小问只有自身带对应任务线索时，才借用总题干判断题型和主题。
    def normalize(text: str) -> str:
        return text.lower().translate(str.maketrans("₀₁₂₃₄₅₆₇₈₉", "0123456789"))

    def strip_latex(text: str) -> str:
        return re.sub(r"\\(?:ce|rm|text|mathrm|mathbf)(?![a-z])", " ", text)

    is_small_question = str(item.get("question_id")) != str(item.get("parent_id"))
    parent_question = questions.get(str(item.get("parent_id")), {})
    business_type = str(parent_question.get("business_type") or "")
    parent_stem = normalize(str(parent_question.get("stem") or ""))
    context_text = parent_stem + "\n" + task_text if is_small_question else task_text
    structure_source = focused_questions[0] if is_small_question else parent_question
    is_choice = str(structure_source.get("structure_type") or "") in {"danxuan", "duoxuan"}

    def context_evidence(*terms: str) -> List[str]:
        return [term for term in terms if term in context_text]

    # --- 流程题 ---
    flow_type = business_type == "CHX-GYLCT" or bool(context_evidence(
        "工艺流程", "流程图", "流程如下", "主要流程", "生产流程", "工艺如下", "流程示意",
    ))
    flow_step = task_evidence(
        "流程", "工艺", "步骤", "操作", "工序", "环节", "循环", "滤液", "滤渣", "母液",
        "反应器", "反应池", "沉淀池", "设备", "①", "②", "③", "ⅰ", "ⅱ", "ⅲ",
    )
    in_flow = flow_type and (not is_small_question or bool(flow_step))
    # 单独的“工艺”容易误中“工艺品”“传统工艺”，不作为流程证据。
    flow_scope = task_evidence("流程", "步骤顺序", "先后顺序", "循环利用", "循环使用", "再利用")
    if in_flow:
        flow_scope.append("工艺流程题")
    flow_topic_text = context_text if flow_scope else task_text

    def topic_evidence(*terms: str) -> List[str]:
        return [term for term in terms if term in flow_topic_text]

    recovery = topic_evidence("回收", "提纯", "除杂", "废液", "废渣", "酸浸", "碱浸")
    metal_or_salt = topic_evidence("金属", "铜", "铁", "锌", "镍", "钴", "硫酸盐", "硫酸铜", "硫酸亚铁", "氯化盐", "氯化钠")
    process = task_evidence("流程", "步骤顺序", "加入顺序", "先后顺序") + (["工艺流程题"] if in_flow else [])
    append_candidate(
        "【T2】流程题-金属与盐的回收、除杂和提纯",
        process and recovery and metal_or_salt,
        process + recovery + metal_or_salt,
        "须实际分析回收/提纯流程的原料、试剂、分离节点或产物关系；"
        "孤立金属置换或除杂选择不补流程标签，粗盐专门流程优先按对应释义区分。",
    )
    # 制碱流程既识别工艺名称，也识别题面未写工艺名的原料—产物组合；同时兼容NH₃、NH3、NH_{3}等写法。
    compact_topic = re.sub(r"[\s_{}\\]", "", flow_topic_text)
    named_process = [word for word in ("合成氨", "侯氏制碱", "侯德榜", "联合制碱", "氨碱法", "索尔维") if word in flow_topic_text]
    soda = [word for word in ("纯碱", "碳酸钠", "na2co3") if word in compact_topic]
    brine = [word for word in ("食盐", "氯化钠", "nacl") if word in compact_topic]
    ammonia = [word for word in ("氨", "nh3") if word in compact_topic]
    bicarbonate = [word for word in ("碳酸氢钠", "nahco3") if word in compact_topic]
    soda_route = soda and brine and ammonia and bicarbonate
    special_process = named_process or soda_route
    append_candidate(
        "【T2】流程题-合成氨与侯氏制碱",
        flow_scope and special_process,
        flow_scope + named_process + soda + brine + ammonia + bicarbonate,
        "核验合成氨或制碱工艺节点、原料产物、母液处理和循环关系；"
        "食盐水/氨/碳酸氢钠制纯碱路线即使未写侯氏名称也应核验本候选。"
        "本标签按现有制碱流程口径使用，不把氨碱法与侯氏法的具体工艺混为一谈；"
        "仅问纯碱性质或孤立方程式、未分析流程时不自动继承。",
    )
    reuse = task_evidence("循环利用", "循环使用", "再利用", "重新利用", "重复利用", "循环物质", "循环的物质", "返回", "回用", "再生")
    append_candidate(
        "【T2】流程题-物质循环利用",
        flow_scope and reuse,
        flow_scope + reuse,
        "实际识别回用物质、分析回流/再生路径或循环对工艺的作用时保留；"
        "可与具体工艺流程标签共存，不因已标过滤或方程式而删除。"
        "泛泛提倡回收环保或只出现循环背景，不等于考查流程物质循环。",
    )
    # 只有“海水晒盐得粗盐”的粗盐除杂流程归粗盐流程；海水利用须出现卤水、淡化、提镁、提溴等环节。
    seawater = topic_evidence("卤水", "苦卤", "海水淡化", "淡化海水", "提镁", "提溴", "海水制镁", "海水中提取", "海水综合利用")
    append_candidate(
        "【T2】流程题-海水资源利用",
        flow_scope and seawater,
        flow_scope + seawater,
        "以海水、卤水、苦卤为原料提取淡水、粗盐、镁、溴等的流程；只提海水晒盐常识、不分析流程不选。",
    )
    smelting = topic_evidence("冶炼", "炼铁", "炼钢", "高炉", "湿法炼铜", "火法炼铜", "铁矿石", "赤铁矿", "磁铁矿", "焦炭")
    append_candidate(
        "【T2】流程题-金属冶炼",
        flow_scope and smelting,
        flow_scope + smelting,
        "矿石或含金属化合物经流程转化为金属单质；废液回收金属归回收提纯流程。",
    )
    air_separation = topic_evidence("分离液态空气", "液态空气", "空气液化", "液化空气")
    append_candidate(
        "【T2】流程题-液态空气分离",
        flow_scope and air_separation,
        flow_scope + air_separation,
        "工业分离液态空气制氧气、氮气的流程；须分析液化、沸点差异或产物成分。",
    )
    waste = topic_evidence("废水", "废气", "废渣", "三废", "烟气", "脱硫", "尾气处理", "工业废液", "工厂废液", "含有硫酸铜的废液")
    # 题干里明确是生活垃圾处理时不选（释义：生活废弃物处理不适用）。
    if topic_evidence("生活垃圾", "垃圾分类", "垃圾处理厂"):
        waste = []
    append_candidate(
        "【T2】流程题-工业三废处理",
        flow_scope and waste,
        flow_scope + waste,
        "工业废水、废气、废渣的处理、净化或回收流程；生活垃圾处理不选。",
    )
    tap_water = topic_evidence("自来水厂", "水厂", "净水过程", "自来水的生产", "自来水生产", "净化天然水", "模拟净水")
    append_candidate(
        "【T2】流程题-自来水净化",
        flow_scope and tap_water,
        flow_scope + tap_water,
        "自来水厂净水或实验室模拟净水流程；整题处于该背景时整题标，各小问仍按自己的得分点标。",
    )
    # 成分推断须同时出现“一定……”与“可能……”这类判断，单出现“白色固体”不足以触发。
    must_have = topic_evidence("一定含有", "一定不含", "一定没有", "一定存在", "一定不存在")
    may_have = topic_evidence("可能含有", "可能有", "可能存在")
    append_candidate(
        "【T2】流程题-物质成分探究",
        flow_scope and must_have and may_have,
        flow_scope + must_have + may_have,
        "以流程图或步骤给出加试剂、观察现象、分离，推断未知混合物中一定有、一定没有、可能有的成分。",
    )
    preparation = topic_evidence("制备", "制取", "生产", "合成")
    recovery_flow = process and recovery and metal_or_salt
    specific_flow = special_process or seawater or smelting or air_separation or recovery_flow
    append_candidate(
        "【T2】流程题-物质制备(不含合成氨与侯氏制碱)",
        flow_scope and preparation and not specific_flow,
        flow_scope + preparation,
        "核验物质制备流程的节点、原料条件、分离操作或产物关系；"
        "合成氨/制碱、海水利用、液态空气分离、金属冶炼、金属与盐的回收提纯走专门标签，不用本标签替代。"
        "不能因有流程图就把全部流程标签都选上。",
    )

    # --- 推断题 ---
    # 字母代号要在保留大小写的原文里找：小写化后 C、H 会和碳、氢的元素符号混在一起；
    # 后接“、．.”的是选项字母，不算物质代号。
    raw_parts = [str(parent_question.get("stem") or "")] if is_small_question else []
    raw_parts += [str(q.get(f) or "") for q in focused_questions for f in ("stem", "options")]
    raw_context = re.sub(r"[\s{}_$]", "", strip_latex("\n".join(raw_parts)))
    letter_codes = set(re.findall(r"(?<![A-Za-z])[A-H](?![a-z0-9A-Z、．.])", raw_context))
    if len(set(re.findall(r"[甲乙丙丁戊己]", raw_context))) >= 3:
        letter_codes.add("甲乙丙")
    infer_type = business_type == "CHX-TDT" or (
        bool(context_evidence("推断", "转化关系", "相互转化", "均为初中化学", "是初中化学常见")) and len(letter_codes) >= 3
    )
    infer_task = task_evidence("化学式", "名称", "是什么", "推断", "物质", "成分", "方程式")
    in_inference = infer_type and (not is_small_question or bool(infer_task))
    infer_scope = ["推断题"] if in_inference else []

    def infer_evidence(*terms: str) -> List[str]:
        return [term for term in terms if term in context_text] if in_inference else []

    # 推断线索只看题干（总题干和当前题干），不看选项和解析，避免“高温”“沉淀”等词从选项里带进来。
    stem_parts = [str(parent_question.get("stem") or "")] if is_small_question else []
    stem_parts += [str(q.get("stem") or "") for q in focused_questions]
    stem_text = normalize("\n".join(stem_parts))

    def clue_evidence(*terms: str) -> List[str]:
        return [term for term in terms if term in stem_text] if in_inference else []

    game = infer_evidence("接龙", "迷宫", "闯关", "游戏", "拼图", "爬梯", "棋盘", "解锁", "传球", "接力")
    # 表格型要求表格里给的是物质两两混合的现象；框图题附一张信息表不算。
    table = infer_evidence("下表", "表格", "如表", "表中")
    table_phenomena = infer_evidence("↓", "↑", "无明显现象", "无现象", "产生沉淀", "产生气体")
    # 反应式型要求出现“A+B→C”“A+B=C+D”这类完整反应式，单个“A→E”只是在引用框图里的转化。
    equations = re.findall(
        r"(?<![A-Za-z])[A-H](?:\+[A-Za-z0-9()]+)+(?:=|＝|→|->|\\to|\\xrightarrow)[A-Za-z0-9()]+",
        raw_context.replace("＋", "+"),
    ) if in_inference else []
    experiment_chain = infer_evidence("取样", "取少量", "加入足量", "滴加", "过滤", "加水溶解", "步骤")
    composition = infer_evidence("一定含有", "可能含有", "一定不含", "一定没有", "组成", "成分")
    diagram = infer_evidence("如图", "<image>", "框图", "关系图", "转化关系", "箭头", "相连", "连线", "“—”", "“→”")
    append_candidate("【T2】推断题类型-游戏型", game, infer_scope + game,
                     "游戏规则与未知物质推断同时成立才选；此时不再标框图型、连线型。")
    append_candidate("【T2】推断题类型-表格型", table and table_phenomena and not game, infer_scope + table + table_phenomena,
                     "以表格呈现物质间两两反应现象并据此推断。")
    append_candidate("【T2】推断题类型-反应式型", equations and not game, infer_scope + equations[:3],
                     "题干主要以反应式呈现物质转化关系。")
    append_candidate("【T2】推断题类型-实验型", experiment_chain and composition and not game,
                     infer_scope + experiment_chain + composition,
                     "以取样、加试剂、观察现象的实验链推断未知成分；无实验场景的转化推断不选。")
    for label in ("【T2】推断题类型-框图型", "【T2】推断题类型-连线型"):
        append_candidate(label, diagram and not game and not (table and table_phenomena), infer_scope + diagram,
                         "看图中字母代号是否带框：带圆框或方框为框图型，不带框为连线型，两者只选一个。")
    append_candidate(
        "【T2】推断题类型-文字叙述型",
        in_inference and len(letter_codes) >= 3 and not (game or (table and table_phenomena) or diagram or equations),
        infer_scope,
        "纯文字叙述、不借助框图表格连线的推断题。",
    )
    triangle = clue_evidence("三角")
    append_candidate("【T2】推断题线索-三角转化关系", triangle, infer_scope + triangle,
                     "三种物质两两转化或闭环转化是推断关键时才选。")
    colors = clue_evidence("黑色", "红色", "紫红色", "暗紫色", "蓝色", "浅绿色", "黄色", "红褐色", "白色沉淀", "蓝色沉淀")
    append_candidate("【T2】推断题线索-颜色特征", colors, infer_scope + colors,
                     "颜色必须是确定物质的关键线索。")
    states = clue_evidence("常温下为液体", "常温下为气体", "常温下是液体", "常温下是气体", "无色液体", "无色气体", "液态", "气态")
    append_candidate("【T2】推断题线索-物质状态", states, infer_scope + states,
                     "物质状态必须是题目明确给出、用于确定身份的关键线索。")
    conditions = clue_evidence("点燃", "通电", "高温", "催化剂", "加热")
    append_candidate("【T2】推断题线索-反应条件", conditions, infer_scope + conditions,
                     "反应条件明确给出且是确定核心反应的关键线索。")
    extremes = [
        m for m in re.findall(r"[^，。；、\s]{0,6}最[^，。；、\s]{1,6}", stem_text)
        if not re.search(r"最(?:外层|后|终|简整数|佳|好|少量|初|先|近|强|弱|大的是|小的是|合理|恰当|适宜)", m)
    ][:3] if in_inference else []
    append_candidate("【T2】推断题线索-化学之最", extremes, infer_scope + extremes,
                     "“之最”信息是推断起点时才选。")
    # 质量关系须给出具体数值或比例，单提“质量分数”“相对分子质量”不够。
    mass = [m.group(0) for m in re.finditer(
        r"(?:相对分子质量|式量)(?:为|是|等于)\s*\d+|质量比(?:为|是)\s*\d+\s*[:：]\s*\d+"
        r"|原子个数比(?:为|是)?\s*\d+\s*[:：]\s*\d+|质量分数(?:为|是)\s*\d+",
        stem_text,
    )][:3] if in_inference else []
    append_candidate("【T2】推断题线索-质量关系", mass, infer_scope + mass,
                     "题目给出具体质量数据或比例且是推断关键时才选。")
    uses = clue_evidence("改良酸性土壤", "灭火", "胃酸", "发酵粉", "建筑材料", "补钙剂", "制肥皂", "造纸", "玻璃", "洗涤剂", "调味品", "人工降雨", "医疗急救", "供给呼吸")
    append_candidate("【T2】推断题线索-物质用途", uses, infer_scope + uses,
                     "具体用途是判断物质的直接依据才选；笼统用途不选。")
    reaction_types = clue_evidence("化合反应", "分解反应", "置换反应", "复分解反应", "中和反应", "基本反应类型")
    append_candidate("【T2】推断题线索-反应类型", reaction_types, infer_scope + reaction_types,
                     "反应类型作为已知条件且是确定转化关系的关键线索才选。")
    phenomena = clue_evidence("白色沉淀", "蓝色沉淀", "红褐色沉淀", "产生气泡", "有气泡", "变浑浊", "复燃", "火星四射", "变红", "变蓝")
    append_candidate("【T2】推断题线索-反应现象", phenomena, infer_scope + phenomena,
                     "特征现象直接承担推断作用才选。")
    append_candidate("【T2】推断题线索-反应现象-实验类", len(experiment_chain) >= 2 and phenomena,
                     infer_scope + experiment_chain + phenomena,
                     "须把前一步的处理、试剂或结论带入后续观察，形成实验证据链；孤立一次现象不选。")

    # --- 科学探究题 ---
    # “实验探究”“实验结论”在普通实验题里也常见，题型判断要求同时出现猜想或提出问题。
    inquiry_markers = context_evidence(
        "提出问题", "猜想与假设", "作出猜想", "提出猜想", "猜想一", "猜想1", "猜想①", "猜想ⅰ",
        "【设计实验】", "【实验探究】", "【进行实验】", "【实验结论】", "反思评价", "反思与评价", "【查阅资料】", "拓展延伸",
    )
    has_hypothesis_marker = any(m.startswith(("提出问题", "猜想", "作出猜想", "提出猜想")) for m in inquiry_markers)
    inquiry_type = business_type == "CHX-KXTJT" or (len(inquiry_markers) >= 2 and has_hypothesis_marker)
    inquiry_task = task_evidence(
        "猜想", "假设", "实验", "步骤", "操作", "现象", "结论", "理由", "原因", "评价", "改进", "不足", "数据", "计算", "方案",
    )
    in_inquiry = inquiry_type and not is_choice and (not is_small_question or bool(inquiry_task))
    inquiry_scope = ["科学探究题"] if in_inquiry else []

    def stage(*terms: str) -> List[str]:
        return task_evidence(*terms) if in_inquiry else []

    has_measured_value = bool(re.search(r"\d+(?:\.\d+)?\s*(?:g|克|ml|毫升|l|升|%|℃|mg|kg)", task_text))
    stage_rules = (
        ("【T2】科学探究环节-猜想与假设", stage("猜想", "假设"), "提出、补充或判断猜想是否合理。"),
        ("【T2】实验-设计与操作-科学探究题", stage("实验步骤", "实验操作", "设计实验", "实验方案", "取少量", "取样", "操作方法"),
         "设计、补充或执行实验方案、步骤和操作；只评价已有方案归实验评价。"),
        ("【T2】科学探究环节-解释与结论", stage("实验结论", "得出结论", "结论是", "成立", "理由是", "原因是", "原因可能是"),
         "由现象、数据得出结论或解释原因。"),
        ("【T2】科学探究环节-数据与计算", stage("数据", "测得", "计算", "质量为", "体积为") if has_measured_value else [],
         "利用实验测得数据处理或计算；按方程式算样品含量另看计算标签。"),
        ("【T2】科学探究环节-实验评价", stage("评价", "不足", "改进", "不严谨", "不合理", "缺陷", "反思", "优点"),
         "评价已有方案、证据或结论，指出不足或提出改进。"),
    )
    for label, signals, guidance in stage_rules:
        append_candidate(label, signals, inquiry_scope + signals, guidance)

    # 主题词必须出现在“探究/研究/验证/测定……”附近，才算探究对象；只在题干某处出现不够，
    # 例如探究炼铁的题里出现“铁锈”，探究制氧机的题里出现“二氧化锰”。
    def inquiry_topic(*terms: str) -> List[str]:
        if not in_inquiry:
            return []
        hits = []
        for term in terms:
            escaped = re.escape(term)
            pattern = (
                rf"(?:探究|研究|验证|测定|探秘|探索|实验目的|提出问题|项目式学习)[^。；！？\n]{{0,30}}{escaped}"
                rf"|{escaped}[^。；！？\n]{{0,15}}(?:探究|研究|验证|测定|兴趣)"
            )
            if re.search(pattern, context_text):
                hits.append(term)
        return hits

    topic_rules = (
        ("【T2】科学探究题-食品包装与保鲜成分(大题)", inquiry_topic("干燥剂", "脱氧剂", "保鲜剂", "食品包装", "充气包装", "双吸剂", "食品袋")),
        ("【T2】科学探究题-药品变质(大题)", inquiry_topic("是否变质", "变质程度", "部分变质", "全部变质", "变质情况", "变质")),
        ("【T2】科学探究题-未知药品成分(大题)", inquiry_topic("标签破损", "标签受损", "标签残缺", "标签已破损", "标签模糊", "失去标签")),
        ("【T2】科学探究题-呼出与吸入气体(大题)", inquiry_topic("呼出气体", "吸入空气", "呼出的气体", "吸入的空气")),
        ("【T2】科学探究题-蜡烛燃烧(大题)", inquiry_topic("蜡烛", "石蜡")),
        ("【T2】科学探究题-二氧化碳灭火局限性(大题)", inquiry_topic("镁在二氧化碳中燃烧", "镁条在二氧化碳", "吹气生火", "二氧化碳灭火")),
        ("【T2】科学探究题-空气中氧气含量测定(大题)", inquiry_topic("空气中氧气含量", "空气中氧气的含量", "氧气的体积分数", "空气里氧气的含量")),
        ("【T2】科学探究题-质量守恒验证(大题)", inquiry_topic("验证质量守恒", "质量守恒定律", "质量守恒")),
        ("【T2】科学探究题-质量守恒应用(大题)",
         inquiry_topic("元素组成", "组成元素", "原子个数比", "化学式")
         if context_evidence("增重", "质量增加", "吸收", "称量", "测得", "原子个数比", "元素质量比") else []),
        ("【T2】科学探究题-金属活动性(大题)", inquiry_topic("活动性", "活泼性")),
        ("【T2】科学探究题-金属锈蚀条件(大题)", inquiry_topic("生锈", "锈蚀", "铜绿")),
        ("【T2】科学探究题-燃烧条件与灭火原理(大题)", inquiry_topic("燃烧的条件", "燃烧条件", "着火点", "灭火原理", "灭火的原理")),
        ("【T2】科学探究题-催化剂效率(大题)", inquiry_topic("催化剂", "催化效果", "催化作用", "催化能力")),
        ("【T2】科学探究题-反应速率影响因素(大题)", inquiry_topic("反应速率", "反应快慢", "反应的快慢", "分解速率")),
        ("【T2】科学探究题-无明显现象反应是否发生(大题)", inquiry_topic("无明显现象", "没有明显现象", "是否发生了反应", "是否发生反应", "可视化")),
        ("【T2】科学探究题-二氧化碳与水反应(大题)", inquiry_topic("二氧化碳与水", "二氧化碳和水", "co2与水", "co2和水")),
        ("【T2】科学探究题-未知反应产物(大题)", inquiry_topic("产物", "生成物", "生成了什么", "反应原理")),
        ("【T2】科学探究题-物质除杂与净化(大题)", inquiry_topic("除杂", "提纯", "净化")),
    )
    matched_topics = []
    for label, signals in topic_rules:
        append_candidate(label, signals, inquiry_scope + signals, "核对整题探究对象是否就是该主题，并检查是否有探究过程。")
        if signals:
            matched_topics.append(label)
    # 以下几类范围较宽，只在没有更具体主题命中时作为兜底候选。
    fallback_rules = (
        ("【T2】科学探究题-酸碱性质(大题)", inquiry_topic("酸的性质", "碱的性质", "酸的化学性质", "碱的化学性质", "酸和碱的化学性质", "中和反应")),
        ("【T2】科学探究题-物质组成与含量(大题)", inquiry_topic("成分", "组成", "含量", "溶质")),
        ("【T2】科学探究题-物质变化条件与影响因素(大题)", inquiry_topic("条件", "影响因素")),
        ("【T2】科学探究题-物质性质与变化规律(大题)", inquiry_topic("性质", "规律")),
    )
    fallback_taken = bool(matched_topics)
    for label, signals in fallback_rules:
        append_candidate(label, signals and not fallback_taken, inquiry_scope + signals,
                         "范围较宽的兜底主题；更具体的科学探究题主题成立时不选。")
        fallback_taken = fallback_taken or bool(signals)

    hypothesis = task_evidence("猜想", "假设")
    theoretical_rejection = task_evidence(
        "没有意义", "无意义", "无需实验", "不需要实验", "不必实验",
        "违背", "不可能成立", "不可能生成", "不可能存在",
    )
    # 很多题不会直接写“没有意义”，而是问“哪个猜想不合理/错误，理由是……”。
    # 只有“猜想/假设 + 否定判断 + 要求说明理由”同时出现才补，避免把“根据现象说明猜想不成立”
    # 这类实验后排除也当成理论预判。
    reasoned_rejection = re.search(
        r"(?:猜想|假设)[^。；！？\n]{0,30}(?:不合理|不成立|错误)"
        r"[^。；！？\n]{0,30}(?:理由|原因|依据)"
        r"|(?:不合理|不成立|错误)[^。；！？\n]{0,30}"
        r"(?:理由|原因|依据)[^。；！？\n]{0,30}(?:猜想|假设)",
        task_text,
    )
    pre_analysis_rejection = re.search(
        r"(?:通过分析|经过讨论|理论上|理论分析|根据已有知识|依据已有知识)"
        r"[^。；！？\n]{0,80}(?:猜想|假设)[^。；！？\n]{0,20}(?:不合理|不成立|错误)"
        r"|(?:猜想|假设)[^。；！？\n]{0,30}(?:不合理|不成立|错误)"
        r"[^。；！？\n]{0,40}(?:通过分析|经过讨论|理论上|理论分析)",
        task_text,
    )

    def rejection_is_experimental_basis(match: Optional[re.Match]) -> bool:
        """否定结论附近有实验观察，且没有明显的理论判断线索时，不按理论预判召回。"""
        if match is None:
            return False
        # 只看否定判断之前的上下文：实验后排除通常是“先观察到现象，再判断猜想不成立”；
        # 理论预判通常是先判断，后面才进入实验，不能被后面的实验描述误伤。
        context = task_text[max(0, match.start() - 280):match.end() + 30]
        experimental_cues = (
            "观察到", "观察颜色", "实验现象", "由实验", "实验完毕",
            "测得", "读数", "收集到", "实验数据",
        )
        theory_cues = (
            "理论", "根据已有知识", "依据已有知识", "经过讨论",
            "反应物中不含", "反应物中没有", "元素守恒", "质量守恒",
            "不能共存", "会继续反应", "受损标签", "标签上",
        )
        return any(cue in context for cue in experimental_cues) and not any(
            cue in context for cue in theory_cues
        )

    reasoned_rejection_is_theoretical = (
        reasoned_rejection is not None
        and not rejection_is_experimental_basis(reasoned_rejection)
    )
    pre_analysis_rejection_is_theoretical = (
        pre_analysis_rejection is not None
        and not rejection_is_experimental_basis(pre_analysis_rejection)
    )
    theoretical_reason = evidence(
        "反应物中不含", "反应物中没有", "生成物中不含", "生成物中没有",
        "元素守恒", "质量守恒", "不能共存", "物质共存",
        "颜色不符", "状态不符", "金属活动性",
    )
    theoretical_signals = theoretical_rejection + theoretical_reason
    if reasoned_rejection_is_theoretical:
        theoretical_signals.append(reasoned_rejection.group(0))
    if pre_analysis_rejection_is_theoretical:
        theoretical_signals.append(pre_analysis_rejection.group(0))
    append_candidate(
        "科学探究-基于理论直接否定猜想",
        hypothesis and (
            theoretical_rejection
            or reasoned_rejection_is_theoretical
            or pre_analysis_rejection_is_theoretical
        ),
        hypothesis + theoretical_signals,
        "仅凭科学原理在实验前直接否定猜想才选；"
        "根据实验现象排除猜想或一般方案可行性评价不选。",
    )
    return hints


def build_candidate_pool(
    item: Dict[str, Any],
    include_details: bool = True,
) -> Tuple[List[str], str]:
    """融合多路参考标签，输出T1在前、T2在后的参考池及完整解释。"""
    question_id = item.get("question_id")
    # 字典保留首次出现顺序，同时按稳定ID去重；来源仅用于错误排查，不发送给模型。
    sources_by_id = defaultdict(list)

    def add_candidate(knw_id: str, source: str) -> None:
        if source not in sources_by_id[knw_id]:
            sources_by_id[knw_id].append(source)

    # 候选来源可放标签ID或名称；统一映射到当前目录名称并按ID去重。
    for field in CONFIG["candidate_source_fields"]:
        for value in _as_list(item.get(field), field, question_id):
            value = str(value)
            knw_id = value if value in id_to_leaf_name else leaf_name_to_id.get(value)
            if knw_id is not None:
                add_candidate(knw_id, field)

    # 2. 可选补漏：固定补充名单 + 原有关键词规则。均只加候选，不直接决定标签。
    for label in CONFIG.get("extra_candidate_labels", []):
        if label not in leaf_name_to_id:
            raise ValueError(f"手动补充标签不是当前标签目录中的有效名称: {label}")
        add_candidate(leaf_name_to_id[label], "手动补充")
    hints = build_candidate_hints(item)
    # 规则召回只补“当前三路候选里还没有”的标签，并限制每题净新增数量。
    # 先放具体题型/实验/计算 T2，再放 T1，最后放科学探究阶段/泛化兜底。稀有题型更适合
    # 关键词召回，而常见 T1 通常已经由模型候选覆盖。原始规则命中数仍可用于排查，
    # 但真正加入候选池的最多 5 个。
    broad_inquiry_hints = {
        "【T2】科学探究环节-猜想与假设",
        "【T2】实验-设计与操作-科学探究题",
        "【T2】科学探究环节-解释与结论",
        "【T2】科学探究环节-数据与计算",
        "【T2】科学探究环节-实验评价",
    }
    generic_inquiry_hints = {
        "【T2】科学探究题-酸碱性质(大题)",
        "【T2】科学探究题-物质组成与含量(大题)",
        "【T2】科学探究题-物质变化条件与影响因素(大题)",
        "【T2】科学探究题-物质性质与变化规律(大题)",
    }

    def hint_priority(hint: Dict[str, str]) -> int:
        label = hint["label"]
        if label in CONFIG.get("candidate_hint_high_priority_labels", []):
            return -1
        if label in broad_inquiry_hints:
            return 2
        if label in generic_inquiry_hints:
            return 3
        if label_type_by_name[label] == "T2":
            return 0
        return 1

    ranked_hints = sorted(
        enumerate(hints),
        key=lambda indexed_hint: (hint_priority(indexed_hint[1]), indexed_hint[0]),
    )
    selected_hints = []
    for _, hint in ranked_hints:
        knw_id = leaf_name_to_id[hint["label"]]
        if knw_id in sources_by_id:
            continue
        if len(selected_hints) >= CONFIG["max_candidate_hints_per_item"]:
            break
        selected_hints.append(hint)
    for hint in selected_hints:
        add_candidate(leaf_name_to_id[hint["label"]], "题目证据规则召回")

    # 3. 业务规则第 7 节已确认的 T1/T2 对：池里有一个就补另一个，只补一层。
    if CONFIG.get("enable_pair_completion", True):
        for partner_name, source_name in pair_partners_for(item, [id_to_leaf_name[k] for k in sources_by_id]):
            add_candidate(leaf_name_to_id[partner_name], f"T1/T2配对：{source_name}")

    # 4. 可选补入释义直接引用的标签，只扩展一层；32k 上下文下默认关闭。
    base_ids = list(sources_by_id)
    if CONFIG["expand_explanation_references"]:
        for source_id in base_ids:
            source_name = id_to_leaf_name[source_id]
            for reference in explanation_references_by_name.get(source_name, ()):
                reference_id = leaf_name_to_id[reference]
                add_candidate(reference_id, f"释义引用：{source_name}")

    # 5. T1在前、T2在后；同类维持合并顺序，不按来源数量排序。
    ordered_ids = sorted(
        sources_by_id,
        key=lambda knw_id: label_type_by_name[id_to_leaf_name[knw_id]] != "T1",
    )
    item["_candidate_sources"] = dict(sources_by_id)
    item["_candidate_hints"] = selected_hints
    labels = [id_to_leaf_name[knw_id] for knw_id in ordered_ids]
    if len(labels) > CONFIG["max_candidate_labels"]:
        raise ValueError(
            f"候选数{len(labels)}超过上限{CONFIG['max_candidate_labels']}: "
            f"question_id={question_id}"
        )
    # 所有来源一视同仁：已有、规则补漏、手动补充、释义引用都必须有完整释义。
    missing_explanations = [
        label for label in labels
        if not isinstance(leaf_name_to_explanation.get(label), str)
        or not leaf_name_to_explanation[label].strip()
    ]
    if missing_explanations:
        raise ValueError(f"候选标签缺少完整释义，不发送请求: {missing_explanations}")
    if not include_details:
        return labels, ""

    # 6. 候选展示只拼名称与完整释义；不加编号，避免分析里出现“第3项”这类训练时看不到的指代。
    detail_blocks = [
        CANDIDATE_ITEM_TEMPLATE.format(label=label, explanation=leaf_name_to_explanation[label])
        for label in labels
    ]
    return labels, "\n\n".join(detail_blocks)




def get_image_path_dict(image_dict_path: str) -> Dict[str, str]:
    """读取 image_id 到本地图片路径的映射。路径留空时返回空字典，带图题目将被跳过等待处理。"""
    if not image_dict_path or not os.path.exists(image_dict_path):
        print(
            f"警告：图片映射文件未配置或不存在: {image_dict_path!r}，"
            f"数据中带图的题目将无法处理（跳过），请配置后重跑。"
        )
        return {}

    image_data = read_json_and_jsonl(image_dict_path)
    return {
        str(image_item["image_id"]): image_item["image_path"]
        for image_item in image_data
        if image_item.get("image_id") is not None and image_item.get("image_path")
    }


def get_question_content(
    item: dict,
    image_path_dict: Dict[str, str],
) -> Tuple[str, List[str], bool]:
    """组装题目文本，并按 <image> 出现顺序返回对应的原始图片路径。

    【整题/小题分流】：
    - 给整题打标（question_id == parent_id）：提供全部小题的背景+全部解析/答案；
    - 给某小题打标（question_id != parent_id）：提供全量背景，但只聚焦当前小题的解析/答案。
    """
    question_id = str(item["question_id"])
    parent_id = str(item["parent_id"])
    questions_map = {str(key): value for key, value in item["questions_map"].items()}

    # 1. 获取有序题目序列
    # 用户确认 questions_map 已排序，直接转为列表即可
    all_question_entries = list(questions_map.items())
    all_questions_sequence = [question for _, question in all_question_entries]
    if not all_questions_sequence:
        return "", [], False

    # 判断当前是给大题打标还是给小题打标
    is_main_question = (question_id == parent_id)

    # 找到当前小题在序列中的索引（如果是大题，则包含全部）
    current_focus_index = len(all_questions_sequence)
    if not is_main_question:
        for idx, (q_id, _) in enumerate(all_question_entries):
            if q_id == question_id:
                current_focus_index = idx
                break

    image_path_list: List[str] = []
    image_pointers = {q_id: 0 for q_id, _ in all_question_entries}
    missing_image = False

    def collect_field_images(q_id: Any, question: dict, field_name: str) -> None:
        nonlocal missing_image
        image_count = len(question.get("images", {}).get(field_name, []) or [])
        pointer = image_pointers.get(q_id, 0)

        for _ in range(image_count):
            pointer += 1
            image_id = f"{pointer}_{q_id}"
            image_path = image_path_dict.get(image_id)
            if image_path:
                image_path_list.append(image_path)
            else:
                print(f"警告：{parent_id}-{question_id} 找不到本地图片 {image_id}")
                missing_image = True

        image_pointers[q_id] = pointer

    def append_field(
        parts: List[str],
        q_id: Any,
        question: dict,
        field_name: str,
        title: str,
    ) -> None:
        text = str(question.get(field_name, "") or "").strip()
        if not text:
            if question.get("images", {}).get(field_name):
                collect_field_images(q_id, question, field_name)
            return
        collect_field_images(q_id, question, field_name)
        parts.append(f"【{title}】：\n{text}")

    main_q_id, main_stem_q = all_question_entries[0]
    main_parts: List[str] = []
    append_field(main_parts, main_q_id, main_stem_q, "stem", "总题干")
    append_field(main_parts, main_q_id, main_stem_q, "options", "总题干选项")

    if is_main_question and len(all_question_entries) == 1:
        append_field(main_parts, main_q_id, main_stem_q, "analysis", "解析")
        append_field(main_parts, main_q_id, main_stem_q, "answer", "答案")
        final_input_content = "# 独立题完整信息（全部属于本次标注范围）\n" + "\n\n".join(main_parts)
    elif is_main_question:
        append_field(main_parts, main_q_id, main_stem_q, "analysis", "总题干解析")
        append_field(main_parts, main_q_id, main_stem_q, "answer", "总题干答案")
        subquestion_blocks = []
        for sequence_number, (sub_q_id, sub_question) in enumerate(all_question_entries[1:], 1):
            question_index = sub_question.get("question_index") or sequence_number
            sub_parts: List[str] = []
            append_field(sub_parts, sub_q_id, sub_question, "stem", "小问题干")
            append_field(sub_parts, sub_q_id, sub_question, "options", "小问选项")
            append_field(sub_parts, sub_q_id, sub_question, "analysis", "小问解析")
            append_field(sub_parts, sub_q_id, sub_question, "answer", "小问答案")
            subquestion_blocks.append(
                f"## 第{question_index}小问（独立得分点）\n" + "\n\n".join(sub_parts)
            )
        final_input_content = (
            "# 复合题总题干（所有小问的共同背景）\n"
            + "\n\n".join(main_parts)
            + "\n\n# 各小问（逐问识别独立核心任务）\n"
            + "\n\n".join(subquestion_blocks)
        )
    else:
        current_focus_q = questions_map.get(question_id)
        if current_focus_q is None or current_focus_index == len(all_question_entries):
            raise ValueError(
                f"当前小题 {question_id!r} 不在 questions_map 中，无法生成聚焦打标上下文。"
            )

        previous_blocks = []
        for sequence_number, (sub_q_id, sub_question) in enumerate(
            all_question_entries[1:current_focus_index], 1
        ):
            question_index = sub_question.get("question_index") or sequence_number
            previous_parts: List[str] = []
            append_field(previous_parts, sub_q_id, sub_question, "stem", "前序小问题干")
            append_field(previous_parts, sub_q_id, sub_question, "options", "前序小问选项")
            previous_blocks.append(
                f"## 第{question_index}小问（仅作上下文，不属于本次标注范围）\n"
                + "\n\n".join(previous_parts)
            )

        current_parts: List[str] = []
        append_field(current_parts, question_id, current_focus_q, "stem", "当前小题题干")
        append_field(current_parts, question_id, current_focus_q, "options", "当前小题选项")
        append_field(current_parts, question_id, current_focus_q, "analysis", "当前小题解析")
        append_field(current_parts, question_id, current_focus_q, "answer", "当前小题答案")

        previous_context = (
            "\n\n# 前序小问（仅用于理解引用关系）\n" + "\n\n".join(previous_blocks)
            if previous_blocks
            else ""
        )
        final_input_content = (
            "# 复合题总题干（仅作共同背景，不直接继承标签）\n"
            + "\n\n".join(main_parts)
            + previous_context
            + "\n\n# 当前唯一待标小问（只为本区块选择标签）\n"
            + "\n\n".join(current_parts)
        )

    if final_input_content.count("<image>") != len(image_path_list):
        print(
            f"警告：{parent_id}-{question_id} 图片占位符数量 "
            f"{final_input_content.count('<image>')} 与本地图片数量 {len(image_path_list)} 不一致"
        )
        missing_image = True

    return final_input_content, image_path_list, missing_image


# ============================== 提示词（精简版） ==============================
# 只保留《化学题目打标业务规则》第 1～7 节的核心条款，不放题型指南；标签边界交给候选解释。
# 与完整版 2026_11_05_chemistry_cot_make.py 的区别只在本节、build_prompt_from_content 和配置区。
# 正文中「」里的都是目录标签名，启动时由 validate_prompt_labels 逐个校验。

PROMPT_RULES_TEXT = """你是初中化学题目的标注人员。标签用于相似题推荐和题目分类，目标是标出学生解决这道题需要知道、学习的内容。

下方“候选标签”由历史标注和关键词规则合并而来，每个候选附有解释（核心与适用范围、边界及与其他标签区别）。候选不是正确答案，可以全部不选；先快速判断每个候选是否与当前得分点直接相关，相关项再按解释核对适用范围和门槛，不相关项直接排除，不展开、不反复比较。

## 一、总原则
1. 打标单位是得分点：每个空、每个小问、每个选项（“以下说法正确的是”这类题中的错误选项也是），每个得分点单独判断。
2. 宁缺勿错：每个标签都必须有题目证据，并达到标签解释写明的适用门槛。不贴切的标签不标。
3. 每个得分点从两面检查：考查形式（写方程式、写化学符号、读图、计算、描述现象、设计或评价方案）和化学知识（物质性质、反应、实验原理或操作）。两面分别判断，有贴切标签就标，缺哪一面不凑标签。同一标签可以对应得分点所需的知识，也可以同时概括整题实验或主题背景；不能因为它也描述背景，就漏掉它对具体得分点的作用。写方程式、读图或计算等考查形式有对应标签且实际考查时，也照常标注。
4. T1 是主要落点，T2 按需补充：同一得分点在同一轮同时判断贴切 T1 和 T2，不要求成对，也不为凑对硬选；有贴切 T2 就一起标，没有就只标 T1。确实找不到可靠 T1 时可只保留贴切 T2；T1/T2 都没有时才输出“无”。
5. 名称以“-其他”结尾的是兜底标签，同组具体标签都不贴切时才用。

## 二、标注范围
- 当前小问：只标当前小问的得分点；前文结论视为已知条件，不补标前文考点。背景标签（流程题、推断题类型、科学探究题主题）可标可不标，小问是流程或探究中的直接步骤时可以标。
- 复合题整题：标注范围包括各小问得分点和整题背景。标签可以同时对应某个小问的得分点、概括整题背景，或两者兼有；最终标签去重。不加入既不对应任何小问得分点、也不符合整题背景的标签。
- 非复合题：按全部得分点标注，并检查背景标签。

## 三、已确认的规则（与解释冲突时以此为准）
1. 高级标签 = 基础标签 + 额外内容：题目实际出现额外内容才标高级标签，标了就不再标它包含的基础标签。合集标签（名称里并列几个主题）考到其中一块就可标，有更具体的单项标签时优先具体项。
2. 化学式只是答案的表达方式时（推断出的物质、实验试剂、流程中的滤渣等），不标「化学式-读写、含义」和「【T2】化学用语」，改标得出答案所用的知识。
3. 名称带“(大题)”的标签只用于较长的综合实验复合题；“【T2】科学探究题-”开头的还要求有提出问题、猜想、设计实验、得出结论等探究过程。选择题不用这两类，也不用“【T2】科学探究环节-”开头的标签；选择题中判断方案用「【T2】实验-设计与评价-选择题」。
4. 图像类标签要求实际读取坐标图像，普通装置图或只有“如图”不算。图像 T2 每张图只选一个，按 pH → 金属与酸 → 金属酸碱盐其他反应 → 溶液组成变化 → 质量变化 的顺序取第一个符合的。
5. 相对分子质量、常见化合价等只在推导中顺带使用时不标。
6. T1、T2 都依据各自的标签解释和题目证据判断是否适用；在同一轮完成，不要先标完 T1 再单独重审全部 T2；不根据名称相似或层级关系推断标签，也不为凑对硬选。
7. 可选名称有三类：候选标题中的标签、提示词规则中用「」写出的标签、候选解释“边界及与其他标签区别”中用「」写出的标签。三类都可以选，但只选符合题目证据的标签；名称必须逐字一致，T2 保留【T2】前缀，不改名、不合并、不创造标签。"""

PROMPT_CHECKLIST_TEXT = """## 作答前自检
1. 范围对不对：小问没有带入其他小问的得分点；整题检查了所有小问和整题背景。
2. 每个标签是否有题目证据并符合解释；同一标签可同时对应得分点和整题背景，最终只输出一次。
3. 每个得分点是否在同一轮核对了贴切 T1/T2，T2 是否按需而非机械成对；标签名是否属于候选标题、提示词规则中的「」或“边界及与其他标签区别”中的「」；analysis 中说明采用的标签，<labels> 中逐一列出且不额外增删。"""

PROMPT_OUTPUT_TEXT = """## 输出格式
先写标注分析，再给最终标签。只输出一个 <result>，内部依次是 <analysis> 和 <labels>，不要 Markdown 代码块或其他文字。

analysis 会单独用作训练数据，读者只能看到题目，看不到上面的候选和解释：
- 像直接看着题目推理一样写，每个得分点按“学生要怎么作答 → 需要什么知识 → 贴切的 T1”来写；同一任务有合适 T2 时紧跟写出，没有就只写 T1。每个得分点一两句话。
- 整题背景标签在【得分点分析】末尾单列一句核验；不另开 T2 或背景章节。
- 同一标签可以在得分点和整题背景中说明不同作用；<labels> 中去重，只列一次。
- 不要出现“候选”“预标”“召回”“释义”“解释中”这类字眼，因为训练的时候不会有候选标签了；找不到贴切标签时写“没有贴切的标签，不标”。标签名用「」括起。
- 【最终决策】只写去重后的简短结论，不再逐点复述原因。
- 依次写三个标题，各出现一次：【题目总览】【得分点分析】【最终决策】；某项没有内容写“无”。
- 不要使用半角 <、>、&，大小关系用“＜”“＞”或中文。

<labels> 中用英文分号“;”连接标签名，去重，T1 在前、T2 在后；整条结果没有任何可用标签时只写“无”。

示例（只示范格式）：
<result>
<analysis>
【题目总览】复合题整题，围绕木炭还原氧化铜实验，共三个得分点。
【得分点分析】第1问要写化学方程式，既考查书写形式「化学方程式-书写、正误判断」，也要依据木炭还原氧化铜的反应知识「碳单质-实验-碳还原氧化铜」。第2问解释先撤导管后熄灯，考查综合实验中的操作注意事项「实验探究-操作注意事项与找错(大题)」。第3问按纯净物代入化学方程式计算，考查「计算-化学方程式-纯净物」，同一任务对应「【T2】计算-化学方程式-纯净物」；相对分子质量只是中间工具，不标。整题实验背景核验为「碳单质-实验-碳还原氧化铜」，它已在第1问承担反应知识，最终去重。
【最终决策】去重后保留上述 4 个 T1（其中「碳单质-实验-碳还原氧化铜」兼作整题实验背景）和 1 个对应 T2；不标工具性的相对分子质量。
</analysis>
<labels>化学方程式-书写、正误判断;实验探究-操作注意事项与找错(大题);计算-化学方程式-纯净物;碳单质-实验-碳还原氧化铜;【T2】计算-化学方程式-纯净物</labels>
</result>
请开始标注："""

SCOPE_NOTES = {
    "当前小问": "只标当前小问的得分点；题干和前面小问只是上下文。背景标签可标可不标。",
    "复合题整题": "标签为各小问标签的并集，再加整题背景标签；不加入任何小问都没有的知识或考查形式标签。",
    "非复合题": "按全部得分点标知识和考查形式，并检查整题背景标签。",
}
def get_task_scope(item: dict) -> str:
    """区分复合题小问、复合题整题和非复合题，三者的标注范围不同。"""
    if str(item.get("question_id")) != str(item.get("parent_id")):
        return "当前小问"
    return "复合题整题" if len(item.get("questions_map") or {}) > 1 else "非复合题"


def build_prompt_from_content(item: dict, question_full_content: str) -> str:
    """规则在前，候选解释、本题内容在后，自检和输出格式放最后；不放题型指南。"""
    candidate_labels = item.get("_candidate_labels") or []
    candidate_details = item.get("_candidate_details") or ""
    if not candidate_details:
        rebuilt_labels, candidate_details = build_candidate_pool(item)
        if candidate_labels and candidate_labels != rebuilt_labels:
            raise ValueError("候选池在加载阶段与请求阶段不一致")
        candidate_labels = rebuilt_labels
        item["_candidate_labels"] = candidate_labels
        item["_candidate_details"] = candidate_details
    task_scope = get_task_scope(item)
    prompt = f"""{PROMPT_RULES_TEXT}

## 候选标签及解释（排列顺序不代表正确性）
本区的候选标题、上文规则中的「」以及各候选“边界及与其他标签区别”中的「」，三类都是可选标签名；边界中只出现一次的标签也可以选，不要求重复成为候选标题。
{candidate_details}

## 本次标注对象：【{task_scope}】
{SCOPE_NOTES[task_scope]}

{question_full_content}

{PROMPT_CHECKLIST_TEXT}

{PROMPT_OUTPUT_TEXT}"""
    item["_type_guides"] = []
    output_budget(item, prompt)
    if len(prompt) > CONFIG["max_prompt_chars"]:
        raise ValueError(f"提示词字符数{len(prompt)}超过上限{CONFIG['max_prompt_chars']}，保留待复核，不截断解释")
    return prompt


def output_budget(item: dict, prompt: str) -> int:
    """按上下文长度估算本题可用的输出 token；不够最低输出时报错，不发请求。"""
    image_count = len(item.get("_image_paths") or [])
    estimated_prompt = int(len(prompt) * CONFIG["tokens_per_char"]) + image_count * CONFIG["tokens_per_image"]
    budget = min(CONFIG["max_tokens"], CONFIG["context_length"] - estimated_prompt - CONFIG["context_margin"])
    if budget < CONFIG["min_output_tokens"]:
        raise ValueError(
            f"提示词字符数{len(prompt)}、图片{image_count}张，估算输入约{estimated_prompt} token，"
            f"剩余输出{budget}低于下限{CONFIG['min_output_tokens']}"
        )
    return budget


def validate_prompt_labels() -> None:
    """提示词和配对表中的名称必须全部在当前目录中；召回规则用一条空记录跑一遍，触发所有名称检查。"""
    texts = PROMPT_RULES_TEXT + PROMPT_CHECKLIST_TEXT + PROMPT_OUTPUT_TEXT
    quoted = set(re.findall(r"「([^「」]+)」", texts))
    paired = {name for t1s, t2s in CONFIRMED_T1_T2_PAIRS for name in (*t1s, *t2s)}
    stale = sorted(name for name in quoted | paired if name not in leaf_name_to_id)
    if stale:
        raise ValueError(f"提示词或配对表中的标签名不在当前目录: {stale}")
    stale_priority = [
        label for label in CONFIG.get("candidate_hint_high_priority_labels", [])
        if label not in leaf_name_to_id
    ]
    if stale_priority:
        raise ValueError(f"高优先级召回标签不在当前目录: {stale_priority}")
    probe = {"question_id": "1", "parent_id": "1", "questions_map": {"1": {"stem": "", "options": ""}}}
    previous = CONFIG.get("enable_candidate_hints", True)
    CONFIG["enable_candidate_hints"] = True
    try:
        build_candidate_hints(probe)
    finally:
        CONFIG["enable_candidate_hints"] = previous
    print(f"提示词标签名校验通过：引用 {len(quoted)} 个目录标签，配对表 {len(paired)} 个，召回规则名称已全部检查。")


def validate_config():
    """校验精排运行所需的路径和字段配置。"""
    missing = []
    if not CONFIG.get("tree_path"):
        missing.append("tree_path（化学知识点树）")
    if not CONFIG.get("explanations_path"):
        missing.append("explanations_path（标签解释文件）")
    if not CONFIG.get("input_file_list") or not any(CONFIG.get("input_file_list")):
        missing.append("input_file_list（输入数据）")
    if not CONFIG.get("output_dir"):
        missing.append("output_dir（输出目录）")
    if missing:
        raise ValueError(
            "以下配置尚未填写（留空待处理），请补充后再运行:\n  - " + "\n  - ".join(missing)
        )
    for path in [CONFIG["tree_path"], CONFIG["explanations_path"], *CONFIG["input_file_list"]]:
        if not os.path.isabs(path) or not os.path.isfile(path):
            raise FileNotFoundError(f"请确认输入绝对路径存在：{path}")
    # 历史已处理文件必须存在，且不能与输入文件重合，否则会全部误跳过。
    for path in CONFIG.get("already_processed_file_list", []):
        if not os.path.isabs(path) or not os.path.isfile(path):
            raise FileNotFoundError(f"请确认历史已处理文件绝对路径存在：{path}")
        if os.path.abspath(path) in map(os.path.abspath, CONFIG["input_file_list"]):
            raise ValueError(f"历史已处理文件不能与输入文件重合：{path}")
    if not os.path.isabs(CONFIG["output_dir"]):
        raise ValueError("output_dir必须是绝对路径")
    if CONFIG["max_retries"] < 1 or CONFIG["max_items"] < 0:
        raise ValueError("max_retries须至少1，max_items须非负")
    if CONFIG["label_field"] in {"pre_tag_label_2026_10_23", "pre_tag_label_2026_10_23_id"}:
        raise ValueError("精标输出字段不能覆盖预标标签字段")
    CONFIG["already_processed_file"] = os.path.join(
        CONFIG["output_dir"], f"{CONFIG['output_prefix']}_{CONFIG['temperature']}_merged.jsonl"
    )
    if os.path.abspath(CONFIG["already_processed_file"]) in map(os.path.abspath, CONFIG["input_file_list"]):
        raise ValueError("第二轮输出不能与输入文件重合")


def merge_jsonl_files(output_dir: str, model_name: str, final_output_path: str) -> None:
    """
    执行全局去重合并，并在成功后删除源文件。
    1. 读取所有源文件（包括已有的merged文件）进行全局去重。
    2. 用最终结果覆盖写回 merged 文件。
    3. 成功写入后，删除所有被合并的小文件（不包括merged文件自身）。
    4. 没有新增的非空结果文件时，不重写已有 merged 文件。
    """
    if final_output_path is None:
        final_output_path = os.path.join(output_dir, f"{model_name}_{CONFIG['temperature']}_merged.jsonl")

    # 1. 识别所有需要合并的非空“小文件”（明确排除 merged 和 error 文件）
    files_to_merge = []
    for f in os.listdir(output_dir):
        file_path = os.path.join(output_dir, f)
        if (os.path.isfile(file_path) and
                f.endswith(".jsonl") and
                model_name in f and 
                "error" not in f and 
                "merged" not in f and
                os.path.getsize(file_path) > 0):
            files_to_merge.append(file_path)

    # 没有新结果时，保留既有 merged 文件，避免无意义的大文件读写。
    if not files_to_merge:
        if os.path.exists(final_output_path):
            print("没有新增的非空结果文件，保留现有 merged 文件，不执行重写。")
        else:
            print("没有找到需要合并的非空结果文件。")
        return

    print(f"开始全局合并：将 {len(files_to_merge)} 个源文件与已有的 merged 文件合并...")

    # 2. 全局去重：将所有数据加载到内存字典中
    unique_items = {}
    unique_id_key = CONFIG['unique_id']

    # 2.1 首先加载已有的 merged 文件作为基础
    if os.path.exists(final_output_path):
        try:
            items = read_json_and_jsonl(final_output_path)
            for item in items:
                item_id = item.get(unique_id_key)
                if item_id:
                    unique_items[str(item_id)] = item
            print(f"已从 '{os.path.basename(final_output_path)}' 加载 {len(unique_items)} 条基础记录。")
        except Exception as e:
            print(f"读取现有的 merged 文件时出错: {e}")
            print("为保护已有数据，本次不覆盖 merged 文件，也不删除源文件。")
            return

    # 2.2 接着加载所有小文件，新数据会覆盖字典中的旧数据
    for file_path in files_to_merge:
        initial_count = len(unique_items)
        try:
            items = read_json_and_jsonl(file_path)
            for item in items:
                item_id = item.get(unique_id_key)
                if item_id:
                    unique_items[str(item_id)] = item # 核心去重/更新步骤
            print(f"已处理: {os.path.basename(file_path)}，唯一记录总数从 {initial_count} 更新为 {len(unique_items)}")
        except Exception as e:
            print(f"处理文件 {file_path} 时出错: {e}")
            print("为避免漏合并，本次不覆盖 merged 文件，也不删除源文件。")
            return
    
    # 3. 先写入同目录临时文件，再原子替换 merged，避免中断时截断旧结果。
    temp_output_path = f"{final_output_path}.tmp"
    try:
        with open(temp_output_path, 'wb') as outfile:
            for item in unique_items.values():
                outfile.write(orjson.dumps(item) + b'\n')
        os.replace(temp_output_path, final_output_path)
        
        print(f"\n全局合并完成。")
        print(f"最终唯一记录总数: {len(unique_items)}")
        print(f"输出路径: {final_output_path}")

    except Exception as e:
        print(f"写入最终 merged 文件时发生错误: {e}")
        if os.path.exists(temp_output_path):
            try:
                os.remove(temp_output_path)
            except OSError as cleanup_error:
                print(f"清理临时合并文件失败: {cleanup_error}")
        print("错误：写入 merged 文件失败，为保护旧结果和源文件，本次不删除任何文件。")
        return

    # 4. 仅在原子替换成功后删除已合并的小文件。
    print(f"\n开始删除 {len(files_to_merge)} 个已合并的源文件...")
    deleted_count = 0
    for file_path in files_to_merge:
        # 双重保险：再次确认不会误删 merged 文件
        if os.path.basename(file_path) == os.path.basename(final_output_path):
            continue
            
        try:
            os.remove(file_path)
            deleted_count += 1
        except Exception as e:
            print(f"删除文件 {file_path} 失败: {str(e)}")
            
    print(f"文件删除完成，成功删除 {deleted_count}/{len(files_to_merge)} 个文件。")


class AsyncProgressBar:
    """线程安全的异步进度条"""
    def __init__(self, total, desc):
        self.pbar = tqdm(total=total, desc=desc, unit="item", mininterval=3)
        self.lock = asyncio.Lock()

    async def update(self, n=1):
        async with self.lock:
            self.pbar.update(n)

    async def close(self):
        async with self.lock:
            self.pbar.close()


async def _read_image_base64_async(image_path: str) -> Tuple[str, str]:
    """异步读取本地图片，返回 MIME 类型和 Base64 文本。"""
    async with aiofiles.open(image_path, "rb") as handle:
        image_data = await handle.read()

    mime_type = mimetypes.guess_type(image_path)[0] or "image/png"
    if not mime_type.startswith("image/"):
        mime_type = "image/png"
    return mime_type, base64.b64encode(image_data).decode("ascii")


class PermanentRequestError(RuntimeError):
    """参数、认证等不可通过相同请求重试解决的服务错误。"""


def split_model_message(message: dict) -> Tuple[str, str]:
    """兼容分离的reasoning_content与内容内<think>；只解析最终回答。"""
    content = message.get("content") or ""
    reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
    if not isinstance(content, str) or not isinstance(reasoning, str):
        raise ValueError("模型content/reasoning必须为字符串")
    if content.lstrip().startswith("<think>"):
        stripped = content.lstrip()
        if "</think>" not in stripped:
            raise ValueError("思考块被截断，尚无最终回答")
        thought, content = stripped[len("<think>"):].split("</think>", 1)
        reasoning = reasoning or thought.strip()
    if "<think>" in content or "</think>" in content:
        raise ValueError("最终回答中存在无法分离的思考标记")
    return content.strip(), reasoning


async def get_api_response(
    prompt: str,
    image_paths: List[str],
    session: aiohttp.ClientSession,
    model_url: str,
    encoded_images: Optional[List[Tuple[str, str]]] = None,
    max_tokens: Optional[int] = None,
) -> dict:
    """发送一次请求。统一由process_item控制传输和语义重试，避免重试相乘。"""
    if encoded_images is None:
        encoded_images = await asyncio.gather(
            *[_read_image_base64_async(path) for path in image_paths]
        )
    prompt_parts = prompt.split("<image>")
    if len(prompt_parts) - 1 != len(encoded_images):
        raise PermanentRequestError("图片占位符与图片数量不一致")
    content_parts = []
    for index, part in enumerate(prompt_parts):
        if part:
            content_parts.append({"type": "text", "text": part})
        if index < len(encoded_images):
            mime, data = encoded_images[index]
            content_parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{data}"},
            })
    payload = {
        "model": CONFIG["model_name"],
        "messages": [{"role": "user", "content": content_parts}],
        "max_tokens": max_tokens or CONFIG["max_tokens"],
        "stream": False,
        "temperature": CONFIG["temperature"],
        "top_p": CONFIG["top_p"],
        "top_k": CONFIG["top_k"],
        "presence_penalty": CONFIG["presence_penalty"],
        "repetition_penalty": CONFIG["repetition_penalty"],
        "chat_template_kwargs": {
            "enable_thinking": CONFIG["enable_thinking"],
            "reasoning_effort": CONFIG["reasoning_effort"],
        },
    }
    timeout = aiohttp.ClientTimeout(
        total=CONFIG["timeout"], sock_connect=CONFIG["connect_timeout"],
        sock_read=CONFIG["timeout"],
    )
    async with session.post(
        model_url, data=orjson.dumps(payload),
        headers={"Content-Type": "application/json"}, timeout=timeout,
    ) as response:
        if response.status != 200:
            body = (await response.text())[:4000]
            detail = f"HTTP {response.status}: {body}"
            if 400 <= response.status < 500 and response.status not in (408, 425, 429):
                raise PermanentRequestError(detail)
            raise RuntimeError(detail)
        data = orjson.loads(await response.read())
    if not isinstance(data, dict):
        raise ValueError("API响应不是对象")
    if data.get("error"):
        raise RuntimeError(f"API返回错误: {data['error']}")
    choices = data.get("choices") or []
    if not choices:
        raise ValueError("API返回空choices")
    choice = choices[0]
    return {
        "finish_reason": choice.get("finish_reason"),
        "message": choice.get("message") or {},
        "usage": data.get("usage") or {},
    }


async def process_item(item: Dict, session: aiohttp.ClientSession, model_url: str) -> Optional[Dict]:
    """思考模式复核；先前标签不覆盖，异常结果不伪装成空标签。"""
    image_paths = item.get("_image_paths", [])
    if len(image_paths) > CONFIG["max_images_per_item"]:
        raise ValueError("图片数量超过上限")
    prompt = build_prompt_from_content(item, item["_question_content"])
    max_tokens = output_budget(item, prompt)
    # 本地图片错误不反复请求；编码在重试之间复用。
    encoded_images = await asyncio.gather(
        *[_read_image_base64_async(path) for path in image_paths]
    )
    failures = []
    item["_attempt_records"] = []
    for attempt in range(1, CONFIG["max_retries"] + 1):
        item["output"] = ""
        item["_last_reasoning"] = ""
        item["_response_meta"] = {"attempt": attempt}
        try:
            response = await get_api_response(
                prompt, image_paths, session, model_url, encoded_images=encoded_images,
                max_tokens=max_tokens,
            )
            message = response["message"]
            # 分离失败时仍留下原响应，供错误审计。
            item["output"] = message.get("content") or ""
            item["_last_reasoning"] = message.get("reasoning_content") or ""
            item["_response_meta"].update({
                "finish_reason": response["finish_reason"], "usage": response["usage"],
            })
            if response["finish_reason"] not in ("stop", "eos_token"):
                raise ValueError(f"模型输出未正常结束: {response['finish_reason']}；不保存截断标签")
            content, reasoning = split_model_message(message)
            item["output"], item["_last_reasoning"] = content, reasoning
            parsed = parse_rerank_result(
                content,
                CONFIG["label_field"],
                CONFIG["analysis_field"],
            )

            result = dict(item)
            result.update(parsed)

            if CONFIG["save_reasoning"]:
                result[CONFIG["reasoning_field"]] = reasoning
            else:
                result.pop(CONFIG["reasoning_field"], None)
            for field in (
                "prompt", "output", "_question_content", "_image_paths", "_candidate_labels",
                "_candidate_sources", "_candidate_hints",
                "_candidate_details", "_last_reasoning", "_response_meta", "_retry_errors", "question_info_cleaned", "second_rerank_analysis_2026_09_20", 
                "_attempt_records",
            ):
                result.pop(field, None)
            return result
        except Exception as error:
            error_text = f"{type(error).__name__}: {error}"
            failures.append({"attempt": attempt, "error": error_text})
            item["_retry_errors"] = failures
            response_meta = dict(item.get("_response_meta") or {})
            attempt_record = {
                "attempt": attempt,
                "error": error_text,
                "finish_reason": response_meta.get("finish_reason"),
                "usage": response_meta.get("usage", {}),
                "model_output": item.get("output", ""),
            }
            if CONFIG["save_reasoning"]:
                attempt_record["reasoning_content"] = item.get("_last_reasoning", "")
            item["_attempt_records"].append(attempt_record)
            if isinstance(error, PermanentRequestError) or attempt == CONFIG["max_retries"]:
                raise
            print(
                f"题目 question_id={item.get(CONFIG['unique_id'])} "
                f"parent_id={item.get('parent_id')} "
                f"第{attempt}次失败，准备重试: {error}"
            )
            await asyncio.sleep(
                CONFIG["retry_backoff_seconds"] * (2 ** (attempt - 1)) + random.uniform(0, 1)
            )
    raise RuntimeError("重试结束但没有结果")


def build_error_log(item: Dict, error_msg: str) -> bytes:
    error_log = {
        "status": "failed",
        "question_id": item.get("question_id"),
        "unique_id": item.get(CONFIG["unique_id"], "无ID"),
        "parent_id": item.get("parent_id"),
        "error": error_msg,
        "model_output": item.get("output", ""),
        "response_meta": item.get("_response_meta", {}),
        "retry_errors": item.get("_retry_errors", []),
        # 每个失败尝试都保存当时的输出、结束原因和 reasoning，避免只看到最后一次空白响应。
        "attempt_records": item.get("_attempt_records", []),
        "candidate_labels": item.get("_candidate_labels", []),
        "candidate_sources": item.get("_candidate_sources", {}),
        "candidate_hints": item.get("_candidate_hints", []),
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if CONFIG["save_reasoning"]:
        error_log["reasoning_content"] = item.get("_last_reasoning", "")
    return orjson.dumps(error_log) + b"\n"


def build_retry_attempt_log(item: Dict, result: Dict) -> bytes:
    """最终成功时仍保存前序失败尝试；正常结果另写结果文件，互不覆盖。"""
    label_ids = result.get(f"{CONFIG['label_field']}_id", [])
    label_name_by_id = id_to_leaf_name
    return orjson.dumps(
        {
            "status": "retry_succeeded",
            "question_id": result.get("question_id"),
            "unique_id": result.get(CONFIG["unique_id"], "无ID"),
            "parent_id": result.get("parent_id"),
            "failed_attempt_count": len(item.get("_attempt_records", [])),
            "attempt_records": item.get("_attempt_records", []),
            "candidate_labels": item.get("_candidate_labels", []),
            "candidate_sources": item.get("_candidate_sources", {}),
            "candidate_hints": item.get("_candidate_hints", []),
            "final_labels": result.get(CONFIG["label_field"], []),
            "final_label_ids": label_ids,
            "final_label_names_by_id": [label_name_by_id.get(str(knw_id)) for knw_id in label_ids],
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    ) + b"\n"


async def write_record(
    record_type: str,
    record_bytes: bytes,
    output_file: str,
    error_log_file: str,
    write_lock: asyncio.Lock,
):
    """每条请求完成后立即追加并刷新；锁只避免多个协程交叉写同一文件。"""
    target_file = output_file if record_type == "result" else error_log_file
    async with write_lock:
        async with aiofiles.open(target_file, "ab") as handle:
            await handle.write(record_bytes)
            await handle.flush()


async def worker(
    session: aiohttp.ClientSession,
    semaphore: Semaphore,
    item: Dict,
    progress_bar: AsyncProgressBar,
    model_url: str,
    output_file: str,
    error_log_file: str,
    write_lock: asyncio.Lock,
):
    """单题请求；请求完成后立即落盘，不等待其他任务。"""
    try:
        async with semaphore:
            result = await process_item(item, session, model_url)

        if result:
            await write_record(
                "result", orjson.dumps(result) + b"\n", output_file, error_log_file, write_lock
            )
            # 前序失败但最终成功：正常结果已经落盘后，再把失败尝试追加到 error 日志。
            if item.get("_attempt_records"):
                try:
                    await write_record(
                        "error",
                        build_retry_attempt_log(item, result),
                        output_file,
                        error_log_file,
                        write_lock,
                    )
                except Exception as write_error:
                    print(
                        f"重试失败记录写入失败（正常结果已保存）: "
                        f"question_id={item.get('question_id')} parent_id={item.get('parent_id')} "
                        f"error={write_error}"
                    )
        else:
            await write_record(
                "error", build_error_log(item, "API响应为空"), output_file, error_log_file, write_lock
            )
    except Exception as e:
        try:
            await write_record(
                "error", build_error_log(item, f"处理异常: {str(e)}"), output_file, error_log_file, write_lock
            )
        except Exception as write_error:
            print(f"错误日志写入失败: {write_error}")
    finally:
        await progress_bar.update()


async def main(
    assigned_items: List[Dict],
    model_config: Dict, 
    url_idx: int
):
    """主函数，处理分配给当前URL的数据"""
    model_url = model_config["url"]
    current_concurrency = model_config.get(
        "concurrency", CONFIG["concurrency_per_process"]
    )
    
    output_dir = CONFIG["output_dir"]
    os.makedirs(output_dir, exist_ok=True)

    # 生成唯一输出文件和错误日志文件
    url_tag = model_url.split("//")[-1].split("/")[0].replace(":",".")
    url_tag = "_".join(url_tag.split(".")[-2:])

    output_file = os.path.join(
        output_dir,
        f"{CONFIG['output_prefix']}_{url_tag}_p{url_idx}.jsonl" 
    )

    error_log_file = os.path.join(
        output_dir,
        f"{CONFIG['output_prefix']}_{url_tag}_p{url_idx}_error.jsonl"
    )

    print(f"[URL={url_tag} (进程{url_idx})] 开始处理 {len(assigned_items)} 条数据")
    if not assigned_items:
        print(f"[URL={url_tag}] 无数据可处理，退出")
        return

    # 连接池配置    
    connector = aiohttp.TCPConnector(
        limit_per_host=current_concurrency * 2,
        limit=0,
        ttl_dns_cache=300,
        use_dns_cache=True,
        enable_cleanup_closed=True
    )
    
    async with aiohttp.ClientSession(connector=connector) as session:
        semaphore = Semaphore(current_concurrency)
        progress_bar = AsyncProgressBar(len(assigned_items), f"[URL={url_tag}] 进度")

        write_lock = asyncio.Lock()
        max_in_flight = current_concurrency * CONFIG["max_in_flight_multiplier"]
        item_iterator = iter(assigned_items)
        pending_tasks = set()

        def create_worker_task(item: Dict):
            return asyncio.create_task(
                worker(
                    session,
                    semaphore,
                    item,
                    progress_bar,
                    model_url,
                    output_file,
                    error_log_file,
                    write_lock,
                )
            )

        try:
            for _ in range(min(max_in_flight, len(assigned_items))):
                pending_tasks.add(create_worker_task(next(item_iterator)))

            while pending_tasks:
                done_tasks, pending_tasks = await asyncio.wait(
                    pending_tasks,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in done_tasks:
                    task.result()

                for _ in done_tasks:
                    try:
                        pending_tasks.add(create_worker_task(next(item_iterator)))
                    except StopIteration:
                        break
        except KeyboardInterrupt:
            print(f"[URL={url_tag}] 用户中断，正在取消未完成请求；已完成结果已逐条落盘...")
            for task in pending_tasks:
                task.cancel()
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        finally:
            await progress_bar.close()


def load_all_data(image_path_dict: Dict[str, str]) -> List[Dict]:
    """读取输入、跳过已完成记录，并构造每个question_id的精排参考池。"""
    read_workers = CONFIG["read_workers"]
    print(f"======== 开始数据加载（文件读取将使用 {read_workers} 个进程） ========")

    # --- 步骤 1: 并行加载已处理题目的 ID ---
    print("\n[步骤 1/3] 并行加载已处理题目的 ID...")
    
    processed_ids = set()
    unique_id_key = CONFIG["unique_id"]

    def read_ids_from_in_parallel(filepath: str) -> Set[Any]:
        """
        使用多进程读取文件，然后提取question_id。
        """
        if not (filepath and os.path.exists(filepath)):
            return set()
        
        items = read_json_and_jsonl(filepath, num_workers=read_workers)
        return {str(item[unique_id_key]) for item in items if item.get(unique_id_key) is not None}

    
    # 读取已处理ID文件
    processed_filepath = CONFIG.get("already_processed_file")
    if processed_filepath:
        processed_ids.update(read_ids_from_in_parallel(processed_filepath))
    
    # 读取中途目录下的文件
    output_dir = CONFIG.get("output_dir", "")
    if os.path.isdir(output_dir):
        for fname in os.listdir(output_dir):
            file_path = os.path.join(output_dir, fname)
            if (
                os.path.isfile(file_path)
                and os.path.getsize(file_path) > 0
                and fname.startswith(CONFIG["output_prefix"])
                and fname.endswith(".jsonl")
                and "error" not in fname
                and "merged" not in fname
            ):
                processed_ids.update(read_ids_from_in_parallel(file_path))

    # 读取历史已处理结果文件（如之前跑完的 20w merged 文件），其中的 unique_id 一并跳过。
    for history_file in CONFIG.get("already_processed_file_list", []):
        history_ids = read_ids_from_in_parallel(history_file)
        processed_ids.update(history_ids)
        print(f"历史已处理文件: {history_file}，读取到 {len(history_ids):,} 条已处理ID。")

    print(f"ID加载完成。已处理ID数量: {len(processed_ids)}")

    # --- 步骤 2: 并行读取主数据文件 ---
    print(f"\n[步骤 2/3] 并行读取主数据文件: {CONFIG['input_file_list']}...")
    all_items = []
    for input_file in CONFIG["input_file_list"]:
        all_items.extend(read_json_and_jsonl(input_file, num_workers=read_workers))
    
    print(f"主数据读取完成，共加载 {len(all_items)} 条独立题目。")

    print("\n[步骤 3/3] 构造精排参考池和题目上下文...")

    selected_items: List[Dict] = []
    selection_counts = {
        "already_processed": 0,
        "duplicate_input": 0,
        "missing_id": 0,
        "empty_candidate_pool": 0,
        "too_many_candidates": 0,
        "candidate_error": 0,
        "missing_image": 0,
        "too_many_images": 0,
        "empty_question_content": 0,
        "question_content_error": 0,
        "prompt_too_long": 0,
    }

    preprocess_error_file = os.path.join(
        CONFIG["output_dir"],
        f"{CONFIG['output_prefix']}_preprocess_error.jsonl",
    )
    os.makedirs(CONFIG["output_dir"], exist_ok=True)

    def write_preprocess_error(handle, item: Dict, category: str, error: str) -> None:
        handle.write(
            orjson.dumps(
                {
                    "question_id": item.get(unique_id_key),
                    "parent_id": item.get("parent_id"),
                    "category": category,
                    "error": error,
                },
                option=orjson.OPT_APPEND_NEWLINE,
            )
        )

    seen_input_ids = set()
    # 追加保留历次预处理错误，防止续跑抹掉此前失败记录。
    with open(preprocess_error_file, "ab") as error_handle:
        for item in all_items:
            if CONFIG["max_items"] and len(selected_items) >= CONFIG["max_items"]:
                break
            raw_key = item.get(unique_id_key)
            if raw_key is None or not str(raw_key).strip():
                selection_counts["missing_id"] += 1
                write_preprocess_error(error_handle, item, "missing_id", "缺少有效question_id")
                continue
            problem_key = str(raw_key)
            if problem_key in processed_ids:
                selection_counts["already_processed"] += 1
                continue
            if problem_key in seen_input_ids:
                selection_counts["duplicate_input"] += 1
                continue
            seen_input_ids.add(problem_key)

            try:
                candidate_labels, candidate_details = build_candidate_pool(item, include_details=True)
                item["_candidate_labels"] = candidate_labels
                item["_candidate_details"] = candidate_details
            except Exception as error:
                error_text = str(error)
                if "候选池为空" in error_text:
                    category = "empty_candidate_pool"
                elif "超过上限" in error_text:
                    category = "too_many_candidates"
                else:
                    category = "candidate_error"
                selection_counts[category] += 1
                write_preprocess_error(error_handle, item, category, error_text)
                continue

            try:
                question_content, image_paths, missing_image = get_question_content(
                    item, image_path_dict
                )
                if missing_image:
                    selection_counts["missing_image"] += 1
                    write_preprocess_error(
                        error_handle, item, "missing_image", "题目图片缺失或占位符数量不一致"
                    )
                    continue
                if len(image_paths) > CONFIG["max_images_per_item"]:
                    selection_counts["too_many_images"] += 1
                    write_preprocess_error(
                        error_handle,
                        item,
                        "too_many_images",
                        f"图片数量{len(image_paths)}超过上限{CONFIG['max_images_per_item']}",
                    )
                    continue
                if not question_content:
                    selection_counts["empty_question_content"] += 1
                    write_preprocess_error(
                        error_handle, item, "empty_question_content", "题目上下文为空"
                    )
                    continue

                item["_question_content"] = question_content
                item["_image_paths"] = image_paths
                # 启动请求前检查文本预算；完整解释在内存复用，不重复拼候选池。
                build_prompt_from_content(item, question_content)
                selected_items.append(item)
            except Exception as error:
                category = "prompt_too_long" if "提示词字符数" in str(error) else "question_content_error"
                selection_counts[category] += 1
                write_preprocess_error(
                    error_handle, item, category, str(error)
                )

    print("\n======== 数据处理完成 ========")
    print(f"精排候选过滤后，最终待处理 {len(selected_items):,} 条。")
    print(f"筛选统计: {selection_counts}")
    print(f"预处理错误日志: {preprocess_error_file}")
    return selected_items


def split_items_equally(items: List[Dict], num_chunks: int) -> List[List[Dict]]:
    """将数据平均分配为num_chunks个子列表"""
    chunk_size = len(items) // num_chunks
    remainder = len(items) % num_chunks

    chunks = []
    start = 0
    for i in range(num_chunks):
        # 前remainder个chunk多分配1条数据
        end = start + chunk_size + (1 if i < remainder else 0)
        chunks.append(items[start:end])
        start = end
    return chunks


def run_chunk(args):
    """分块运行函数，接收(分配的数据, URL配置字典, URL索引)"""
    # spawn子进程不会继承父进程动态加载的映射与配置。
    global leaf_name_to_id, id_to_leaf_name, label_type_by_name
    global leaf_name_to_explanation
    (
        assigned_items,
        model_config,
        url_idx,
        config,
        name_to_id,
        id_to_name,
        types,
        explanations,
    ) = args
    CONFIG.update(config)
    leaf_name_to_id = name_to_id
    id_to_leaf_name = id_to_name
    label_type_by_name = types
    leaf_name_to_explanation = explanations
    rebuild_explanation_reference_index()
    try:
        asyncio.run(main(assigned_items, model_config, url_idx))
    except KeyboardInterrupt:
        print(f"URL索引 {url_idx} 已响应中断信号并停止。")
    except Exception as e:
        print(f"URL索引 {url_idx} 处理失败: {str(e)}")


def run_items_phase(
    items: List[Dict],
    model_urls: List[Dict],
    phase_name: str,
) -> None:
    """使用全部模型端口处理一个阶段，并在中断时收回该阶段的子进程。"""
    if not items:
        print(f"[{phase_name}] 没有待处理数据，跳过该阶段。")
        return

    chunks = split_items_equally(items, len(model_urls))
    print(
        f"[{phase_name}] 待处理 {len(items):,} 条，"
        f"分配给 {len(model_urls)} 个客户端进程。"
    )

    processes = []
    try:
        for url_idx, (chunk, model_config) in enumerate(zip(chunks, model_urls)):
            process = multiprocessing.Process(
                target=run_chunk,
                args=((
                    chunk,
                    model_config,
                    url_idx,
                    CONFIG,
                    leaf_name_to_id,
                    id_to_leaf_name,
                    label_type_by_name,
                    leaf_name_to_explanation,
                ),),
                name=f"{phase_name}-URL-{url_idx + 1}",
            )
            processes.append(process)
            process.start()

        for process in processes:
            process.join()
        print(f"[{phase_name}] 处理完成。")

    except KeyboardInterrupt:
        print(f"[{phase_name}] 收到 Ctrl+C，等待子进程保存已完成结果...")

        for process in processes:
            if process.is_alive():
                process.join(timeout=8)

        for process in processes:
            if process.is_alive():
                print(f"终止未退出子进程: {process.name}")
                process.terminate()
        for process in processes:
            process.join()
        raise


def preview_prompts(items: List[Dict]) -> None:
    """保存几条完整提示词（尽量各取一条小问、整题、非复合题），便于改提示词后人工检查。"""
    count = CONFIG.get("preview_prompt_count", 0)
    if not count:
        return
    picked, seen_scopes = [], set()
    for item in items:
        scope = get_task_scope(item)
        if scope not in seen_scopes:
            picked.append(item)
            seen_scopes.add(scope)
        if len(picked) >= count:
            break
    blocks = []
    for item in picked:
        prompt = build_prompt_from_content(item, item["_question_content"])
        blocks.append(
            f"===== question_id={item.get('question_id')} 范围={get_task_scope(item)} "
            f"候选={len(item['_candidate_labels'])} 图片={len(item.get('_image_paths') or [])} "
            f"字符={len(prompt)} 预计可用输出={output_budget(item, prompt)} =====\n{prompt}"
        )
    preview_path = os.path.join(CONFIG["output_dir"], f"{CONFIG['output_prefix']}_prompt_preview.txt")
    with open(preview_path, "w", encoding="utf-8") as handle:
        handle.write("\n\n".join(blocks))
    print(f"已保存 {len(blocks)} 条提示词预览: {preview_path}")


def run_parallel(model_urls: List[Dict], image_path_dict: Dict[str, str]):
    """主调度函数"""
    if not model_urls:
        print("错误：未提供GPU节点URL")
        return

    # 1. 预读取并过滤所有数据
    filtered_items = load_all_data(image_path_dict)
    if not filtered_items:
        print("警告：过滤后无待处理数据")
        return
    preview_prompts(filtered_items)
    if CONFIG.get("preview_only"):
        print("preview_only=True：只保存提示词预览，不请求模型。")
        return

    # 2. 先跑带图题，再跑无图题；两组都使用全部端口，避免负载不均。
    image_items = [item for item in filtered_items if item.get("_image_paths")]
    no_image_items = [item for item in filtered_items if not item.get("_image_paths")]
    print(
        f"总待处理数据: {len(filtered_items):,}；"
        f"无图 {len(no_image_items):,}；带图 {len(image_items):,}。"
    )
    interrupted = False
    try:
        run_items_phase(image_items, model_urls, "题目-带图")
        run_items_phase(no_image_items, model_urls, "题目-无图")
        print("题目处理完成")

    except KeyboardInterrupt:
        interrupted = True
        print("主进程收到 Ctrl+C：当前阶段已停止，随后合并已落盘结果...")

    finally:
        final_output_path = os.path.join(
            CONFIG['output_dir'],
            f"{CONFIG['output_prefix']}_{CONFIG['temperature']}_merged.jsonl",
        )
        print("开始合并已落盘结果...")
        merge_jsonl_files(
            output_dir=CONFIG["output_dir"],
            model_name=CONFIG["output_prefix"],
            final_output_path=final_output_path,
        )
        if interrupted:
            print("已中断请求并合并已完成结果；下次运行会自动跳过 merged 文件中的题目。")


# ====== 日常配置区（服务与运行参数集中在这里） ======
# 候选来源字段在本配置区的 candidate_source_fields 列表中修改。

# Qwen3.6-35B-A3B-FP8 部署在 164 的 0、1、2、3 号卡，每卡一个服务，--context-length 32768。
base_urls = [
    "http://10.5.1.164:4600/v1/chat/completions",
    "http://10.5.1.164:4601/v1/chat/completions",
    "http://10.5.1.164:4602/v1/chat/completions",
    "http://10.5.1.164:4603/v1/chat/completions",
]


# ====== 配置区 ======
CONFIG = {
    "timeout": 1200,
    # 每题输出（思考 + analysis + labels）上限 14000 token；给复杂复合题留足最终标签输出余量。
    "max_tokens": 14000,

    "temperature": 0.3,
    "top_p": 0.95,
    "top_k": 20,
    # 按 Qwen3.8 官方思考模式推荐不加惩罚；惩罚过大容易语言混杂、公式和化学式写乱。
    "presence_penalty": 0,
    "repetition_penalty": 1.05,

    # 服务端 --context-length，须与部署命令一致；输入 + max_tokens 超过它的题记入预处理错误。
    # 输入估算 = 字符数×tokens_per_char + 图片数×tokens_per_image。
    # 11-05 用 108 条分层样本实测：文本 0.637～0.659 token/字符，单图 252～272 token；这里取略高的值留余量。
    "context_length": 32768,
    "tokens_per_char": 0.68,
    "tokens_per_image": 300,
    "context_margin": 512,
    # 输出预算固定，上下文放不下完整 max_tokens 时不发请求。
    "min_output_tokens": 14000,

    # 每个模型端口拆分的客户端进程数。
    "processes_per_url": 4,
    # 每个客户端进程同时向模型服务发起的最大请求数。
    "concurrency_per_process": 16,
    # 在途任务数 = 当前进程实际并发 × 此倍数；不增加模型服务端并发。
    "max_in_flight_multiplier": 4,
    "max_images_per_item": 20,

    # 标签名称和ID以名称目录为准，释义以指定解释文件为准；解释文件后续可替换更新。
    # 候选解释里展示的字段；精简版只放核心与适用范围、边界及与其他标签区别。
    "explanation_fields": [
        "core_concept",
        "frequently_tested_scenarios",
        "differences_from_other_examination_points",
    ],
    "tree_path": "/DATA/disk8/yexinyong/tagging_knowledge_points_for_questions/data/knowledge_points/chemistry/knowledge_point_tree/process/2026_11_17_tree_explain/2026_11_17_chemistry_label_names_v10.jsonl",
    "explanations_path": "/DATA/disk8/yexinyong/tagging_knowledge_points_for_questions/data/knowledge_points/chemistry/knowledge_point_tree/process/2026_11_17_tree_explain/2026_11_17_chemistry_label_explain_v15.jsonl",

    "input_file_list": [
        "/DATA/disk8/yexinyong/tagging_knowledge_points_for_questions/data/chemistry/model_request/test_dataset/2026_11_17_prompt_test/chemistry_qwen3_5_9b_2026_11_11_0.0_merged_with_final_label_ids.jsonl",
    ],
    # 历史已跑完的结果文件列表：其中的 unique_id 视为已处理，跑新批次时自动跳过。
    "already_processed_file_list": [],

    "output_dir": "/DATA/disk8/yexinyong/tagging_knowledge_points_for_questions/data/chemistry/model_request/train_dataset/cot_gene/2026_11_17/prompt_test",

    # 图片映射（image_id -> 本地图片路径）。留空则数据中带图的题目会跳过，等待处理。
    "image_id_path_mapping_path": "/DATA/disk8/yexinyong/tagging_knowledge_points_for_questions/data/image/chemistry/image_id_path_mapping_compress.jsonl",

    "model_name": "Qwen3.8-27B-FP8",

    "unique_id": "question_id",

    # 参考考点池来源字段（可放标签ID或名称，多字段取并集并按 knw_id 去重）。
    # 精简版只用三路：09-28 Qwen3.8-27B 微调（精度高）、10-23 预标（召回高）、09-20 第二轮精排。
    # 11-05 用 102 条结果统计：原标签 origin_knw_id 精度 52%、召回 29%，单独提供的候选选中率 11%，不再使用。
    "candidate_source_fields": [
        "2026_09_28_qwen3_8_27b_id",
        "2026_11_11_qwen3_5_9b_full_chemistry_id",
        "final_label_ids"
    ],

    "label_field": "cot_label_2026_11_17",
    "analysis_field": "cot_analysis_2026_11_17",
    "reasoning_field": "cot_reasoning_2026_11_17",

    "output_prefix": "2026_11_17_chemistry",
    "enable_thinking": True,
    # Qwen3.6 思考深度：xhigh 会要求“考虑各种可能”，实测容易吃满输出预算；medium 更稳定。
    "reasoning_effort": "medium",
    # 服务端需正确支持 enable_thinking 和 reasoning parser；记录返回的原生思考，便于做 CoT 数据时取舍。
    "save_reasoning": True,
    # 打开关键词规则召回，优先补稀有题型/实验/计算类 T2；常见 T1 仍可补，但排在具体 T2 后面。
    "enable_candidate_hints": True,
    # 规则召回只在当前候选池之外补标签；每题最多净新增 5 个，避免复合题把小问的所有宽泛规则都堆进来。
    "max_candidate_hints_per_item": 5,
    # 稀有、难通过模型候选覆盖的标签放在最高优先级，不受常规 T1/T2 排序影响；名称须在当前目录中。
    "candidate_hint_high_priority_labels": [
        "科学探究-基于理论直接否定猜想",
    ],
    "enable_pair_completion": False,
    # 解释引用展开后候选解释中位数约 18.6k token，32k 上下文放不下，默认关闭。
    "expand_explanation_references": False,
    # 手动固定补充的候选名称（每题都会加）；必须使用当前名称目录中的完整名称，默认不额外添加。
    "extra_candidate_labels": [],
    "max_candidate_labels": 60,
    "max_prompt_chars": 60000,
    "max_output_labels": 30,
    "read_workers": 64,

    "max_retries": 3,
    "retry_backoff_seconds": 2.0,
    "connect_timeout": 15,
    # 0 为全量；初次可改为 100 进行接口/思考模式/输出格式试跑。
    "max_items": 0,
    # 启动时保存几条完整提示词到 output_dir；preview_only=True 时只保存预览，不请求模型。
    "preview_prompt_count": 3,
    "preview_only": False,

    "model_urls": [],
}
# =====================

# 将每个模型端口拆分为多个独立客户端进程。
expanded_model_urls = []
for url in base_urls:
    for _ in range(CONFIG["processes_per_url"]):
        expanded_model_urls.append(
            {"url": url, "concurrency": CONFIG["concurrency_per_process"]}
        )
CONFIG["model_urls"] = expanded_model_urls


CONFIG['already_processed_file'] = (
    os.path.join(
        CONFIG['output_dir'],
        f"{CONFIG['output_prefix']}_{CONFIG['temperature']}_merged.jsonl"
    )
    if CONFIG["output_dir"]
    else None
)


if __name__ == "__main__":

    validate_config()

    tree_path = CONFIG["tree_path"]

    result_list, leaf_name_to_id, id_to_leaf_name, new_name_to_old_name, old_name_to_new_name, old_leaf_name_to_id = load_tree(tree_path)
    load_explanations(CONFIG["explanations_path"])
    validate_prompt_labels()

    image_path_dict = get_image_path_dict(CONFIG["image_id_path_mapping_path"])

    model_urls = CONFIG["model_urls"]
    run_parallel(model_urls, image_path_dict)
