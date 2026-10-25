# A13 实测：断网可用 + 缩放对齐（2026-10-25，DESKTOP-PH4UL0H）

- 系统：Windows-11-10.0.26200-SP0；主显示器原缩放 150%，推荐 150%
- 脚本：`tools/a13_check.py --wifi-off --scales 100,125,150`；结束时缩放已恢复为 150%
- 总结论：通过

## 断网

- 真断 Wi-Fi：是；检查期间外网可达：False；后台和页面接口都在进程内断网壳里运行
- 两个进程尝试对外连接 2 次：screenrecall.daemon dns huggingface.co（utils/_detect_agent.py:196 _fetch_registry）；screenrecall.webapi dns huggingface.co（utils/_detect_agent.py:196 _fetch_registry）
- 说明：`_detect_agent` 是 huggingface_hub 1.33 拼请求头时取“AI 代理名单”（每天一次、失败就当空名单），不受 local_files_only 控制；HF_HUB_OFFLINE 要在 huggingface_hub 被导入前设好才能关掉它。断网时立即失败、不影响功能（调用栈见数据目录 netguard.jsonl）。

| 功能 | 结果 | 说明 |
|---|---|---|
| 文字搜索 | 通过 | frame_id=6b58003e2ef744ef9892164a141d5d73 |
| 画面搜索 | 通过 | results=1, message=None, test_frame_rank=1 |
| 查看原图 | 通过 | size=[1143, 796], bytes=42524 |
| 导入截图并搜到 | 通过 | imported=1, frame_id=ad14da89382e48fca936a7bc8945f493 |

## 缩放 150%（有效 DPI 144）

| 窗口 | 捕获方式 | 截图 / 期望尺寸 | 右下黑边 | 角标比例 · 残差 | 边界偏差 | 文字行 IoU（最小） | 结论 |
|---|---|---|---|---|---|---|---|
| aware | print_window | 1143×796 / 1143×796 | 2 / 2 px | 1.0 · 0.0 px | [-0.5, -0.5] | 0.677（6/6 行） | 通过 |
| unaware | print_window_logical | 761×532 / 761.3×532 | 0 / 1 px | 1.0001 · 0.02 px | [-0.5, -0.0] | 0.653（6/6 行） | 通过 |
| edge | print_window | 1857×1281 / 1857×1281 | 0 / 0 px | 1.5 · 0.0 px | [7.5, 8.0] | 0.764（6/6 行） | 通过 |

页面高亮框（Edge 打开详情页，量 SVG 多边形和图片的相对位置）：

- aware：2 个框，最大偏差 0.0 CSS px（0.0 物理 px），devicePixelRatio 1.5，通过
- unaware：2 个框，最大偏差 0.0 CSS px（0.0 物理 px），devicePixelRatio 1.5，通过
- edge：2 个框，最大偏差 0.0 CSS px（0.0 物理 px），devicePixelRatio 1.5，通过

## 缩放 125%（有效 DPI 120）

| 窗口 | 捕获方式 | 截图 / 期望尺寸 | 右下黑边 | 角标比例 · 残差 | 边界偏差 | 文字行 IoU（最小） | 结论 |
|---|---|---|---|---|---|---|---|
| aware | print_window | 952×664 / 952×664 | 1 / 1 px | 1.0 · 0.01 px | [-0.5, -0.5] | 0.777（6/6 行） | 通过 |
| unaware | print_window_logical | 762×532 / 761.6×532 | 1 / 1 px | 1.0 · 0.02 px | [-0.5, -0.5] | 0.693（6/6 行） | 通过 |
| edge | print_window | 1547×1067 / 1547×1067 | 0 / 0 px | 1.25 · 0.01 px | [6.3, 4.8] | 0.769（6/6 行） | 通过 |

页面高亮框（Edge 打开详情页，量 SVG 多边形和图片的相对位置）：

- aware：2 个框，最大偏差 0.0 CSS px（0.0 物理 px），devicePixelRatio 1.25，通过
- unaware：2 个框，最大偏差 0.0 CSS px（0.0 物理 px），devicePixelRatio 1.25，通过
- edge：2 个框，最大偏差 0.0 CSS px（0.0 物理 px），devicePixelRatio 1.25，通过

## 缩放 100%（有效 DPI 96）

| 窗口 | 捕获方式 | 截图 / 期望尺寸 | 右下黑边 | 角标比例 · 残差 | 边界偏差 | 文字行 IoU（最小） | 结论 |
|---|---|---|---|---|---|---|---|
| aware | print_window | 762×532 / 762×532 | 1 / 1 px | 1.0 · 0.0 px | [-0.5, -0.5] | 0.754（6/6 行） | 通过 |
| unaware | print_window | 762×532 / 762×532 | 1 / 1 px | 1.0 · 0.02 px | [-0.5, -0.5] | 0.717（6/6 行） | 通过 |
| edge | print_window | 1236×853 / 1236×853 | 0 / 0 px | 1.0 · 0.0 px | [5.5, 5.5] | 0.736（6/6 行） | 通过 |

页面高亮框（Edge 打开详情页，量 SVG 多边形和图片的相对位置）：

- aware：2 个框，最大偏差 0.0 CSS px（0.0 物理 px），devicePixelRatio 1，通过
- unaware：2 个框，最大偏差 0.0 CSS px（0.0 物理 px），devicePixelRatio 1，通过
- edge：2 个框，最大偏差 0.0 CSS px（0.0 物理 px），devicePixelRatio 1，通过

## 判定口径

- 截图尺寸：DPI 感知窗口 = 窗口可见边框的物理尺寸；不感知 DPI 的窗口 = 物理尺寸 × 96 / 显示器 DPI（±2 px），并且和库里记录的宽高一致。
- 右下黑边：从右边 / 下边往里连续几乎全黑的列数 / 行数 ≤ 4（区域算错时多出来的部分是黑的；Windows 11 窗口自带 1–2 像素深色边框）。
- 角标：四角品红方块的位置拟合出的比例 = 1（Tk 窗口）或 devicePixelRatio（Edge），残差 < 2 px；Tk 窗口另外用窗口自己报告的画布位置独立算一遍偏移，误差 ≤ 2 px，画布右、下边缘贴着截图边缘（≤ 3.5 px，窗口边框）；Edge 新界面在网页四周自带约 5 CSS px 圆角边距，这一列只记录不判定。
- 文字行：每行已知文字的实际位置（由角标换算）和 OCR 框的 IoU ≥ 0.5。
- 页面高亮框：详情页里 SVG 多边形的屏幕位置和“图片位置 + OCR 坐标 × 显示比例”最大相差 ≤ 1 CSS px。
- 断网：后台和页面接口在进程内断网壳里运行（对外连接和域名解析一律失败并记录）；加 --wifi-off 时同时真断开 Wi-Fi。

叠加了框的截图（绿 = 文字实际位置，红 = OCR 框，蓝圈 = 角标）在数据目录 `overlays/` 里，不提交。
