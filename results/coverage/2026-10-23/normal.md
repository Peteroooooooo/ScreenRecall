# 捕获覆盖：normal 模式（脚本种子 0，采样间隔 3.0 秒）

- 播放试次 120/120；排错模式提前退出等模式不符而排除 0；来自测试窗口的保存画面 936 张
- **目标条件**（背景 ('none', 'cursor_blink')，事件 ≥6 秒，未提前退出排错模式）：24/24（100%，95% CI 86%–100%）；目标 ≥95%，达到（只针对这一冻结脚本，不外推）
- 全部条件：59/120（49%，95% CI 40%–58%）
- 漏掉的事件 61 个，其中有可见中断标记（unsaved_change）45 个
- 文字核对：已覆盖且 OCR 完成 59 个，找到事件令牌 59 个
- 事件前基线没被保存的试次：1

## 按事件类型

| 取值 | 覆盖 |
| --- | --- |
| aba | 17/40（42%，95% CI 29%–58%） |
| passive_message | 15/40（38%，95% CI 24%–53%） |
| single_char | 27/40（68%，95% CI 52%–80%） |

## 按持续秒数

| 取值 | 覆盖 |
| --- | --- |
| 1 | 1/24（4%，95% CI 1%–20%） |
| 3 | 7/24（29%，95% CI 15%–49%） |
| 4 | 11/24（46%，95% CI 28%–65%） |
| 6 | 19/24（79%，95% CI 60%–91%） |
| 10 | 21/24（88%，95% CI 69%–96%） |

## 按背景

| 取值 | 覆盖 |
| --- | --- |
| big_animation | 6/30（20%，95% CI 10%–37%） |
| cursor_blink | 19/30（63%，95% CI 46%–78%） |
| none | 18/30（60%，95% CI 42%–75%） |
| scroll | 16/30（53%，95% CI 36%–70%） |

## 各条件组合

| 事件 / 秒数 / 背景 | 覆盖 |
| --- | --- |
| aba|10s|big_animation | 1/2（50%，95% CI 9%–91%） |
| aba|10s|cursor_blink | 2/2（100%，95% CI 34%–100%） |
| aba|10s|none | 2/2（100%，95% CI 34%–100%） |
| aba|10s|scroll | 2/2（100%，95% CI 34%–100%） |
| aba|1s|big_animation | 0/2（0%，95% CI 0%–66%） |
| aba|1s|cursor_blink | 0/2（0%，95% CI 0%–66%） |
| aba|1s|none | 0/2（0%，95% CI 0%–66%） |
| aba|1s|scroll | 0/2（0%，95% CI 0%–66%） |
| aba|3s|big_animation | 0/2（0%，95% CI 0%–66%） |
| aba|3s|cursor_blink | 0/2（0%，95% CI 0%–66%） |
| aba|3s|none | 0/2（0%，95% CI 0%–66%） |
| aba|3s|scroll | 0/2（0%，95% CI 0%–66%） |
| aba|4s|big_animation | 1/2（50%，95% CI 9%–91%） |
| aba|4s|cursor_blink | 2/2（100%，95% CI 34%–100%） |
| aba|4s|none | 1/2（50%，95% CI 9%–91%） |
| aba|4s|scroll | 0/2（0%，95% CI 0%–66%） |
| aba|6s|big_animation | 0/2（0%，95% CI 0%–66%） |
| aba|6s|cursor_blink | 2/2（100%，95% CI 34%–100%） |
| aba|6s|none | 2/2（100%，95% CI 34%–100%） |
| aba|6s|scroll | 2/2（100%，95% CI 34%–100%） |
| passive_message|10s|big_animation | 1/2（50%，95% CI 9%–91%） |
| passive_message|10s|cursor_blink | 2/2（100%，95% CI 34%–100%） |
| passive_message|10s|none | 2/2（100%，95% CI 34%–100%） |
| passive_message|10s|scroll | 2/2（100%，95% CI 34%–100%） |
| passive_message|1s|big_animation | 0/2（0%，95% CI 0%–66%） |
| passive_message|1s|cursor_blink | 0/2（0%，95% CI 0%–66%） |
| passive_message|1s|none | 0/2（0%，95% CI 0%–66%） |
| passive_message|1s|scroll | 0/2（0%，95% CI 0%–66%） |
| passive_message|3s|big_animation | 1/2（50%，95% CI 9%–91%） |
| passive_message|3s|cursor_blink | 0/2（0%，95% CI 0%–66%） |
| passive_message|3s|none | 0/2（0%，95% CI 0%–66%） |
| passive_message|3s|scroll | 0/2（0%，95% CI 0%–66%） |
| passive_message|4s|big_animation | 0/2（0%，95% CI 0%–66%） |
| passive_message|4s|cursor_blink | 0/2（0%，95% CI 0%–66%） |
| passive_message|4s|none | 1/2（50%，95% CI 9%–91%） |
| passive_message|4s|scroll | 0/2（0%，95% CI 0%–66%） |
| passive_message|6s|big_animation | 0/2（0%，95% CI 0%–66%） |
| passive_message|6s|cursor_blink | 2/2（100%，95% CI 34%–100%） |
| passive_message|6s|none | 2/2（100%，95% CI 34%–100%） |
| passive_message|6s|scroll | 2/2（100%，95% CI 34%–100%） |
| single_char|10s|big_animation | 1/2（50%，95% CI 9%–91%） |
| single_char|10s|cursor_blink | 2/2（100%，95% CI 34%–100%） |
| single_char|10s|none | 2/2（100%，95% CI 34%–100%） |
| single_char|10s|scroll | 2/2（100%，95% CI 34%–100%） |
| single_char|1s|big_animation | 0/2（0%，95% CI 0%–66%） |
| single_char|1s|cursor_blink | 1/2（50%，95% CI 9%–91%） |
| single_char|1s|none | 0/2（0%，95% CI 0%–66%） |
| single_char|1s|scroll | 0/2（0%，95% CI 0%–66%） |
| single_char|3s|big_animation | 0/2（0%，95% CI 0%–66%） |
| single_char|3s|cursor_blink | 2/2（100%，95% CI 34%–100%） |
| single_char|3s|none | 2/2（100%，95% CI 34%–100%） |
| single_char|3s|scroll | 2/2（100%，95% CI 34%–100%） |
| single_char|4s|big_animation | 0/2（0%，95% CI 0%–66%） |
| single_char|4s|cursor_blink | 2/2（100%，95% CI 34%–100%） |
| single_char|4s|none | 2/2（100%，95% CI 34%–100%） |
| single_char|4s|scroll | 2/2（100%，95% CI 34%–100%） |
| single_char|6s|big_animation | 1/2（50%，95% CI 9%–91%） |
| single_char|6s|cursor_blink | 2/2（100%，95% CI 34%–100%） |
| single_char|6s|none | 2/2（100%，95% CI 34%–100%） |
| single_char|6s|scroll | 2/2（100%，95% CI 34%–100%） |
