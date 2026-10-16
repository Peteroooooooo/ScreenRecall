# 数据集：标注与划分

这里放**冻结后**的类别表、标注 CSV 和划分清单（随代码提交），用于方案 6.1 / 6.4 的分类实验。frame_id 只对产生它的那份数据库有效，所以每个数据集目录要写明来自哪台机器、哪份 `data\` 备份。不要提交截图本身或真实聊天内容。

流程（命令在仓库根目录运行，Windows 用 `.\.venv\Scripts\python.exe`）：

1. 团队先写标注手册（类别定义、混合画面的正例 / 反例），把 `labelset.example.json` 复制成 `datasets/<名称>/labelset.json` 并冻结。
2. 导出待标注清单：`python -m screenrecall.dataset --data-dir data export --out datasets/<名称>/labels.csv`。只导出 OCR 完成、原图可用的画面；可以用 Excel 打开（UTF-8 BOM）。
3. 标注人填 `task_id`（同一任务 / 同一来源文档用同一编号）、`scenario`、`language`、`label`、`annotator`；第二位成员填 `review_label`、`reviewer`；最终决定写 `final_label`，复核改了类别时必须写 `change_reason`。
4. 校验：`python -m screenrecall.dataset --data-dir data check --labels ... --labelset ...`。
5. 划分：`python -m screenrecall.dataset --data-dir data split --labels ... --labelset ... --fractions <train,val,test> --seed 0 --out datasets/<名称>/split.json`。同一 `task_id`（没填时用录制段）、像素完全相同或 OCR 全文相同的画面强制同组；比例由团队按组数决定，清单里会记录比例、种子、标注文件指纹和各划分的类别分布，并提示缺类。
6. 训练与评测：`python -m screenrecall.classify --data-dir data train --labels ... --labelset ... --split ... --out results/classifier/<名称> --report results/classifier/<名称>/report.json`。划分后再改标注会被拒绝，需要重新划分。
7. 页面显示标签：在 `config.local.toml` 写 `[classify] model_dir = "results/classifier/<名称>"`，重启后台；已有画面用 `python -m screenrecall.classify --data-dir data predict --model results/classifier/<名称>` 补预测。
