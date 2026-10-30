# -*- coding: utf-8 -*-
"""增量追加场景化数据到 MySQL（不重建、不清表、不动原有 10 个案件）。

只做四件事：
1) 追加账户 51-100（客户 51-100 已存在）；
2) 把 19 条占位规则改写为可被引擎解析的条件（版本保持 v1.0）；
3) 追加场景化交易（违规 / 正常 / 边界三类）；
4) 调用 RiskEngine 全量扫描，为违规账户生成新案件与因果事件流。

原有 10 个案件（账户 1-10 命中规则 1 的 v1.0）不会被改动：
引擎复扫时会因 (account_id=1..10, rule_id=1, version=v1.0) 已存在而跳过。
"""
import datetime

from init_db import DB_NAME, FMT, get_conn, build_scenario_transactions, RULES
from risk_engine import RiskEngine


def _count(cur, sql):
    cur.execute(sql)
    return cur.fetchone()[0]


def main():
    base_time = datetime.datetime(2023, 10, 1, 0, 0, 0)
    conn = get_conn(DB_NAME)
    cur = conn.cursor()

    # 1) 追加账户 51-100（幂等：已存在则忽略）
    cur.executemany(
        "INSERT IGNORE INTO accounts (id, customer_id, account_no) VALUES (%s, %s, %s)",
        [(i, i, f"6222020000{i:04d}") for i in range(51, 101)])
    print(f"[1/4] 账户追加完成，当前账户数: {_count(cur, 'SELECT COUNT(*) FROM accounts')}")

    # 2) 改写 19 条占位规则为可解析条件（版本保持 v1.0，属初始化修正）
    now = datetime.datetime.now().strftime(FMT)
    for idx, (name, cond, level) in enumerate(RULES, start=1):
        cur.execute(
            "UPDATE rules SET rule_name=%s, condition_str=%s, risk_level=%s, "
            "updated_by=%s, updated_at=%s WHERE id=%s",
            (name, cond, level, "admin", now, idx))
    print(f"[2/4] 规则已改写为可解析条件，当前规则数: {_count(cur, 'SELECT COUNT(*) FROM rules')}")

    # 3) 追加场景化交易（幂等：账户 51-100 已有交易则跳过）
    already = _count(cur, "SELECT COUNT(*) FROM transactions WHERE account_id BETWEEN 51 AND 100")
    if already:
        print(f"[3/4] 账户 51-100 已有 {already} 笔交易，跳过交易追加")
    else:
        txns, _labels = build_scenario_transactions(base_time)
        cur.executemany(
            "INSERT INTO transactions (account_id, counterparty_account, amount, tx_time) "
            "VALUES (%s, %s, %s, %s)", txns)
        print(f"[3/4] 场景化交易追加完成: {len(txns)} 笔")

    conn.commit()
    conn.close()

    # 4) 调用引擎全量扫描：为新增违规账户生成案件（原有 10 个案件跳过、不变）
    print("[4/4] 正在调用 Semantica 引擎扫描交易并生成案件...")
    summary = RiskEngine().run_full_scan(operator="admin")
    print(f"      命中 {summary['hits']} 次 | 新建案件 {summary['created']} 个 | "
          f"已有案件 {summary['existing']} 个（原 10 个） | 跳过 {summary['skipped']} 个")

    # 统计
    conn = get_conn(DB_NAME)
    cur = conn.cursor()
    print("\n" + "=" * 60)
    print("📊 客户数:", _count(cur, "SELECT COUNT(*) FROM customers"),
          "| 账户数:", _count(cur, "SELECT COUNT(*) FROM accounts"))
    print("💸 交易流水数:", _count(cur, "SELECT COUNT(*) FROM transactions"))
    print("   ├─ 违规账户(51-62):",
          _count(cur, "SELECT COUNT(*) FROM transactions WHERE account_id BETWEEN 51 AND 62"), "笔")
    print("   ├─ 正常账户(63-95):",
          _count(cur, "SELECT COUNT(*) FROM transactions WHERE account_id BETWEEN 63 AND 95"), "笔")
    print("   └─ 边界账户(96-100):",
          _count(cur, "SELECT COUNT(*) FROM transactions WHERE account_id BETWEEN 96 AND 100"), "笔")
    print("📜 规则数:", _count(cur, "SELECT COUNT(*) FROM rules"))
    print("🚨 预警案件数:", _count(cur, "SELECT COUNT(*) FROM cases"))
    print("📝 审计日志数:", _count(cur, "SELECT COUNT(*) FROM audit_logs"))

    print("\n新案件按规则分布（前 10）:")
    cur.execute(
        "SELECT r.rule_name, COUNT(*) c, MIN(c2.account_id) acc_min, MAX(c2.account_id) acc_max "
        "FROM cases c2 JOIN rules r ON r.id = c2.rule_id "
        "WHERE c2.account_id BETWEEN 51 AND 100 "
        "GROUP BY r.rule_name ORDER BY c DESC")
    for row in cur.fetchall():
        print(f"   {row[0]:<20} {row[1]:>2} 个案件  (账户 {row[2]}-{row[3]})")

    print("\n无命中的账户（不违规）:")
    cur.execute(
        "SELECT a.id FROM accounts a WHERE a.id >= 51 "
        "AND NOT EXISTS (SELECT 1 FROM cases c3 WHERE c3.account_id = a.id) ORDER BY a.id")
    ids = [r[0] for r in cur.fetchall()]
    print(f"   共 {len(ids)} 个: {ids}")
    conn.close()


if __name__ == "__main__":
    main()
