# 可执行的检查起点

用项目 Python 执行以下辅助函数。仅依赖已安装的 pandas 和 openpyxl；不能替代业务判断。先完整读完本文件再执行代码。

audit_table 的重复计数是涉及重复的全部行；缺失比例按读取行数计算。safe_rate 仅适用于 0 至 1 的比例，safe_growth 仅适用于正基期。IQR 标记用于敏感性比较，不自动删除数据；非数值、无限值应先核实。公式审计支持 xlsx 中选定工作表，不重算，也不保证缓存新鲜；未缓存的错误公式同样只会记为不可用，不假装识别了计算结果。

```python
import math

import pandas as pd
from openpyxl import load_workbook


def audit_table(df, key_columns=None):
    """只检查传入范围，不修改数据。"""
    rows = len(df)
    missing = {
        str(column): {
            "count": int(df[column].isna().sum()),
            "rate": float(df[column].isna().mean()) if rows else None,
        }
        for column in df.columns
    }
    numeric = df.select_dtypes(include="number")
    return {
        "rows": rows,
        "columns": len(df.columns),
        "missing": missing,
        "zero_counts": {
            str(column): int(numeric[column].eq(0).sum())
            for column in numeric.columns
        },
        "duplicate_rows": int(df.duplicated(keep=False).sum()),
        "duplicate_key_rows": (
            int(df.duplicated(subset=key_columns, keep=False).sum())
            if key_columns else None
        ),
    }


def finite_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def safe_rate(numerator, denominator):
    numerator = finite_number(numerator)
    denominator = finite_number(denominator)
    if numerator is None or denominator is None or denominator <= 0:
        return None
    if numerator < 0 or numerator > denominator:
        return None
    return numerator / denominator


def safe_growth(current, previous):
    current = finite_number(current)
    previous = finite_number(previous)
    if current is None or previous is None or previous <= 0:
        return None
    result = (current - previous) / previous
    return result if math.isfinite(result) else None


def outlier_sensitivity(series):
    """保留原始观测，仅比较候选异常标记的影响。"""
    values = pd.to_numeric(series, errors="raise").dropna()
    if not all(math.isfinite(float(value)) for value in values):
        raise ValueError("存在无限值，请先核实其来源")
    if values.empty:
        return {
            "valid_n": 0, "outlier_count": 0, "mean": None,
            "median": None, "mean_without_flagged": None,
        }
    q1, q3 = values.quantile([0.25, 0.75])
    iqr = q3 - q1
    flagged = (values < q1 - 1.5 * iqr) | (values > q3 + 1.5 * iqr)
    remaining = values[~flagged]
    return {
        "valid_n": len(values),
        "outlier_count": int(flagged.sum()),
        "mean": float(values.mean()),
        "median": float(values.median()),
        "mean_without_flagged": float(remaining.mean()) if len(remaining) else None,
    }


def audit_excel_formulas(path, sheet_name):
    """比较公式与缓存，不进行 Excel 重算。"""
    formulas = load_workbook(path, read_only=True, data_only=False, keep_links=False)
    cached = None
    try:
        cached = load_workbook(path, read_only=True, data_only=True, keep_links=False)
        result = {
            "formula_count": 0, "unavailable_count": 0,
            "cached_error_count": 0, "recalculated": False,
            "cache_freshness": "unknown",
        }
        formula_rows = formulas[sheet_name].iter_rows()
        cached_rows = cached[sheet_name].iter_rows()
        for formula_row, cached_row in zip(formula_rows, cached_rows):
            for formula, value in zip(formula_row, cached_row):
                if formula.data_type != "f":
                    continue
                result["formula_count"] += 1
                if value.value is None:
                    result["unavailable_count"] += 1
                elif value.data_type == "e":
                    result["cached_error_count"] += 1
        return result
    finally:
        formulas.close()
        if cached is not None:
            cached.close()
```

使用方式：先确定粒度和业务键，调用 audit_table；在原始有效明细上另算决定性指标，与主汇总对账。分析率时保留分子/分母，不平均各组率；分析金额先排查关键公式。辅助函数未检查日期覆盖、连接关系、外部链接和公式新鲜度，这些应另行记录。没有日期明细就不能自动对齐期间；没有业务键就不能宣称去重检查已通过。
