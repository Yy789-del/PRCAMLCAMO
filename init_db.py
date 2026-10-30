import random
import datetime
import json

import pymysql

# ---------------------------------------------------------------------------
# MySQL 连接配置（按你的实际情况修改 user / password）
# 注意：risk_engine.py 中也有一份 DB_CONFIG，两处需保持一致
# ---------------------------------------------------------------------------
DB_SERVER = {
    "host": "localhost",
    "port": 3306,
    "user": "root",
    "password": "123456",   # TODO: 改成你的 MySQL 密码
    "charset": "utf8mb4",
}
DB_NAME = "risk_audit"

FMT = "%Y-%m-%d %H:%M:%S"


def get_conn(database=None):
    cfg = dict(DB_SERVER)
    if database:
        cfg["database"] = database
    return pymysql.connect(**cfg)


# ---------------------------------------------------------------------------
# 场景化交易生成（账户 51-100）：违规 / 正常 / 边界 三类情况
# ---------------------------------------------------------------------------
# 违规场景：每个场景 2 个账户，在同一个 24h 窗口内制造高风险聚集
# (场景名, 账户元组, 24h内笔数, 金额下限, 金额上限, 对手方个数, 对手前缀, 日偏移, 是否夜间)
VIOLATION_SCENARIOS = [
    ("分散转出-多对手方",  (51, 52),  8, 29000, 32000, 6, "77", 0, False),
    ("大额单笔-巨额",      (53, 54),  3, 60000, 90000, 2, "66", 1, False),
    ("夜间高频",           (55, 56), 10, 20000, 26000, 5, "55", 2, True),
    ("高频拆分-加剧",      (57, 58), 12, 17000, 21000, 6, "44", 3, False),
    ("聚合转入-大额",      (59, 60),  6, 42000, 50000, 2, "33", 4, False),
    ("集中转出-多笔大额",  (61, 62),  7, 35000, 50000, 4, "22", 5, False),
]
NORMAL_ACCOUNTS = list(range(63, 96))     # 33 个：正常账户（不违规）
BOUNDARY_ACCOUNTS = list(range(96, 101))  # 5 个：边界账户（接近阈值但不触发）

# 20 条规则（规则 1 为原重点规则，其余 19 条覆盖多种违规类型；条件均可被引擎解析）
RULES = [
    ("疑似拆分交易",       "24h笔数>=5 AND 总额>100000 AND 对手数>=3",  "HIGH"),
    ("分散转出-多对手方",  "总额>200000 AND 24h笔数>=8 AND 对手数>=5",  "HIGH"),
    ("分散转出-对手集中",  "总额>200000 AND 对手数>=6",                 "MEDIUM"),
    ("大额单笔-巨额",      "单笔最大>40000 AND 对手数>=2",              "MEDIUM"),
    ("大额单笔-高均值",    "平均单笔>40000 AND 24h笔数>=2",             "MEDIUM"),
    ("夜间高频-笔数",      "总额>200000 AND 夜间笔数>=4 AND 24h笔数>=8", "HIGH"),
    ("夜间高频-占比",      "总额>200000 AND 夜间占比>=50",              "MEDIUM"),
    ("高频拆分-加剧",      "总额>200000 AND 24h笔数>=10 AND 对手数>=5",  "HIGH"),
    ("高频拆分-高总额",    "总额>250000 AND 24h笔数>=10",               "HIGH"),
    ("聚合转入-大额",      "总额>250000 AND 24h笔数>=5",                "HIGH"),
    ("集中转出-多笔大额",  "单笔最大>40000 AND 24h笔数>=6",             "MEDIUM"),
    ("高均值-多对手",      "平均单笔>35000 AND 对手数>=5",              "MEDIUM"),
    ("夜间大额",           "单笔最大>40000 AND 夜间笔数>=3",            "MEDIUM"),
    ("超高频交易",         "总额>200000 AND 24h笔数>=12",               "HIGH"),
    ("单笔超大额",         "单笔最大>60000",                            "HIGH"),
    ("巨额总额",           "总额>300000",                               "HIGH"),
    ("多对手-高总额",      "总额>200000 AND 对手数>=6",                 "MEDIUM"),
    ("夜间高频-大额",      "总额>200000 AND 夜间笔数>=5 AND 24h笔数>=8", "HIGH"),
    ("均值异常",           "平均单笔>50000",                            "MEDIUM"),
    ("综合可疑-高频高额",  "总额>200000 AND 24h笔数>=8",                "HIGH"),
]


def _add_burst(txns, acc_id, n, lo, hi, n_cp, cp_prefix, day, base_time, night=False):
    """在同一个 24h 窗口内生成 n 笔交易（窗口跨度 < 24h）。"""
    start = base_time + datetime.timedelta(days=day)
    for _ in range(n):
        hours = random.uniform(0, 5.5) if night else random.uniform(0, 22)
        t = start + datetime.timedelta(hours=hours, minutes=random.randint(0, 59))
        amount = round(random.uniform(lo, hi), 2)
        counterparty = f"{cp_prefix}{random.randint(1, n_cp):04d}"
        txns.append((acc_id, counterparty, amount, t.strftime(FMT)))


def build_scenario_transactions(base_time):
    """生成账户 51-100 的场景化交易，返回 (交易列表, {账户: 场景标签})。

    设计原则（保证"违规/不违规"可验证）：
    - 违规账户：单次 24h 窗口内的高风险聚集，命中多条规则；
    - 正常账户：小额、少对手方、时间分散，不命中任何规则；
    - 边界账户：24h 内 4 笔 / 总额约 9.8 万，差一点触发"疑似拆分交易"，用于演示不误报。
    """
    txns = []
    labels = {}

    for name, accs, n, lo, hi, ncp, prefix, day, night in VIOLATION_SCENARIOS:
        for acc in accs:
            _add_burst(txns, acc, n, lo, hi, ncp, prefix, day, base_time, night)
            labels[acc] = f"违规场景[{name}]"

    for acc in NORMAL_ACCOUNTS:
        _add_burst(txns, acc, 4, 200, 3800, 3, "11", 0, base_time)
        _add_burst(txns, acc, 5, 200, 3800, 3, "12", 2, base_time)
        _add_burst(txns, acc, 5, 200, 3800, 3, "13", 4, base_time)
        labels[acc] = "正常账户"

    for acc in BOUNDARY_ACCOUNTS:
        _add_burst(txns, acc, 4, 23500, 25500, 3, "99", 0, base_time)   # 4 笔 / 约 9.8 万
        _add_burst(txns, acc, 3, 500, 2000, 2, "98", 3, base_time)
        labels[acc] = "边界账户"

    return txns, labels


def init_db():
    # 0. 建库（如已存在则复用），并清空旧表
    server_conn = get_conn()
    with server_conn.cursor() as cur:
        cur.execute(
            f"CREATE DATABASE IF NOT EXISTS {DB_NAME} "
            "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci")
    server_conn.close()
    print(f"已连接 MySQL，数据库 {DB_NAME} 就绪")

    conn = get_conn(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("SET FOREIGN_KEY_CHECKS = 0")
    for t in ["audit_logs", "cases", "transactions", "accounts", "rules", "customers"]:
        cursor.execute(f"DROP TABLE IF EXISTS {t}")
    cursor.execute("SET FOREIGN_KEY_CHECKS = 1")
    print("已清空旧表")

    # 1. 创建表结构
    cursor.execute('''
    CREATE TABLE customers (
        id INT PRIMARY KEY,
        name VARCHAR(64),
        id_card VARCHAR(32),
        phone VARCHAR(16)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''')

    cursor.execute('''
    CREATE TABLE accounts (
        id INT PRIMARY KEY,
        customer_id INT,
        account_no VARCHAR(32),
        FOREIGN KEY (customer_id) REFERENCES customers (id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''')

    cursor.execute('''
    CREATE TABLE transactions (
        id INT PRIMARY KEY AUTO_INCREMENT,
        account_id INT,
        counterparty_account VARCHAR(32),
        amount DOUBLE,
        tx_time DATETIME,
        FOREIGN KEY (account_id) REFERENCES accounts (id)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''')

    cursor.execute('''
    CREATE TABLE rules (
        id INT PRIMARY KEY AUTO_INCREMENT,
        rule_name VARCHAR(128),
        condition_str VARCHAR(255),
        risk_level VARCHAR(16),
        version VARCHAR(16),
        updated_by VARCHAR(64),
        updated_at DATETIME,
        is_active TINYINT
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''')

    cursor.execute('''
    CREATE TABLE cases (
        id INT PRIMARY KEY AUTO_INCREMENT,
        account_id INT,
        rule_id INT,
        risk_level VARCHAR(16),
        status VARCHAR(16),
        assignee VARCHAR(64),
        created_at DATETIME
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''')

    cursor.execute('''
    CREATE TABLE audit_logs (
        id INT PRIMARY KEY AUTO_INCREMENT,
        case_id INT,
        event_type VARCHAR(32),
        payload TEXT,
        event_time DATETIME
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4''')

    # 2. 生成 100 个客户 和 100 个账户（客户 51-100 对应账户 51-100）
    print("正在生成客户和账户数据...")
    customers = []
    accounts = []
    for i in range(1, 101):
        customers.append((i, f"客户{i:03d}", f"11010519900101{i:04d}", f"1380000{i:04d}"))
        accounts.append((i, i, f"6222020000{i:04d}"))

    cursor.executemany("INSERT INTO customers VALUES (%s, %s, %s, %s)", customers)
    cursor.executemany("INSERT INTO accounts VALUES (%s, %s, %s)", accounts)

    # 3. 生成 500 条交易
    print("正在生成交易数据...")
    transactions = []
    base_time = datetime.datetime(2023, 10, 1, 0, 0, 0)

    # 刻意构造前 10 个账户触发【疑似拆分交易】规则 (24h内 >=5笔，总额>10万，对手方>=3)
    trigger_tx_details = {}  # 记录触发规则的交易明细，稍后写入审计日志

    for acc_id in range(1, 11):
        trigger_tx_details[acc_id] = []
        for j in range(6):  # 6 笔交易
            # 随机偏移 24 小时
            tx_time = base_time + datetime.timedelta(hours=random.randint(0, 23), minutes=random.randint(0, 59))
            # 金额 1.5万 - 3万 之间
            amount = round(random.uniform(15000, 30000), 2)
            counterparty = f"999999{random.randint(1, 3):04d}"  # 只有 3 个对手方
            transactions.append((acc_id, counterparty, amount, tx_time.strftime(FMT)))
            trigger_tx_details[acc_id].append(counterparty)  # 临时记录

    # 剩余 40 个账户生成随机交易，凑够 500 条
    for _ in range(500 - 60):  # 已经生成了 60 条
        acc_id = random.randint(11, 50)
        # 金额 100 - 5000 的随机数
        amount = round(random.uniform(100, 5000), 2)
        counterparty = f"888888{random.randint(1, 20):04d}"
        # 随机时间
        tx_time = base_time + datetime.timedelta(days=random.randint(0, 5), hours=random.randint(0, 23))
        transactions.append((acc_id, counterparty, amount, tx_time.strftime(FMT)))

    # 3.5 追加场景化交易（账户 51-100）：违规 / 正常 / 边界
    print("正在生成场景化交易数据（违规 / 正常 / 边界）...")
    scenario_txns, scenario_labels = build_scenario_transactions(base_time)
    transactions.extend(scenario_txns)

    cursor.executemany(
        "INSERT INTO transactions (account_id, counterparty_account, amount, tx_time) VALUES (%s, %s, %s, %s)",
        transactions)

    # 4. 生成 20 条规则（全部为可解析条件，特征词见 risk_engine.FEATURE_ALIASES）
    print("正在生成规则配置...")
    now = datetime.datetime.now().strftime(FMT)
    rules = [(name, cond, level, "v1.0", "admin", now, 1)
             for name, cond, level in RULES]

    cursor.executemany(
        "INSERT INTO rules (rule_name, condition_str, risk_level, version, updated_by, updated_at, is_active) VALUES (%s, %s, %s, %s, %s, %s, %s)",
        rules)

    # 5. 生成 10 个案件及审计日志 (满足 10 个可完整回放案件要求)
    print("正在生成案件和审计日志...")
    for acc_id in range(1, 11):
        # 查一下这个账户这 24h 的交易，作为 payload 存入日志
        cursor.execute("SELECT id, amount, counterparty_account, tx_time FROM transactions WHERE account_id = %s",
                       (acc_id,))
        tx_rows = cursor.fetchall()

        tx_ids = [row[0] for row in tx_rows]
        total_amount = sum([row[1] for row in tx_rows])

        # 插入案件表
        cursor.execute(
            "INSERT INTO cases (account_id, rule_id, risk_level, status, assignee, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
            (acc_id, 1, "HIGH", "待处置", "未分配", now))
        case_id = cursor.lastrowid

        # 写入日志：1. 数据进入
        payload_ingest = json.dumps({"msg": "接收到最新交易流水", "tx_count": len(tx_ids)},
                                    ensure_ascii=False, default=str)
        cursor.execute("INSERT INTO audit_logs (case_id, event_type, payload, event_time) VALUES (%s, %s, %s, %s)",
                       (case_id, "数据进入", payload_ingest, now))

        # 写入日志：2. 规则命中 (核心！把具体触发的交易塞进 payload)
        payload_rule = json.dumps({
            "rule_name": "疑似拆分交易",
            "rule_version": "v1.0",
            "trigger_tx_ids": tx_ids,
            "total_amount": total_amount,
            "counterparties_count": 3,
            "detail": tx_rows
        }, ensure_ascii=False, default=str)
        cursor.execute("INSERT INTO audit_logs (case_id, event_type, payload, event_time) VALUES (%s, %s, %s, %s)",
                       (case_id, "规则命中", payload_rule, now))

    conn.commit()
    conn.close()

    # 6. 调用 Semantica 引擎全量扫描：为新增场景生成预警案件与因果事件流
    #    原有 10 个案件（账户 1-10 命中规则 1）版本一致，会被识别为"已有案件"而跳过，不受影响。
    print("正在调用 Semantica 引擎扫描交易并生成案件...")
    scan_ok = False
    try:
        from risk_engine import RiskEngine
        summary = RiskEngine().run_full_scan(operator="admin")
        scan_ok = True
        print(f"  引擎扫描：命中 {summary['hits']} 次 | 新建案件 {summary['created']} 个 | "
              f"已有案件 {summary['existing']} 个 | 跳过 {summary['skipped']} 个")
    except Exception as exc:
        print(f"  [警告] 引擎扫描失败（不影响数据构建）：{exc}")

    # 7. 验证数据数量
    conn = get_conn(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM transactions")
    tx_count = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM rules")
    rule_count = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM cases")
    case_count = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM customers")
    cus_count = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM accounts")
    acc_count = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM transactions WHERE account_id BETWEEN 51 AND 62")
    violation_tx = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM transactions WHERE account_id BETWEEN 96 AND 100")
    boundary_tx = cursor.fetchone()[0]
    conn.close()

    print(f"\n✅ MySQL 数据库构建完成！库名: {DB_NAME} @ {DB_SERVER['host']}:{DB_SERVER['port']}")
    print(f"📊 客户数: {cus_count} | 账户数: {acc_count}")
    print(f"💸 交易流水数: {tx_count}")
    print(f"   ├─ 违规账户(51-62)交易: {violation_tx} 笔")
    print(f"   ├─ 正常账户(63-95)交易: 462 笔（小额、对手方少、时间分散）")
    print(f"   └─ 边界账户(96-100)交易: {boundary_tx} 笔（24h 内 4 笔、约 9.8 万，接近但不触发）")
    print(f"📜 规则数: {rule_count}（全部可被引擎解析匹配）")
    print(f"🚨 预警案件数: {case_count}（其中构造案件 10 个{'' if scan_ok else '；引擎扫描未执行'}{'，场景命中 ' if scan_ok else ''}{case_count - 10 if scan_ok else ''})")
    for acc in sorted(scenario_labels):
        print(f"   · 账户 {acc:>3}: {scenario_labels[acc]}")


if __name__ == "__main__":
    init_db()
