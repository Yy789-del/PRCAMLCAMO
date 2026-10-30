# -*- coding: utf-8 -*-
"""
view_data.py — 查看 MySQL risk_audit 库的完整数据

用法:
    python view_data.py            # 打印各表行数统计 + 每表前 20 行明细
    python view_data.py --all      # 打印所有表的完整明细（含审计日志完整 payload）

运行前请设置 UTF-8 编码，避免中文乱码:
    PowerShell: $env:PYTHONIOENCODING="utf-8"; python view_data.py
    cmd:        set PYTHONIOENCODING=utf-8 && python view_data.py
"""
import sys

import pandas as pd

from risk_engine import get_engine

PER_TABLE_LIMIT = 20  # 默认每表最多打印的行数

TABLES = [
    ("customers", "SELECT * FROM customers ORDER BY id"),
    ("accounts", "SELECT * FROM accounts ORDER BY id"),
    ("transactions", "SELECT * FROM transactions ORDER BY id"),
    ("rules", "SELECT id, rule_name, condition_str, risk_level, version, "
              "updated_by, updated_at, is_active FROM rules ORDER BY id"),
    ("cases", "SELECT * FROM cases ORDER BY id"),
    ("audit_logs", "SELECT id, case_id, event_type, event_time, payload "
                   "FROM audit_logs ORDER BY id"),
]


def main() -> None:
    show_all = "--all" in sys.argv
    engine = get_engine()

    # 1. 行数统计
    print("=" * 64)
    print("risk_audit 数据库行数统计")
    print("-" * 64)
    counts = {}
    for name, sql in TABLES:
        c = pd.read_sql_query(f"SELECT COUNT(*) AS c FROM {name}", engine)
        counts[name] = int(c.iloc[0]["c"])
        print(f"  {name:<14} {counts[name]:>5} 行")
    print("=" * 64)

    # 2. 逐表明细
    for name, sql in TABLES:
        df = pd.read_sql_query(sql, engine)
        total = len(df)
        print(f"\n{'=' * 64}\n表 {name}（共 {total} 行）\n{'-' * 64}")
        if not show_all and total > PER_TABLE_LIMIT:
            df = df.head(PER_TABLE_LIMIT)
            print(f"（仅显示前 {PER_TABLE_LIMIT} 行；完整明细用 python view_data.py --all）")
        # 默认模式下截断 payload，避免审计日志刷屏
        if "payload" in df.columns and not show_all:
            df = df.copy()
            df["payload"] = df["payload"].astype(str).str.slice(0, 90) + "…"
        with pd.option_context("display.max_rows", None,
                               "display.max_columns", None,
                               "display.width", 220,
                               "display.max_colwidth", 100):
            print(df.to_string(index=False))

    print(f"\n总计: 客户 {counts['customers']} ｜ 账户 {counts['accounts']} ｜ "
          f"交易 {counts['transactions']} ｜ 规则 {counts['rules']} ｜ "
          f"案件 {counts['cases']} ｜ 审计日志 {counts['audit_logs']}")


if __name__ == "__main__":
    main()
