# M0–M3统一模型比较：组员开发交接说明

## 1. 这次实验回答什么问题

本实验预测产品未来三个月是否出现 **consumer-rating-based operational quality deterioration**。目标仍使用冻结定义：未来平均评分相对历史基线下降至少0.3分，或未来低星占比上升至少10个百分点。

由于目标由消费者Rating变化定义，Rating必须作为共同基础。核心问题不是“哪个纯信号天然最接近Rating目标”，而是：加入Sentiment或Engineering以后，是否能够在Rating已有信息之外提供增量价值。

本阶段属于 `EXPLORATORY_NESTED_SIGNAL_COMPARISON`。现有W6-D结果继续保留，不被覆盖。

## 2. 四条受控路线

| 编号 | 路线 | 研究作用 |
|---|---|---|
| M0 | Rating-only | 共同基础参考 |
| M1 | Rating + Sentiment | 检验Sentiment相对Rating的增量 |
| M2 | Rating + Engineering | 检验Engineering相对Rating的增量 |
| M3 | Rating + Sentiment + Engineering | 检验Engineering在Rating和Sentiment之外的增量 |

论文题目对应的核心比较是 `M3 − M1`。`M2 − M0`是重要的补充比较。如果正文只能展示三条路线，建议展示M0、M1和M3，并将M2放入消融或补充材料。

这里使用的是 **Sentiment**，不是Semantic。Sentiment字段来自冻结的离线VADER结果。

## 3. 统一样本、目标和时间划分

- 数据单位：`parent_asin × review_month`。
- 输入文件：`data/amazon_reviews_2023/processed/product_month_analysis_panel_w6c_v1_0.parquet`。
- 文件SHA-256：`c0f520268b2db674830e56d8e3f2c3fb156ee2b17bc947e1206e08c8ecbf4ac3`。
- 主样本：`eligible_main_h3 == true`，共515条。
- 目标：`target_quality_deterioration_h3`。
- 划分字段：`proposed_split_h3`。
- Train：205条，50个正例。
- Validation：150条，47个正例。
- 两个Embargo：28条和17条。
- Test：115条，本开发阶段完全封存。

不得随机划分、移动月份、降低支持门槛或为了改善结果修改目标。

## 4. 精确特征合同

### M0：Rating-only

```text
feature_mean_rating
feature_low_star_share
feature_n_reviews
feature_historical_rating_mean
feature_historical_low_star_share
feature_historical_n_reviews
```

### M1：Rating + Sentiment

M0的六个字段，加上：

```text
feature_mean_sentiment_compound
feature_negative_sentiment_share
```

### M2：Rating + Engineering

M0的六个字段，加上：

```text
feature_mean_engineering_index_main
feature_predicted_failure_share
feature_mean_failure_probability
```

### M3：Rating + Sentiment + Engineering

M0、两个Sentiment字段和三个Engineering字段全部加入。所有字段名称和顺序已冻结在 `config/nested_model_comparison_rules.toml`。

Severity和Persistence独立信号只能作为预先声明的Engineering消融，不得静默加入M2或M3主路线。

## 5. 所有路线共同使用的模型

四条路线使用同一个Pipeline：

1. `SimpleImputer(strategy="median", add_indicator=True)`；
2. `StandardScaler()`；
3. `LogisticRegression(C=1.0, penalty="l2", solver="liblinear", class_weight="balanced", max_iter=1000, random_state=20260731)`。

缺失填补、缺失指示、标准化和模型都只能在Train上拟合。Validation只能执行transform、`predict_proba`和评价。分类阈值固定为0.5。

## 6. Windows下载与运行

```powershell
git clone https://github.com/zzhengyu668-debug/neoshcool.git
Set-Location .\neoshcool
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements-collaboration.txt
.\.venv\Scripts\python.exe .\scripts\verify_collaboration_package.py --require-release-ready
```

创建个人分支，不要直接在main上开发：

```powershell
git switch -c member/nested-engineering-development
```

执行统一开发脚本：

```powershell
.\.venv\Scripts\python.exe .\scripts\run_nested_model_comparison_development.py --executor your_name
```

运行专项测试：

```powershell
.\.venv\Scripts\python.exe -m pytest .\tests\test_nested_model_comparison_development.py -q
```

结果写入：

```text
outputs/nested_model_comparison/<executor>/development/
```

脚本默认同时运行M0–M3，以便证明四条路线使用相同样本、相同模型参数和相同指标口径。负责Engineering的组员重点检查M2、M3及Engineering消融，不应改写M0和M1的公共定义。

## 7. 必须提交的结果

- 四条路线的Validation指标和预测；
- M1−M0、M2−M0、M3−M1和M3−M2的配对差值；
- 1,000次`parent_asin`分组Bootstrap区间；
- 四个Train-fitted模型文件；
- 精确特征合同；
- 输入、配置、代码和模型SHA-256；
- Test封存审计；
- 无未来字段、目标字段或产品身份进入特征的泄漏审计；
- 改善、无改善和变差结果的完整说明。

主排序指标是PR-AUC（项目口径为Average Precision），其次是Brier Score、Recall和F1。PR-AUC、Recall和F1越高越好；Brier Score越低越好。不要只看Accuracy。

## 8. 实验预期

研究假设是M1可能优于M0，M2可能优于M0，M3可能优于M1。但这些只是待检验假设，不是必须实现的结果。当前已有证据显示Engineering增量并不稳定，因此合理结果包括改善、无明显变化或变差。

技术成功标准是：样本一致、无泄漏、结果可复现、所有结果完整保留。不得继续增加特征、调节阈值或查看Test，直到结果变得符合假设。

## 9. 两人分工

### 周正宇

- 维护统一数据、Pipeline和指标计算框架；
- 负责M0和M1；
- 审核样本一致性、泄漏和Bootstrap结果；
- 汇总四条路线并撰写论文结果。

### Engineering模型组员

- 负责M2和M3的实现复核；
- 完成Failure、Severity、Persistence消融；
- 使用统一脚本，只在Train拟合并在Validation开发；
- 提交独立分支和Pull Request；
- 不读取Test目标，不在Test上选择参数。

双方交叉检查代码和哈希。只有四条路线、特征、阈值和代码全部冻结后，才可共同批准一次性Test评价。

## 10. 禁止事项

- 禁止使用Test选择特征、参数或阈值；
- 禁止加入未来评分、未来低星占比或任何`target_`字段作为特征；
- 禁止把`parent_asin`、`device_type`或月份编号作为预测捷径；
- 禁止更改质量恶化定义；
- 禁止为不同路线选择不同算法或参数；
- 禁止隐藏阴性结果；
- 禁止把该目标称为维修、退货、遥测或真实硬件故障真值。
