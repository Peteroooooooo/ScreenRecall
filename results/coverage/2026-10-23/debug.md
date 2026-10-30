# 捕获覆盖：debug 模式（脚本种子 0，采样间隔 1.0 秒）

- 播放试次 120/120；排错模式提前退出等模式不符而排除 0；来自测试窗口的保存画面 936 张
- **目标条件**（背景 ('none', 'cursor_blink')，事件 ≥4 秒，未提前退出排错模式）：36/36（100%，95% CI 90%–100%）；目标 ≥95%，达到（只针对这一冻结脚本，不外推）
- 全部条件：113/120（94%，95% CI 88%–97%）
- 漏掉的事件 7 个，其中有可见中断标记（unsaved_change）6 个
- 文字核对：已覆盖且 OCR 完成 113 个，找到事件令牌 112 个
- 事件前基线没被保存的试次：1

## 按事件类型

| 取值 | 覆盖 |
| --- | --- |
| aba | 37/40（92%，95% CI 80%–97%） |
| passive_message | 38/40（95%，95% CI 84%–99%） |
| single_char | 38/40（95%，95% CI 84%–99%） |

## 按持续秒数

| 取值 | 覆盖 |
| --- | --- |
| 1 | 17/24（71%，95% CI 51%–85%） |
| 3 | 24/24（100%，95% CI 86%–100%） |
| 4 | 24/24（100%，95% CI 86%–100%） |
| 6 | 24/24（100%，95% CI 86%–100%） |
| 10 | 24/24（100%，95% CI 86%–100%） |

## 按背景

| 取值 | 覆盖 |
| --- | --- |
| big_animation | 28/30（93%，95% CI 79%–98%） |
| cursor_blink | 30/30（100%，95% CI 89%–100%） |
| none | 29/30（97%，95% CI 83%–99%） |
| scroll | 26/30（87%，95% CI 70%–95%） |

## 各条件组合

| 事件 / 秒数 / 背景 | 覆盖 |
| --- | --- |
| aba|10s|big_animation | 2/2（100%，95% CI 34%–100%） |
| aba|10s|cursor_blink | 2/2（100%，95% CI 34%–100%） |
| aba|10s|none | 2/2（100%，95% CI 34%–100%） |
| aba|10s|scroll | 2/2（100%，95% CI 34%–100%） |
| aba|1s|big_animation | 1/2（50%，95% CI 9%–91%） |
| aba|1s|cursor_blink | 2/2（100%，95% CI 34%–100%） |
| aba|1s|none | 1/2（50%，95% CI 9%–91%） |
| aba|1s|scroll | 1/2（50%，95% CI 9%–91%） |
| aba|3s|big_animation | 2/2（100%，95% CI 34%–100%） |
| aba|3s|cursor_blink | 2/2（100%，95% CI 34%–100%） |
| aba|3s|none | 2/2（100%，95% CI 34%–100%） |
| aba|3s|scroll | 2/2（100%，95% CI 34%–100%） |
| aba|4s|big_animation | 2/2（100%，95% CI 34%–100%） |
| aba|4s|cursor_blink | 2/2（100%，95% CI 34%–100%） |
| aba|4s|none | 2/2（100%，95% CI 34%–100%） |
| aba|4s|scroll | 2/2（100%，95% CI 34%–100%） |
| aba|6s|big_animation | 2/2（100%，95% CI 34%–100%） |
| aba|6s|cursor_blink | 2/2（100%，95% CI 34%–100%） |
| aba|6s|none | 2/2（100%，95% CI 34%–100%） |
| aba|6s|scroll | 2/2（100%，95% CI 34%–100%） |
| passive_message|10s|big_animation | 2/2（100%，95% CI 34%–100%） |
| passive_message|10s|cursor_blink | 2/2（100%，95% CI 34%–100%） |
| passive_message|10s|none | 2/2（100%，95% CI 34%–100%） |
| passive_message|10s|scroll | 2/2（100%，95% CI 34%–100%） |
| passive_message|1s|big_animation | 2/2（100%，95% CI 34%–100%） |
| passive_message|1s|cursor_blink | 2/2（100%，95% CI 34%–100%） |
| passive_message|1s|none | 2/2（100%，95% CI 34%–100%） |
| passive_message|1s|scroll | 0/2（0%，95% CI 0%–66%） |
| passive_message|3s|big_animation | 2/2（100%，95% CI 34%–100%） |
| passive_message|3s|cursor_blink | 2/2（100%，95% CI 34%–100%） |
| passive_message|3s|none | 2/2（100%，95% CI 34%–100%） |
| passive_message|3s|scroll | 2/2（100%，95% CI 34%–100%） |
| passive_message|4s|big_animation | 2/2（100%，95% CI 34%–100%） |
| passive_message|4s|cursor_blink | 2/2（100%，95% CI 34%–100%） |
| passive_message|4s|none | 2/2（100%，95% CI 34%–100%） |
| passive_message|4s|scroll | 2/2（100%，95% CI 34%–100%） |
| passive_message|6s|big_animation | 2/2（100%，95% CI 34%–100%） |
| passive_message|6s|cursor_blink | 2/2（100%，95% CI 34%–100%） |
| passive_message|6s|none | 2/2（100%，95% CI 34%–100%） |
| passive_message|6s|scroll | 2/2（100%，95% CI 34%–100%） |
| single_char|10s|big_animation | 2/2（100%，95% CI 34%–100%） |
| single_char|10s|cursor_blink | 2/2（100%，95% CI 34%–100%） |
| single_char|10s|none | 2/2（100%，95% CI 34%–100%） |
| single_char|10s|scroll | 2/2（100%，95% CI 34%–100%） |
| single_char|1s|big_animation | 1/2（50%，95% CI 9%–91%） |
| single_char|1s|cursor_blink | 2/2（100%，95% CI 34%–100%） |
| single_char|1s|none | 2/2（100%，95% CI 34%–100%） |
| single_char|1s|scroll | 1/2（50%，95% CI 9%–91%） |
| single_char|3s|big_animation | 2/2（100%，95% CI 34%–100%） |
| single_char|3s|cursor_blink | 2/2（100%，95% CI 34%–100%） |
| single_char|3s|none | 2/2（100%，95% CI 34%–100%） |
| single_char|3s|scroll | 2/2（100%，95% CI 34%–100%） |
| single_char|4s|big_animation | 2/2（100%，95% CI 34%–100%） |
| single_char|4s|cursor_blink | 2/2（100%，95% CI 34%–100%） |
| single_char|4s|none | 2/2（100%，95% CI 34%–100%） |
| single_char|4s|scroll | 2/2（100%，95% CI 34%–100%） |
| single_char|6s|big_animation | 2/2（100%，95% CI 34%–100%） |
| single_char|6s|cursor_blink | 2/2（100%，95% CI 34%–100%） |
| single_char|6s|none | 2/2（100%，95% CI 34%–100%） |
| single_char|6s|scroll | 2/2（100%，95% CI 34%–100%） |
