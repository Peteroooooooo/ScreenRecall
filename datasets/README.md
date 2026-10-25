# 数据集：标注与划分

**采集与标注规程（两人共用）见 [COLLECTION.md](COLLECTION.md)**；任务表、查询分配和模板在 `collection/`。受控截图用 `python -m screenrecall.collect`（清单 / 检查 / 导入 / 标注表 / 事实→相关帧）导入同一个数据目录，`python -m screenrecall.indexer` 在 Mac 或 Windows 上跑完 OCR 与向量；之后按下面的步骤校验、划分、训练、评测。

这里放**冻结后**的类别表、标注 CSV 和划分清单（随代码提交），用于方案 6.1 / 6.4 的分类实验。frame_id 只对产生它的那份数据库有效，所以每个数据集目录要写明来自哪台机器、哪份 `data\` 备份。不要提交截图本身或真实聊天内容。

流程（命令在仓库根目录运行，Windows 用 `.\.venv\Scripts\python.exe`）：

1. 团队先写标注手册（类别定义、混合画面的正例 / 反例），把 `labelset.example.json` 复制成 `datasets/<名称>/labelset.json` 并冻结。
2. 导出待标注清单：`python -m screenrecall.dataset --data-dir data export --out datasets/<名称>/labels.csv`。只导出 OCR 完成、原图可用的画面；可以用 Excel 打开（UTF-8 BOM）。
3. 标注人填 `task_id`（同一任务 / 同一来源文档用同一编号）、`scenario`、`language`、`label`、`annotator`；第二位成员填 `review_label`、`reviewer`；最终决定写 `final_label`，复核改了类别时必须写 `change_reason`。
4. 校验：`python -m screenrecall.dataset --data-dir data check --labels ... --labelset ...`。
5. 划分：`python -m screenrecall.dataset --data-dir data split --labels ... --labelset ... --fractions <train,val,test> --seed 0 --out datasets/<名称>/split.json`。同一 `task_id`（没填时用录制段）、像素完全相同或 OCR 全文相同的画面强制同组；比例由团队按组数决定，清单里会记录比例、种子、标注文件指纹和各划分的类别分布，并提示缺类。
6. 训练与评测：`python -m screenrecall.classify --data-dir data train --labels ... --labelset ... --split ... --out results/classifier/<名称> --report results/classifier/<名称>/report.json`。划分后再改标注会被拒绝，需要重新划分。
7. 页面显示标签：在 `config.local.toml` 写 `[classify] model_dir = "results/classifier/<名称>"`，重启后台；已有画面用 `python -m screenrecall.classify --data-dir data predict --model results/classifier/<名称>` 补预测。

## 检索查询真值与五组实验（方案 6.1、6.2）

1. 按方案写 40 条有答案的主查询：开发 / 盲测各 20 条，每侧每场景（代码、聊天、网页、数字/表格）5 条，整体中英各 20 条。格式见 `queries.example.json`；`relevant` 填相关 frame_id，`rationale` 写判定依据（依据原图与任务记录，不以某个检索器是否命中反推）。第二位成员复核后冻结，冻结后不再改。
2. 检查是否符合查询设计：`python -m screenrecall.evaluate --data-dir data --queries datasets/<名称>/queries.json --split all --out results/retrieval/check --check-only`。
3. 开发阶段只跑 dev：`python -m screenrecall.evaluate --data-dir data --queries ... --split dev --out results/retrieval/<日期>-dev`，据此决定规则与参数。
4. 规则冻结后**只跑一次** blind：`--split blind --out results/retrieval/<日期>-blind`，结果（`results.json` 逐条 + `results.md` 汇总，含查询文件指纹）原样提交，不根据答案补写引号、切换模式或启用放宽匹配。

实验只需要数据目录和模型，可以把 Windows 上录好的 `data\`（整个目录拷贝）拿到 Mac 上跑；frame_id 跟着数据库走。
