# 导入必要的库
import os
import json
import orjson
from typing import *
import random
from tqdm import tqdm
from collections import defaultdict, OrderedDict
from copy import deepcopy



def read_json_and_jsonl(input_path):
    """
    使用 orjson 的高效读取函数
    优化点：使用二进制读取模式，利用 orjson 的极速解析能力
    """
    input_path = os.path.abspath(input_path)
    
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"文件不存在: {input_path}")
    
    _, ext = os.path.splitext(input_path)
    ext = ext.lower()
    
    # --- 读取 .json ---
    if ext == ".json":
        # orjson.loads 接受 bytes，所以用 "rb" 读取
        with open(input_path, "rb") as f:
            data = orjson.loads(f.read())
        print(f"读取JSON文件: {input_path}, 数据量: {len(data) if isinstance(data, list) else '非列表结构'}")
        return data
    
    # --- 读取 .jsonl ---
    elif ext == ".jsonl":
        data = []
        with open(input_path, "rb") as f:
            # 一次性读取所有字节并按行分割，速度极快
            lines = f.read().splitlines()
            
        for line_num, line in enumerate(lines, 1):
            # 二进制行的 strip
            line = line.strip()
            if not line:
                continue
            try:
                data.append(orjson.loads(line))
            except orjson.JSONDecodeError:
                print(f"警告: 行 {line_num} 解析失败，已跳过")
                continue
        
        print(f"读取JSONL文件: {input_path}, 有效数据量: {len(data)}/{len(lines)}")
        return data
    
    else:
        raise ValueError(f"不支持的文件格式: {ext}，仅支持.json和.jsonl")


def write_json_and_jsonl(data, output_path):
    """
    使用 orjson 的高效写入函数
    优化点：直接写入 bytes，避免字符串编码开销
    """
    output_path = os.path.abspath(output_path)
    output_dir = os.path.dirname(output_path)
    os.makedirs(output_dir, exist_ok=True)
    
    _, ext = os.path.splitext(output_path)
    ext = ext.lower()
    
    # --- 写入 .json ---
    if ext == ".json":
        with open(output_path, "wb") as f:
            # OPT_INDENT_2: 格式化输出 (2空格缩进)
            # orjson 默认就是 utf-8，无需 ensure_ascii=False
            f.write(orjson.dumps(data, option=orjson.OPT_INDENT_2))
        print(f"写入JSON文件: {output_path}, 数据量: {len(data) if isinstance(data, list) else '非列表结构'}")
    
    # --- 写入 .jsonl ---
    elif ext == ".jsonl":
        with open(output_path, "wb") as f:
            # 使用生成器表达式 + writelines 批量写入，内存效率高
            # orjson.dumps 返回 bytes，我们需要手动添加换行符 b'\n'
            f.writelines(orjson.dumps(item) + b'\n' for item in data)
        print(f"写入JSONL文件: {output_path}, 数据量: {len(data)}")
    
    else:
        raise ValueError(f"不支持的文件格式: {ext}，仅支持.json和.jsonl")


def format_process_grouped(input_path, output_path):
    """
    核心处理函数：
    1. 按 parent_id 分组
    2. 提取并清洗字段
    3. 严格排序：父题在前，子题按 index 排序
    4. 生成一行一个 parent_id 的结构
    """
    print("正在读取数据...")
    input_data = read_json_and_jsonl(input_path)
    
    # 1. 内存中分组
    print("正在根据 parent_id 分组...")
    parent_id_to_items = defaultdict(list)
    for item in input_data:
        pid = item.get("parent_id")
        if pid:
            parent_id_to_items[str(pid)].append(item) # 统一转str防止类型混乱
    
    processed_groups = []
    
    # 2. 遍历处理每个题组
    print("正在重构与排序数据...")
    for parent_id, items in tqdm(parent_id_to_items.items(), desc="Processing Groups"):
        
        parent_item = None
        sub_items = []
        have_image = False
        
        # --- 分离父题与子题 ---
        for item in items:
            qid = str(item.get("question_id"))
            # 确保 parent_id 也是字符串比较
            if qid == parent_id:
                parent_item = item
            else:
                sub_items.append(item)
                
        # 如果没有找到父题（数据缺失），跳过该组
        if not parent_item:
            print(f"警告: 题组 {parent_id} 缺失父题数据，已跳过")
            continue

        # --- 子题排序 ---
        # 按照 question_index 转 int 排序，处理可能为 None 的情况（默认为 0 或 999）
        sub_items.sort(key=lambda x: int(x.get("question_index") if x.get("question_index") is not None else 999))
        
        # --- 构建有序字典 questions_map ---
        # 使用 OrderedDict 确保 JSON 序列化时顺序固定：父题 -> 子题1 -> 子题2 ...
        questions_map = OrderedDict()
        origin_knw_id_by_qid = OrderedDict()
        
        # 定义需要迁移到 questions_map 的字段列表。
        # origin_knw_id 保留在每条输出记录的顶层，不放入 questions_map。
        fields_to_move = ["question_index", "business_type", "structure_type", "difficulty", "create_time"]

        # 1. 处理父题 (排在第一位)
        p_info = deepcopy(parent_item.get("question_info_cleaned", {}))
        if p_info is None: p_info = {} # 防御性编程
        origin_knw_id_by_qid[str(parent_item.get("question_id"))] = parent_item.get(
            "origin_knw_id", p_info.get("origin_knw_id")
        )
        p_info.pop("origin_knw_id", None)
        
        stem_image_url = ""
        if "stem_image_url" in parent_item and parent_item["stem_image_url"]:
            stem_image_url = parent_item["stem_image_url"]
        
        # 迁移外层字段到 info 内部
        for field in fields_to_move:
            if field in parent_item:
                p_info[field] = parent_item[field]
                
        # 放入有序字典
        parent_qid = str(parent_item.get("question_id"))
        questions_map[parent_qid] = p_info
        
        # 2. 处理子题 (按顺序放入)
        child_qids = []
        for sub in sub_items:
            s_info = deepcopy(sub.get("question_info_cleaned", {}))
            if s_info is None: s_info = {}
            sub_qid = str(sub.get("question_id"))
            origin_knw_id_by_qid[sub_qid] = sub.get(
                "origin_knw_id", s_info.get("origin_knw_id")
            )
            s_info.pop("origin_knw_id", None)
            
            # 迁移外层字段
            for field in fields_to_move:
                if field in sub:
                    s_info[field] = sub[field]
                        
            child_qids.append(sub_qid)
            questions_map[sub_qid] = s_info

        
        # --- 组装最终行数据 (修改部分) ---
        shared_metadata = {
            "has_children": len(child_qids) > 0,
            "has_images": any(
                (q.get("question_info_cleaned") or {}).get("has_img") 
                for q in items
            ),
        }
        
        if stem_image_url:
            shared_metadata["stem_image_url"] = stem_image_url

        # 遍历题组内的每一道题（父题和所有子题），生成独立记录
        for target_qid in questions_map.keys():
            new_record = {}
            
            # 1. 第一个插入 question_id，确保它永远在第一行！
            new_record["question_id"] = target_qid
            
            # 2. 插入 parent_id 和 is_stem，方便直观查看
            new_record["parent_id"] = parent_id
            new_record["is_stem"] = (target_qid == parent_id)
            
            if len(questions_map) == 1:
                if target_qid == parent_id:
                    new_record["type"] = "非复合题"
            else:
                if target_qid == parent_id:
                    new_record["type"] = "复合题-题干"
                else:
                    new_record["type"] = f"复合题-第{questions_map[target_qid].get('question_index', 999)}小题"

            # 3. 插入完整的 questions_map 和 metadata
            new_record["questions_map"] = questions_map
            new_record["metadata"] = shared_metadata
            
            # 小题的basic info 放进来，便于查看
            # if not new_record["is_stem"]:
            #     info_copy = questions_map[target_qid].copy()
            #     info_copy.pop("images", None)
            #     info_copy.pop("has_img", None)
            #     info_copy.pop("question_index", None)
            #     info_copy.pop("business_type", None)
            #     info_copy.pop("structure_type", None)
            #     info_copy.pop("difficulty", None)
                
            #     new_record["question_info_cleaned"] = info_copy

            # 标签信息按当前题目写在记录顶层，而非共享的 questions_map 内。
            new_record["origin_knw_id"] = origin_knw_id_by_qid[target_qid]

            # 追加到结果列表中
            processed_groups.append(new_record)

    # 3. 写入文件
    write_json_and_jsonl(processed_groups, output_path)

if __name__ == "__main__":
    # 定义输入文件的路径
    input_path = "/DATA/disk8/yexinyong/tagging_knowledge_points_for_questions/data/primary_school/math/origin/question_bank_database/process/step_1_primary_school_math.jsonl"
        
    # 定义输出文件的路径
    output_path = "/DATA/disk8/yexinyong/tagging_knowledge_points_for_questions/data/primary_school/math/origin/question_bank_database/process/step_2_primary_school_math.jsonl"

    format_process_grouped(input_path, output_path)


