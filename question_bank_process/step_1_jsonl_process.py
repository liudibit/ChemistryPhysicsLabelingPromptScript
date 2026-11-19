import json
import os
from multiprocessing import Pool
from tqdm import tqdm
from typing import List, Dict, Any
import numpy as np

def parse_question_info(info_dict: Dict[str, Any]) -> Dict[str, Any]:
    """
    解析 questionInfo 字段，提取标准字段
    """
    if not info_dict:
        return {
            "stem": "",
            "options": [],
            "analysis": "",
            "answer": ""
        }
    
    return {
        "stem": info_dict.get("stem", ""),
        "options": info_dict.get("options", []),
        "analysis": info_dict.get("analysis", ""),
        "answer": info_dict.get("answer", "") 
    }

def transform_single_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """
    将单个题目对象（无论是父题还是子题）转换为目标格式
    """
    # 提取 questionInfo
    q_info_raw = item.get("questionInfo", {})
    clean_q_info = parse_question_info(q_info_raw)

    # 提取基础信息
    # 注意：根据你的JSON，businessType 在顶层是字符串，在 questionProperty 里是列表
    # 这里优先取顶层的
    business_type = item.get("business_type", "")
    structure_type = item.get("structure_type", "")
    stem_image_url = item.get("stemImageUrl", "")

    result = {
        "question_id": str(item.get("questionId", "")),
        "parent_id": str(item.get("parentId", "")),
        "question_index": item.get("questionIndex", 0), # 可能是数字或字符串
        "subject": str(item.get("subjectName", "")),
        "business_type": str(business_type),
        "structure_type": str(structure_type),
        "difficulty": str(item.get("difficulty", "")),
        "question_info": clean_q_info,
        "stem_image_url": str(stem_image_url),
    }
    return result

def process_line_chunk(lines: List[str]) -> List[Dict[str, Any]]:
    """
    处理一个文本行块：解析JSON -> 扁平化父子题 -> 返回结果列表
    """
    chunk_results = []
    
    for line in lines:
        line = line.strip()
        if not line:
            continue
        
        try:
            raw_data = json.loads(line)
        except json.JSONDecodeError:
            print(f"JSON解析错误: {line}")
            continue

        # 1. 检查是否有子题 (smallQuestions)
        small_questions = raw_data.get("smallQuestions")
        
        # --- 逻辑分支 ---
        
        # 无论是否有子题，当前这一层（父层）本身也是一条数据（通常包含大题干）
        # 如果是单题，它就是题目本身；如果是大题，它是背景材料
        parent_record = transform_single_item(raw_data)
        
        # 如果是复合题，通常父题的 parent_id 等于它自己的 question_id
        # 我们可以通过判断 small_questions 是否为空来给它打个标记（可选）
        # if small_questions and len(small_questions) > 0:
        #     parent_record["is_composite_header"] = True
        # else:
        #     parent_record["is_composite_header"] = False
            
        chunk_results.append(parent_record)

        # 2. 如果有子题，遍历处理子题
        if small_questions and isinstance(small_questions, list):
            for sub_q in small_questions:
                # 子题的结构通常和父题类似，递归调用转换函数
                child_record = transform_single_item(sub_q)
                
                # 确保子题的 parent_id 指向父题
                # (你的样例数据中已经是这样了，这里是双重保险)
                if not child_record["parent_id"]:
                    child_record["parent_id"] = parent_record["question_id"]
                
                # 补全子题可能缺失的字段（如科目），从父题继承
                if not child_record["subject"]:
                    child_record["subject"] = parent_record["subject"]
                
                chunk_results.append(child_record)

    return chunk_results

def process_jsonl_file(input_path: str, output_path: str, num_processes: int):
    """
    读取JSONL文件，并行处理，并写入新的JSONL文件。
    """
    print(f"开始读取文件: {input_path}")
    
    # 1. 读取所有行到内存 (假设文件大小在内存可承受范围内，例如 < 10GB)
    # 如果文件极大(几十GB)，需要改用流式读取+分块，但通常 1-5GB 的文件直接读没问题
    try:
        with open(input_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
    except Exception as e:
        print(f"读取文件失败: {e}")
        return

    total_lines = len(lines)
    if total_lines == 0:
        print("文件为空，跳过。")
        return

    print(f"文件包含 {total_lines} 行，开始并行处理...")

    # 2. 将行列表分割成块
    # 使用 numpy.array_split 进行切分
    # 注意：lines 是 list，array_split 会把它变成 numpy array，这里我们手动切分以保持 list 类型，减少转换开销
    chunk_size = (total_lines // num_processes) + 1
    chunks = [lines[i:i + chunk_size] for i in range(0, total_lines, chunk_size)]

    all_results = []
    
    # 3. 多进程处理
    with Pool(processes=num_processes) as pool:
        # imap_unordered 顺序不重要，速度最快
        for chunk_res in tqdm(pool.imap_unordered(process_line_chunk, chunks), total=len(chunks), desc="Processing Chunks"):
            all_results.extend(chunk_res)

    print(f"\n处理完成! 原始 {total_lines} 行，扁平化后共生成 {len(all_results)} 条记录。")

    # 4. 写入结果
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        for item in all_results:
            f.write(json.dumps(item, ensure_ascii=False) + '\n')
            
    print(f"结果已保存至: {output_path}")

def main():
    
    input_path = "/home/share_ssd_data/nfs-env/yexinyong/llm/question_bank/tagging_knowledge_points_for_questions/data/process/chemistry/origin/sql_jsonl/chemistry_question_data.jsonl"
    output_path = "/home/share_ssd_data/nfs-env/yexinyong/llm/question_bank/tagging_knowledge_points_for_questions/data/process/chemistry/origin/sql_jsonl/step_1_chemistry_question_data.jsonl"
    
    NUM_PROCESSES = 64
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    process_jsonl_file(input_path, output_path, NUM_PROCESSES)

if __name__ == "__main__":
    main()



