# Python 数据分析指南

## 数据分析工作流程

1. **问题定义** → 2. **数据采集** → 3. **数据清洗** → 4. **探索性分析（EDA）**
→ 5. **特征工程** → 6. **建模分析** → 7. **结果解释** → 8. **部署监控**

## 核心生态

```
┌───────────────────────────────────────┐
│          数据分析生态栈                │
├───────────────────┬───────────────────┤
│   数据操作层       │   可视化层        │
│   Pandas          │   Matplotlib      │
│   NumPy           │   Seaborn         │
│   Polars          │   Plotly          │
├───────────────────┼───────────────────┤
│   统计分析层       │   机器学习层      │
│   SciPy           │   Scikit-learn    │
│   StatsModels     │   XGBoost         │
│   Pingouin        │   LightGBM        │
└───────────────────┴───────────────────┘
```

### Pandas 核心操作

```python
import pandas as pd
import numpy as np

# 读取数据
df = pd.read_csv('data.csv')
df = pd.read_excel('data.xlsx')

# 数据探索
df.head()
df.info()
df.describe()
df.isnull().sum()

# 数据清洗
df.dropna(subset=['重要字段'])
df.fillna(df.mean())
df.drop_duplicates()

# 分组聚合
df.groupby('类别').agg({
    '金额': ['sum', 'mean', 'count'],
    '数量': 'sum'
})

# 多表关联
merged = pd.merge(df_orders, df_customers, on='customer_id', how='left')
```

## 数据可视化

- **折线图**：展示趋势变化
- **柱状图**：对比分类数据
- **直方图**：展示数据分布
- **箱线图**：检测异常值
- **散点图**：观察变量关系
- **热力图**：展示相关性矩阵

## 时间序列分析

```python
from statsmodels.tsa.seasonal import seasonal_decompose
from statsmodels.tsa.stattools import adfuller

# 分解时间序列
decomposition = seasonal_decompose(ts, model='additive', period=12)
trend = decomposition.trend
seasonal = decomposition.seasonal
residual = decomposition.resid

# 平稳性检验
result = adfuller(ts)
print(f'ADF Statistic: {result[0]}')
print(f'p-value: {result[1]}')
```

## 最佳实践

- **可重现性**：使用 Jupyter Notebook 记录完整分析过程
- **版本控制**：数据和代码都应纳入版本管理
- **数据质量**：建立数据质量监控和告警机制
- **结果可解释**：每个分析结论都应能回溯到原始数据
- **性能优化**：大数据集使用分块读取、并行计算等技术
