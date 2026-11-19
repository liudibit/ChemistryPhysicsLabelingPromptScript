# -*- coding: utf-8 -*-
"""
@File    : data_cleaning_parallel.py
@Description:
    该脚本用于对包含复杂HTML和多种数据结构的JSONL（每行一个JSON对象）文件进行深度清洗和标准化。
    核心功能包括：
    1. HTML标签清洗与内容提取。
    2. 文本格式（如全角/半角、特殊符号）的归一化。
    3. 题目选项、答案、题干、解析等字段的结构化格式整理。
    4. 针对图片、占位符等特定“噪音”的精准移除。
    为了高效处理大型文件，脚本采用了多进程并行处理架构。
"""

import os
import json
import orjson
import re
from bs4 import BeautifulSoup
import logging
from tqdm import tqdm
from typing import *
from multiprocessing import Pool, cpu_count

# --- 全局配置 ---

# NUM_PROCESSES: 使用的进程数。
# os.cpu_count() 会自动获取您机器的CPU核心数。
# min(50, ...) 是一个保护性措施，防止在核心数极多的服务器上创建过多进程，导致资源开销过大。
NUM_PROCESSES = min(64, cpu_count()) 

# CHUNK_SIZE: 每个进程一次处理的行数。这是一个重要的性能调优参数。
# - 如果值太小，进程间通信和任务调度的开销会相对变大，可能降低效率。
# - 如果值太大，内存占用会增加，且任务分配可能不均（例如，最后一个数据块可能很小）。
# 对于纯文本处理，5000到20000之间通常是一个不错的平衡点。
CHUNK_SIZE = 30000

# 配置日志系统，用于记录处理过程中的错误信息。
logging.basicConfig(
    filename='data_cleaning_parallel.log', # 日志文件名
    level=logging.INFO,                     # 日志级别，INFO及以上级别（WARNING, ERROR）都会被记录
    format='%(asctime)s - %(levelname)s - %(message)s', # 日志格式
    filemode='w'                            # 写入模式，'w'表示每次运行都覆盖旧日志
)

# --- 核心清洗函数 ---
# 这些函数是“纯函数”，意味着给定相同的输入，总是返回相同的输出，且不产生副作用。
# 这使得它们可以被多个进程安全地、独立地调用。

def convert_sup_sub_to_latex_robust(text: str) -> str:
    """
    将HTML中的上下角标标签 (<sup>, <sub>) 转换为LaTeX格式。
    同时处理了 </sup><sup> 这种无意义的连续标签情况，并兼容带有属性的标签。
    """
    # 场景: 'x</sup><sup class="a">2</sup>' -> 'x<sup>2</sup>'
    while re.search(r'</sup>\s*<sup[^>]*>', text):
        text = re.sub(r'</sup>\s*<sup[^>]*>', '', text)
        
    # 场景: 'H</sub><sub style="...">2</sub>' -> 'H<sub>2</sub>'
    while re.search(r'</sub>\s*<sub[^>]*>', text):
        text = re.sub(r'</sub>\s*<sub[^>]*>', '', text)
        
    # 转换为LaTeX上角标: 兼容 <sup word-font="Times New Roman">content</sup> -> ^{content}
    text = re.sub(r'<sup[^>]*>\s*(.*?)\s*</sup>', r'^{\1}', text)
    
    # 转换为LaTeX下角标: 兼容 <sub word-font-size="15px">content</sub> -> _{content}
    text = re.sub(r'<sub[^>]*>\s*(.*?)\s*</sub>', r'_{\1}', text)
    
    return text


def normalize_text_and_extract_imgs(text: Any) -> Tuple[str, List[str]]:
    """
    核心清洗函数。
    
    Returns:
        Tuple[str, List[str]]: (清洗后的文本(含 <image> 占位符), 提取出的图片URL列表)
    """
    if not text or not isinstance(text, str):
        return "", []
    
    extracted_urls = []
    
    # --- 步骤 1: 提取图片并替换为临时 Token ---
    # 使用临时 Token 是为了防止 BeautifulSoup 在步骤 3 中把 <image> 标签当做非法 HTML 清除掉
    TEMP_IMG_TOKEN = "[[__IMG_TOKEN__]]"
    
    def extract_and_replace(match):
        full_tag = match.group(0)
        # 提取 src，兼容单引号和双引号
        src_match = re.search(r'src=[\'"](.*?)[\'"]', full_tag)
        if src_match:
            url = src_match.group(1)
            extracted_urls.append(url)
            return TEMP_IMG_TOKEN
        return "" # 如果没有 src，直接删除该 img 标签

    # 执行替换
    text = re.sub(r'<img[^>]*>', extract_and_replace, text)

    # --- 步骤 2: 常规文本清洗 ---
    text = convert_sup_sub_to_latex_robust(text)
    text = text.replace("$(\\qquad)$", "(   )")
    text = re.sub(r'<span[^>]*class=[\'"][^\'"]*underline fillblank[^\'"]*[\'"][^>]*>.*?</span>', '______', text)
    
    # --- 步骤 3: BeautifulSoup 移除剩余 HTML 标签 ---
    soup = BeautifulSoup(text, 'html.parser')
    
    for table in reversed(soup.find_all('table')):
        try:
            md_table = []
            rows = table.find_all('tr')
            if not rows:
                continue
                
            has_added_header_line = False
            
            for row in rows:
                cols = row.find_all(['td', 'th'])
                if not cols:
                    continue # 跳过没有单元格的空行
                
                col_texts = []
                for col in cols:
                    # 提取文本，并将单元格内部的所有空白和换行符替换为单个空格
                    # 这是为了防止单元格内的换行破坏 Markdown 表格的单行结构
                    cell_text = re.sub(r'\s+', ' ', col.get_text(separator=' ', strip=True))
                    # 替换掉可能干扰 Markdown 表格的管道符
                    cell_text = cell_text.replace('|', '\\|') 
                    col_texts.append(cell_text)
                
                md_table.append("| " + " | ".join(col_texts) + " |")
                
                # 在第一行有效数据后，添加 Markdown 表头分隔线
                if not has_added_header_line:
                    md_table.append("|" + "|".join(["---"] * len(cols)) + "|")
                    has_added_header_line = True
                    
            if md_table:
                # 替换原始 table 标签
                table.replace_with(" \n" + "\n".join(md_table) + "\n ")
        except Exception as e:
            print(f"Error processing table: {e}")
    
    cleaned_text = soup.get_text(separator=' ') 

    # --- 步骤 4: 后处理纯文本 ---
    cleaned_text = re.sub(r'(?:\$\s*)+', ' ', cleaned_text)
    cleaned_text = re.sub(r'\s*(?:\\!\s*)*%\s*(?:\\!\s*)*(?:\\text{\s*})?', r'\%', cleaned_text)
    
    # 全角转半角
    full_width_punctuations_to_keep = {'，', '。', '；', '：', '？', '！', '．'}
    result = []
    for char in cleaned_text:
        code = ord(char)
        if 0xFF01 <= code <= 0xFF5E and char not in full_width_punctuations_to_keep:
            result.append(chr(code - 0xFEE0))
        else:
            result.append(char)
    normalized_str = "".join(result)

    # 符号标准化
    replacements = {
        '﹣': "-", '−': "-", '～': "~",
        '≥': '>=', '≤': '<=', '≠': '!=',
        '\u3000': " ", '\u00A0': " ",
        '…': '...',      
        '\r': ""
    }
    for old, new in replacements.items():
        normalized_str = normalized_str.replace(old, new)
        
    normalized_str = re.sub(r'[^\S\n]+', ' ', normalized_str).strip()

    # --- 步骤 5: 将临时 Token 替换为最终的 <image> 占位符 ---
    # 注意：这里我们统一用 <image>，这是目前 LLaVA 等模型最通用的占位符
    normalized_str = normalized_str.replace(TEMP_IMG_TOKEN, "<image>")

    return normalized_str, extracted_urls

def format_options_and_extract_imgs(options: List[Dict[str, Any]]) -> Tuple[str, List[str]]:
    """
    格式化选项并提取其中的图片。
    """
    if not options or not isinstance(options, list):
        return "", []
    
    formatted_parts = []
    all_options_urls = []
    
    for option_data in options:
        label = option_data.get('title')        # "A", "B"
        content_html = option_data.get('htmlCode') 
        
        if label and content_html:
            cleaned_content, urls = normalize_text_and_extract_imgs(content_html)
            formatted_parts.append(f"{label.strip()}. {cleaned_content}")
            all_options_urls.extend(urls)
            
    if not formatted_parts:
        return "", []
    
    return "\n".join(formatted_parts), all_options_urls


def format_answer(answer: Any) -> Tuple[str, List[str]]:
    """
    清洗答案文本并提取图片链接。
    返回: (清洗后的文本字符串, 图片链接列表)
    """
    if not answer: 
        return "", []

    all_urls = []

    # 情况1：输入是字符串
    if isinstance(answer, str):
        cleaned_answer, ans_urls = normalize_text_and_extract_imgs(answer)
        if ans_urls:
            all_urls.extend(ans_urls)
        # 移除末尾标点
        final_text = cleaned_answer.rstrip('.;,') if cleaned_answer else ""
        return final_text, all_urls

    # 情况2：输入是列表（可能包含嵌套的字典或列表）
    if isinstance(answer, list):
        text_parts = []
        for ans_group in answer:
            raw_answers_list = []
            
            # 提取原始答案列表
            if isinstance(ans_group, dict):
                raw_answers_list = ans_group.get('answers', [])
            elif isinstance(ans_group, list):
                raw_answers_list = ans_group
            
            if not isinstance(raw_answers_list, list): 
                continue
            
            # 处理每一组中的答案
            group_texts = []
            for a in raw_answers_list:
                # 假设 normalize_text_and_extract_imgs 返回 (text, [urls])
                cleaned_text, urls = normalize_text_and_extract_imgs(a)
                
                if cleaned_text:
                    group_texts.append(cleaned_text)
                if urls:
                    all_urls.extend(urls)
            
            # 将同一组的答案用顿号连接
            ans_str = "、".join(group_texts)
            if ans_str:
                text_parts.append(ans_str)
        
        # 不同组的答案用分号连接
        final_text = "； ".join(text_parts) if text_parts else ""
        return final_text, all_urls

    return "", []

def post_process_text(text: str) -> str:
    if not text: return ""
    text = text.replace("()", "(   )")
    text = re.sub(r'\s+(?=[。，！？；：”’）】])', '', text)
    return text.strip()

def process_line(line: bytes) -> Optional[bytes]:
    """
    处理单行JSONL数据。
    生成结构：stem(含<image>), options(含<image>), images(URL列表), analysis, answer
    """
    try:
        data = orjson.loads(line)
        question_info = data.get('question_info', {})
        
        if not isinstance(question_info, dict):
            question_info = json.loads(question_info)
        
        raw_stem = question_info.get('stem', "")
        raw_options = question_info.get('options', [])
        
        # --- 1. 处理题干 (Stem) ---
        cleaned_stem, stem_urls = normalize_text_and_extract_imgs(raw_stem)
        cleaned_stem = post_process_text(cleaned_stem)

        # --- 2. 处理选项 (Options) ---
        formatted_options, options_urls = format_options_and_extract_imgs(raw_options)

        # --- 3. 处理解析 (Analysis) - 纯文本 ---
        cleaned_analysis, analysis_urls = normalize_text_and_extract_imgs(question_info.get('analysis'))
        cleaned_analysis = post_process_text(cleaned_analysis)

        # --- 4. 处理答案 (Answer) ---
        formatted_answer, ans_urls = format_answer(question_info.get('answer'))
        
        # --- 5. 合并图片列表 ---
        images_dict = {
            'stem': stem_urls,
            'options': options_urls,
            'analysis': analysis_urls,
            'answer': ans_urls
        }
        total_img_count = len(stem_urls) + len(options_urls) + len(analysis_urls) + len(ans_urls)
        
        # 清理原始字段
        if 'question_info' in data:
            del data['question_info']
            
        # 构建新结构
        data['question_info_cleaned'] = {
            'stem': cleaned_stem,           
            'options': formatted_options,        
            'analysis': cleaned_analysis,
            'answer': formatted_answer,
            'images': images_dict,             
            'has_img': total_img_count > 0   
        }
        return orjson.dumps(data)
    
    except Exception as e:
        # 简单的错误捕获，避免单个数据损坏导致进程崩溃
        return None

# --- 并行处理框架 ---

def process_chunk(chunk: List[bytes]) -> List[bytes]:
    results = []
    for line in chunk:
        if not line.strip(): continue
        processed = process_line(line)
        if processed is not None:
            results.append(processed)
    return results

def parallel_process_file(input_file: str, output_file: str):
    print(f"开始处理文件: {input_file}")
    print(f"使用 {NUM_PROCESSES} 个进程，每个数据块大小为 {CHUNK_SIZE} 行。")
    print(f"使用 orjson 进行极速处理。")

    os.makedirs(os.path.dirname(output_file), exist_ok=True)

    try:
        # 步骤1: 快速计算总行数 (既然内存大，且为了进度条准确，还是算一下)
        # 如果文件极大（几十G），这一步也可以跳过，直接用文件大小估算进度
        print("正在计算文件总行数...")
        total_lines = 0
        with open(input_file, 'rb') as f: # 使用二进制读取更快
            while True:
                buffer = f.read(1024 * 1024 * 16) # 每次读16MB
                if not buffer:
                    break
                total_lines += buffer.count(b'\n')
        
        if total_lines == 0:
            print("文件为空或只有一行（无换行符），尝试按非空处理。")
            # 简单回退逻辑，防止完全空文件
            total_lines = 1 

        total_chunks = (total_lines + CHUNK_SIZE - 1) // CHUNK_SIZE
        print(f"文件总计约 {total_lines} 行，将被分为 {total_chunks} 个数据块。")

        # 步骤2: 并行处理
        # 使用 'rb' 和 'wb' 模式，因为 orjson 处理的是 bytes，这样完全避免了 Python层面的 str<->bytes 转换开销
        with open(input_file, 'rb') as f_in, \
             open(output_file, 'wb') as f_out, \
             Pool(processes=NUM_PROCESSES) as pool:

            def read_chunks_optimized(file_handle, size):
                """
                优化后的读取生成器。
                使用 file_handle.readlines(hint) 可以比逐行读取更快，
                但为了严格控制 chunk size 这里的逻辑保持简单有效。
                """
                chunk = []
                # 既然内存大，我们可以一次性读多行，减少迭代器开销
                # 这里使用 islice 或者直接循环
                try:
                    for line in file_handle:
                        chunk.append(line)
                        if len(chunk) >= size:
                            yield chunk
                            chunk = []
                    if chunk:
                        yield chunk
                except Exception as e:
                    logging.error(f"读取文件出错: {e}")

            chunks_generator = read_chunks_optimized(f_in, CHUNK_SIZE)
            
            # 使用 imap_unordered
            pbar = tqdm(pool.imap_unordered(process_chunk, chunks_generator), total=total_chunks, desc="正在处理")
            
            for processed_chunk in pbar:
                # 批量写入，减少 I/O 次数
                # processed_chunk 是 bytes 列表
                # join 效率极高
                if processed_chunk:
                    f_out.write(b'\n'.join(processed_chunk) + b'\n')
                    
        print(f"\n处理完成! 清洗后的文件已保存至: {output_file}")

    except FileNotFoundError:
        print(f"错误：输入文件未找到 -> {input_file}")
    except Exception as e:
        print(f"处理过程中发生未知错误: {e}")
        import traceback
        traceback.print_exc()



if __name__ == "__main__":
    
    # 定义输入和输出目录
    input_file = '/DATA/disk8/yexinyong/tagging_knowledge_points_for_questions/data/primary_school/math/origin/question_bank_database/math_question_data.jsonl'
    
    output_file = "/DATA/disk8/yexinyong/tagging_knowledge_points_for_questions/data/primary_school/math/origin/question_bank_database/process/step_1_primary_school_math.jsonl"
    
    parallel_process_file(input_file, output_file)
            
            
            
            