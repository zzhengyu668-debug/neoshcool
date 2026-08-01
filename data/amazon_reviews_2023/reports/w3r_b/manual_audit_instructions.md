# W3R-B 盲化产品人工审查指南

## 目的

本审查只判断 Metadata 是否足以证明某个商品属于 **smart bulb** 或 **smart switch**。请独立判断，不要为了扩大样本量降低产品边界，也不要在完成最终裁决前打开 `blind_id_parent_asin_key.csv`。

主审查表已用固定随机种子 20260729 打乱。旧规则的设备类型、排除原因、决策、置信度和审查标签均已隐藏。

## 独立审查流程

1. Reviewer 1 只填写 `reviewer_1_*` 四列。
2. Reviewer 2 在不知道 Reviewer 1 判断的条件下，只填写 `reviewer_2_*` 四列。
3. 两人完成后比较结果；存在分歧时，由裁决人填写 `adjudicated_*` 三列。
4. 不修改 `blind_id` 或 Metadata 证据列。
5. 不使用评论、评分、价格、产品数量目标或旧规则结果作为判断证据。

## Smart bulb 判断标准

必须同时满足：

1. 产品本身明确是灯泡；以及
2. Metadata 明确显示 app、voice、Wi-Fi、Bluetooth、Zigbee、Z-Wave、Matter、HomeKit 等智能控制证据。

排除摄像头灯泡、普通灯泡、灯串、灯具、附件和控制模块。

## Smart switch 判断标准

必须同时满足：

1. 产品本身是开关或调光器；
2. 用于墙壁照明控制；以及
3. Metadata 明确显示 app、voice 或智能家居协议控制证据。

排除网络交换机、继电器模块、遥控器、扬声器切换器、RF-only 产品、普通机械开关及附件。

## device_type 取值

- `smart_bulb`：明确满足智能灯泡标准。
- `smart_switch`：明确满足智能墙壁开关/调光器标准。
- `neither`：明确不属于以上两类。
- `uncertain`：现有 Metadata 无法可靠确定。

## label 取值

- `correct_target`：Metadata 同时支持正确物理产品身份和智能控制功能。
- `false_positive`：不属于批准目标范围，且没有更具体的排除标签。
- `ambiguous`：证据相互冲突，或同时支持多个目标类型，无法唯一判断。
- `wrong_device_type`：Metadata 明确显示它是另一种产品，而不是智能灯泡或智能墙壁开关。
- `accessory`：附件、替换件、安装件、面板、支架或遥控器等。
- `non_smart`：产品身份可能正确，但没有批准的智能控制功能。
- `insufficient_evidence`：Metadata 信息不足，无法证实或排除。

## confidence 取值

- `high`：产品身份和智能控制证据直接且一致。
- `medium`：总体可判断，但有少量边界或缺失信息。
- `low`：判断依赖有限证据，应优先进入裁决。

## 裁决规则

裁决必须回到同一套产品边界，不得按“希望增加产品数量”作决定。若证据仍不足，保留 `ambiguous` 或 `insufficient_evidence`，不要强行纳入。
