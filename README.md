# 化学复合题（大题级）打标脚本包

本包对应“复合题打标”流程：将一个大题的共同题干与全部小题合并后，调用模型输出一份大题级化学九维标签。

## 包含脚本

- `初中数理化相似题标签/初中化学/generate_chemistry_compound_tags.py`：化学运行入口
- `初中数理化相似题标签/tag_compound_science.py`：核心打标、提示词组装、模型调用、断点与审计导出
- `初中数理化相似题标签/初中数学/math_tag_common.py`：题库取题与题目 ID 读取公共模块

## 使用方式

1. 将压缩包内容解压到原项目根目录，保持目录结构；不包含题目 ID 表、标签库 Excel 和 `.env` 配置。
2. 安装依赖：`pip install -r requirements.txt`
3. 在 `初中数理化相似题标签/初中数学/.env` 配置：
   `MENSHEN_TOKEN`、`TIKU_APP_CODE`、`TIKU_BUSINESS_CODE`、`TIKU_APP_TOKEN`、`TIKU_TIMESTAMP`。
4. 进入 `初中数理化相似题标签` 目录运行：

```bash
python 初中化学/generate_chemistry_compound_tags.py --limit 200 --workers 10 --output-dir 初中化学/outputs/复合题打标_全量
```

默认会导出结果 Excel、`模型审计.jsonl`、`中间结果.json`、`运行统计.json` 与人工复核页面。

## 耗时与 token 统计

每次运行结束后，控制台会打印一段统计，同时把内容追加写入输出目录下的 `运行统计.json`
（`当前汇总` 是最近一次，`历次运行` 保留每次断点续跑的记录）：

- **墙钟耗时**：本次运行从开始到结束的实际时间，以及单题平均耗时、其中模型调用占多少。
- **token 用量**：输入、输出、合计。输出会再拆成「思考」与「正式回答」两部分——
  豆包返回的 `completion_tokens` 已包含思考过程，脚本按 `completion_tokens_details.reasoning_tokens` 相减得出正式回答量。
- **本批 / 累计两套口径**：本批只算这次实际处理的题；累计按大题 ID 从 `模型审计.jsonl`
  去重回算（同一题重跑取最后一条），所以断点续跑或重试失败题都不会重复计数。
- 调用失败的题按 0 token 计入，不会虚增。

结果 Excel 末尾新增 `耗时秒`、`输入tokens`、`输出tokens`、`思考tokens` 四列（不影响原有列序）；
人工复核页每张卡片也会显示该题的耗时与 token；`模型审计.jsonl` 每条记录新增
`耗时秒`、`token用量`，`模型元数据` 中新增 `模型耗时秒` 与 `尝试次数`。

> 注：思考过程通常占掉输出 token 的九成以上，而 `max_tokens=4096` 是思考与正式回答共享的。
> 统计里的「思考占输出比例」可以用来判断是否逼近上限；若出现 `finish_reason=length`，
> 脚本会按解析失败处理，此时应上调 `tag_compound_science.py` 中 `model_call` 的 `max_tokens`。
