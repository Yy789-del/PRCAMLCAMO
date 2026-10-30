# -*- coding: utf-8 -*-
"""
risk_engine.py — 风控规则引擎与因果追踪核心（深度集成 Semantica）

架构说明：
- Semantica 的 Reasoner（Rete 风格模式匹配）使用 ?var 模板做事实联合（join），
  不支持数值比较运算。因此采用业界常见的"特征计算 + 规则联合"两段式设计：
  1. Pandas 对每个账户做 24h 滑动窗口特征聚合（笔数/总额/对手数/夜间交易等）；
  2. 将 rules 表中的中文条件表达式（如 "24h笔数>=5 AND 总额>100000"）解析为原子条件，
     每个账户每满足一个原子条件，就向 Reasoner 断言一条事实 r{规则id}_c{序号}(acc_{账户id})；
  3. Reasoner 对多条件做联合匹配（forward_chain），命中结论 hit_r{规则id}(acc_{账户id})，
     其 premises 即命中的具体条件事实，天然具备可解释性；
  4. 命中后写入 cases 与 audit_logs（数据进入 -> 规则命中 -> 评分变化），
     并通过 semantica.provenance.ProvenanceManager 记录
     "交易窗口 -> 规则版本 -> 案件" 的 W3C PROV-O 因果血缘。

存储说明：
- 业务库使用 MySQL（库名 risk_audit），通过 pymysql / SQLAlchemy 访问；
- semantica 血缘库（provenance.db）为其内置 SQLite 存储，独立于业务库。

合规约束（硬性）：
- 本模块只做"预警/研判辅助"，不包含任何自动冻结、自动拒贷等处置逻辑；
- 所有对外展示与报告文本必须经过 mask_* 脱敏函数处理。
"""
import json
import operator
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import pymysql

# ---------------------------------------------------------------------------
# MySQL 连接配置（按你的实际情况修改 user / password）
# 注意：init_db.py 中的 DB_SERVER 需与此处保持一致
# ---------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROV_DB_PATH = os.path.join(BASE_DIR, "provenance.db")  # semantica 血缘库（SQLite，独立于业务库）

DB_CONFIG = {
    "host": "localhost",
    "port": 3306,
    "user": "root",
    "password": "123456",   # TODO: 改成你的 MySQL 密码
    "database": "risk_audit",
    "charset": "utf8mb4",
}

# 若密码含 @ # : / 等特殊字符，需先做 URL 编码（urllib.parse.quote_plus）
DATABASE_URL = (
    f"mysql+pymysql://{DB_CONFIG['user']}:{DB_CONFIG['password']}"
    f"@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}?charset=utf8mb4"
)
# 仅用于界面展示（不含密码）
DB_PATH = f"mysql://{DB_CONFIG['user']}@{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}"

FMT = "%Y-%m-%d %H:%M:%S"
WINDOW_NS = 24 * 3600 * 10**9  # 24 小时窗口（纳秒）

_ENGINE = None  # SQLAlchemy 引擎（懒加载，供 pandas 读取使用）


def get_conn():
    """新建一个 pymysql 连接（DictCursor，查询结果按列名访问）。

    注意：pymysql 的 with 上下文管理器只负责关闭连接，**不会自动 commit**，
    写操作必须显式调用 conn.commit()。
    """
    cfg = {k: v for k, v in DB_CONFIG.items()}
    return pymysql.connect(**cfg, cursorclass=pymysql.cursors.DictCursor)


def get_engine():
    """SQLAlchemy 引擎（pandas read_sql 用）。"""
    global _ENGINE
    if _ENGINE is None:
        from sqlalchemy import create_engine
        _ENGINE = create_engine(DATABASE_URL, pool_pre_ping=True)
    return _ENGINE


# ---------------------------------------------------------------------------
# 规则条件解析
# ---------------------------------------------------------------------------
# 中文特征词 -> 特征列名（compute_account_features 的输出列）
FEATURE_ALIASES = {
    "24h笔数": "txn_count_24h",
    "24h交易笔数": "txn_count_24h",
    "总额": "total_amount_24h",
    "24h总额": "total_amount_24h",
    "对手数": "counterparty_count_24h",
    "对手方数": "counterparty_count_24h",
    "单笔最大": "max_amount_24h",
    "单笔最大金额": "max_amount_24h",
    "平均单笔": "avg_amount_24h",
    "夜间笔数": "night_count_24h",
    "夜间占比": "night_ratio_24h",  # 0-100 的百分数，夜间=00:00-06:00
}

FEATURE_VOCAB_DOC = (
    "支持的特征词：`24h笔数`、`总额`、`对手数`、`单笔最大`、`平均单笔`、"
    "`夜间笔数`、`夜间占比`（0-100，夜间指 00:00-06:00）；"
    "比较符：`>=` `<=` `>` `<` `=`；多个条件用 `AND` 连接。"
    "示例：`24h笔数>=5 AND 总额>100000 AND 对手数>=3`"
)

_COND_RE = re.compile(r"([\w一-龥]+)\s*(>=|<=|==|>|<|=)\s*(\d+(?:\.\d+)?)")
_OPS = {
    ">=": operator.ge, "<=": operator.le, ">": operator.gt,
    "<": operator.lt, "=": operator.eq, "==": operator.eq,
}

# 解析后的原子条件：(特征列名, 比较符, 阈值, 原始文本)
Atom = Tuple[str, str, float, str]


class ConditionParseError(ValueError):
    """规则条件表达式解析失败。"""


def parse_condition(condition_str: str) -> List[Atom]:
    """把 "24h笔数>=5 AND 总额>100000" 解析为原子条件列表。"""
    atoms: List[Atom] = []
    for raw in re.split(r"\s+AND\s+", condition_str.strip(), flags=re.IGNORECASE):
        raw = raw.strip()
        m = _COND_RE.search(raw)
        if not m:
            raise ConditionParseError(f"无法解析条件: {raw!r}")
        word, op, thr = m.group(1), m.group(2), float(m.group(3))
        if word not in FEATURE_ALIASES:
            raise ConditionParseError(
                f"未知特征词: {word!r}（支持: {', '.join(FEATURE_ALIASES)}）")
        atoms.append((FEATURE_ALIASES[word], op, thr, raw))
    if not atoms:
        raise ConditionParseError("条件表达式为空")
    return atoms


# ---------------------------------------------------------------------------
# 24h 滑动窗口特征计算
# ---------------------------------------------------------------------------
def compute_account_features(df_txn: pd.DataFrame) -> pd.DataFrame:
    """对每个账户，在所有 [t-24h, t] 窗口中取 (笔数, 总额) 最大的窗口，
    输出该窗口的聚合特征与触发交易 ID 列表。

    返回 DataFrame（index=account_id），列：
    txn_count_24h / total_amount_24h / counterparty_count_24h /
    max_amount_24h / avg_amount_24h / night_count_24h / night_ratio_24h /
    window_txn_ids / window_start / window_end
    """
    df = df_txn.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values("timestamp")

    rows = []
    for acc_id, g in df.groupby("account_id"):
        ts = g["timestamp"].astype("int64").to_numpy()
        best = None  # (count, sum, left_idx, right_idx)
        for i in range(len(g)):
            left = int(ts.searchsorted(ts[i] - WINDOW_NS, side="left"))
            w = g.iloc[left:i + 1]
            key = (len(w), w["amount"].sum(), left, i)
            if best is None or key[:2] > best[:2]:
                best = key
        count, total, left, right = best
        w = g.iloc[left:right + 1]
        night_count = int((w["timestamp"].dt.hour < 6).sum())
        rows.append({
            "account_id": acc_id,
            "txn_count_24h": int(count),
            "total_amount_24h": float(total),
            "counterparty_count_24h": int(w["counterparty_account"].nunique()),
            "max_amount_24h": float(w["amount"].max()),
            "avg_amount_24h": float(w["amount"].mean()),
            "night_count_24h": night_count,
            "night_ratio_24h": round(night_count / count * 100, 2),
            "window_txn_ids": [int(x) for x in w["id"]],
            "window_start": w["timestamp"].iloc[0].strftime(FMT),
            "window_end": w["timestamp"].iloc[-1].strftime(FMT),
        })
    return pd.DataFrame(rows).set_index("account_id") if rows else pd.DataFrame()


def risk_score(feats: pd.Series) -> int:
    """透明可解释的评分公式（0-100）：笔数、金额、对手数加权。"""
    score = (feats["txn_count_24h"] * 6
             + min(feats["total_amount_24h"] / 2000, 40)
             + feats["counterparty_count_24h"] * 4)
    return int(min(100, round(score)))


def risk_level(score: int) -> str:
    return "HIGH" if score >= 70 else ("MEDIUM" if score >= 40 else "LOW")


# ---------------------------------------------------------------------------
# 脱敏工具（展示与报告层强制使用）
# ---------------------------------------------------------------------------
_ID_CARD_RE = re.compile(r"(\d{6})\d{8}(\d{3}[\dXx])")
_PHONE_RE = re.compile(r"(1[3-9]\d)\d{4}(\d{4})")
_ACCT_RE = re.compile(r"(\d{4})\d{2,11}(\d{4})")  # 覆盖 10-19 位账号


def mask_text(text: Any) -> Any:
    """对身份证(18位)/手机号(11位)/银行账号(10-19位)的中间部分打 ****。"""
    if not isinstance(text, str):
        return text
    text = _ID_CARD_RE.sub(r"\1********\2", text)
    text = _PHONE_RE.sub(r"\1****\2", text)
    text = _ACCT_RE.sub(r"\1********\2", text)
    return text


def mask_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """对 DataFrame 所有文本列应用脱敏。"""
    out = df.copy()
    for col in out.columns:
        if out[col].dtype == object:
            out[col] = out[col].map(mask_text)
    return out


# ---------------------------------------------------------------------------
# 核心引擎
# ---------------------------------------------------------------------------
class RiskEngine:
    """风控规则引擎：加载 rules 表 -> Pandas 聚合 -> Semantica 匹配 -> 落审计链。"""

    def __init__(self, enable_provenance: bool = True):
        try:
            from semantica.reasoning import Reasoner  # noqa: N813
        except ImportError as exc:
            raise ImportError(
                "未安装 semantica，请先执行: pip install -r requirements.txt") from exc
        self._reasoner_cls = Reasoner
        self.enable_provenance = enable_provenance
        self._prov_manager = None
        if enable_provenance:
            try:
                from semantica.provenance import ProvenanceManager, SQLiteStorage
                self._prov_manager = ProvenanceManager(
                    storage=SQLiteStorage(PROV_DB_PATH))
            except Exception:
                self._prov_manager = None  # 血缘追踪失败不影响主流程

    # -- 数据访问 ----------------------------------------------------------
    def load_active_rules(self) -> List[Dict[str, Any]]:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM rules WHERE is_active = 1 ORDER BY id")
                return cur.fetchall()

    # -- 主流程 ------------------------------------------------------------
    def run_full_scan(self, operator: str = "system") -> Dict[str, int]:
        """全量扫描交易流水，返回 {'rules', 'hits', 'created', 'existing', 'skipped'}。"""
        # 统一列名：库中为 tx_time，特征计算内部使用 timestamp
        df_txn = pd.read_sql_query(
            "SELECT id, account_id, counterparty_account, amount, "
            "tx_time AS timestamp FROM transactions", get_engine())

        features = compute_account_features(df_txn)
        rules = self.load_active_rules()

        # 1) 解析规则并加载到 Semantica Reasoner
        reasoner = self._reasoner_cls()
        atoms_map: Dict[int, List[Atom]] = {}
        rule_map: Dict[int, Dict[str, Any]] = {}
        for r in rules:
            try:
                atoms = parse_condition(r["condition_str"])
            except ConditionParseError as exc:
                print(f"[警告] 规则 #{r['id']} {r['rule_name']} 解析失败，已跳过: {exc}")
                continue
            atoms_map[r["id"]] = atoms
            rule_map[r["id"]] = r
            conds = " AND ".join(f"r{r['id']}_c{j}(?acc)" for j in range(len(atoms)))
            reasoner.add_rule(f"IF {conds} THEN hit_r{r['id']}(?acc)")

        # 2) 数值比较在 Pandas 侧完成，满足的原子条件断言为 Semantica 事实
        for acc_id, feats in features.iterrows():
            for rid, atoms in atoms_map.items():
                for j, (fkey, op, thr, _raw) in enumerate(atoms):
                    if _OPS[op](feats[fkey], thr):
                        reasoner.add_fact(f"r{rid}_c{j}(acc_{acc_id})")

        # 3) Semantica 前向链推理，输出命中结论（含 premises 溯源）
        results = reasoner.forward_chain()

        # 4) 处理命中结果：建案件 + 写因果事件流 + 血缘追踪
        summary = {"rules": len(atoms_map), "hits": 0,
                   "created": 0, "existing": 0, "skipped": 0}
        hit_re = re.compile(r"^hit_r(\d+)\(acc_(\d+)\)$")
        for res in results:
            m = hit_re.match(res.conclusion)
            if not m:
                continue
            rid, acc_id = int(m.group(1)), int(m.group(2))
            summary["hits"] += 1
            self._handle_hit(rid, acc_id, res, rule_map[rid],
                             atoms_map[rid], features.loc[acc_id],
                             operator, summary)
        return summary

    # -- 命中处理 ----------------------------------------------------------
    def _handle_hit(self, rule_id: int, account_id: int, inference,
                    rule: Dict[str, Any], atoms: List[Atom],
                    feats: pd.Series, operator: str,
                    summary: Dict[str, int]) -> None:
        now = datetime.now().strftime(FMT)
        txn_ids = [int(x) for x in feats["window_txn_ids"]]
        score = risk_score(feats)
        # premises（如 r1_c0(acc_3)）映射回可读条件文本
        matched_conditions = []
        for p in inference.premises:
            pm = re.match(rf"^r{rule_id}_c(\d+)\(", p)
            if pm:
                matched_conditions.append(atoms[int(pm.group(1))][3])

        features_snapshot = {
            k: v for k, v in feats.items() if k != "window_txn_ids"
        }
        hit_payload = {
            "rule_id": rule_id,
            "rule_name": rule["rule_name"],
            "rule_version": rule["version"],          # 命中当时的规则版本（留痕）
            "condition_str": rule["condition_str"],
            "matched_conditions": matched_conditions,
            "semantica_conclusion": inference.conclusion,
            "semantica_premises": list(inference.premises),
            "features": features_snapshot,
            "trigger_tx_ids": txn_ids,
            "window": {"start": feats["window_start"], "end": feats["window_end"]},
        }

        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM cases WHERE account_id = %s AND rule_id = %s",
                    (account_id, rule_id))
                existing = cur.fetchone()

                if existing is None:
                    # 新建案件 + 完整事件链：数据进入 -> 规则命中 -> 评分变化
                    assignees = ["张合规", "李风控", "王审计"]
                    assignee = assignees[(account_id + rule_id) % len(assignees)]
                    cur.execute(
                        "INSERT INTO cases (account_id, rule_id, risk_level, status, assignee, created_at) "
                        "VALUES (%s, %s, %s, '待处置', %s, %s)",
                        (account_id, rule_id, risk_level(score), assignee, now))
                    case_id = cur.lastrowid
                    cur.execute(
                        f"SELECT * FROM transactions WHERE id IN "
                        f"({','.join(['%s'] * len(txn_ids))}) ORDER BY tx_time",
                        txn_ids)
                    txns = cur.fetchall()
                    self._log(cur, case_id, "数据进入", {
                        "trigger_tx_ids": txn_ids,
                        "transactions": [dict(t) for t in txns],
                        "window": hit_payload["window"],
                    }, now)
                    self._log(cur, case_id, "规则命中", hit_payload, now)
                    self._log(cur, case_id, "评分变化", {
                        "old_score": None, "new_score": score,
                        "risk_level": risk_level(score),
                        "rule_version": rule["version"], "reason": "首次命中评分",
                        "operator": operator,
                    }, now)
                    summary["created"] += 1
                    self._track_provenance(case_id, rule_id, rule["version"], txn_ids)
                else:
                    case_id = existing["id"]
                    # 已有案件：仅当规则版本发生变化时追加留痕事件（规则修改留痕）
                    cur.execute(
                        "SELECT payload FROM audit_logs WHERE case_id = %s AND event_type = '规则命中' "
                        "ORDER BY id DESC LIMIT 1", (case_id,))
                    last_hit = cur.fetchone()
                    last_version = (json.loads(last_hit["payload"]).get("rule_version")
                                    if last_hit else None)
                    if last_version != rule["version"]:
                        self._log(cur, case_id, "规则命中", hit_payload, now)
                        self._log(cur, case_id, "评分变化", {
                            "old_score": None, "new_score": score,
                            "risk_level": risk_level(score),
                            "rule_version": rule["version"],
                            "reason": f"规则版本变更 {last_version} -> {rule['version']}，重新命中",
                            "operator": operator,
                        }, now)
                        cur.execute("UPDATE cases SET risk_level = %s WHERE id = %s",
                                    (risk_level(score), case_id))
                        self._track_provenance(case_id, rule_id, rule["version"], txn_ids)
                    else:
                        summary["skipped"] += 1
                    summary["existing"] += 1
            # 显式提交：pymysql 的 with 上下文管理器只关闭连接，不会自动 commit
            conn.commit()

    @staticmethod
    def _log(cur, case_id: int, event_type: str,
             payload: Dict[str, Any], ts: str) -> None:
        # default=str：MySQL DATETIME 读出为 datetime 对象，需转字符串后入 JSON
        cur.execute(
            "INSERT INTO audit_logs (case_id, event_type, payload, event_time) "
            "VALUES (%s, %s, %s, %s)",
            (case_id, event_type,
             json.dumps(payload, ensure_ascii=False, default=str), ts))

    # -- Semantica 血缘（因果）追踪 -----------------------------------------
    def _track_provenance(self, case_id: int, rule_id: int,
                          rule_version: str, txn_ids: List[int]) -> None:
        """记录 交易窗口 -> 规则版本 -> 案件 的 W3C PROV-O 血缘链（失败不影响主流程）。"""
        if not self._prov_manager:
            return
        try:
            pm = self._prov_manager
            window_entity = f"txn_window_case{case_id}"
            rule_entity = f"rule_{rule_id}_{rule_version}"
            pm.track_entity(entity_id=window_entity,
                            source="risk_audit:transactions",
                            agent_id="RiskEngine", agent_type="software_agent",
                            metadata={"transaction_ids": txn_ids})
            pm.track_entity(entity_id=rule_entity,
                            source=window_entity,
                            agent_id="RiskEngine", agent_type="software_agent",
                            metadata={"rule_id": rule_id, "version": rule_version})
            pm.track_entity(entity_id=f"case_{case_id}",
                            source=rule_entity,
                            agent_id="RiskEngine", agent_type="software_agent",
                            metadata={"case_id": case_id})
        except Exception:
            pass

    def get_case_lineage(self, case_id: int) -> Optional[Dict[str, Any]]:
        """查询案件的 Semantica 血缘链；不可用时返回 None。"""
        if not self._prov_manager:
            return None
        try:
            return self._prov_manager.get_lineage(f"case_{case_id}")
        except Exception:
            return None


# ---------------------------------------------------------------------------
# 因果报告生成（Markdown，文本已脱敏）
# ---------------------------------------------------------------------------
MANDATORY_NOTICE = "系统辅助研判，需人工确认"


def build_case_report(case_id: int) -> str:
    """生成单案件因果审计报告（Markdown）。所有证件号/手机号/账号均已脱敏。"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM cases WHERE id = %s", (case_id,))
            case = cur.fetchone()
            if case is None:
                raise ValueError(f"案件不存在: {case_id}")
            cur.execute("SELECT * FROM rules WHERE id = %s", (case["rule_id"],))
            rule = cur.fetchone()
            cur.execute("SELECT * FROM accounts WHERE id = %s", (case["account_id"],))
            account = cur.fetchone()
            cur.execute("SELECT * FROM customers WHERE id = %s", (account["customer_id"],))
            customer = cur.fetchone()
            cur.execute(
                "SELECT * FROM audit_logs WHERE case_id = %s ORDER BY event_time, id",
                (case_id,))
            logs = cur.fetchall()

            hit = next((json.loads(l["payload"]) for l in logs
                        if l["event_type"] == "规则命中"), {})
            txn_ids = hit.get("trigger_tx_ids") or hit.get("transaction_ids", [])
            txns = []
            if txn_ids:
                cur.execute(
                    f"SELECT * FROM transactions WHERE id IN "
                    f"({','.join(['%s'] * len(txn_ids))}) ORDER BY tx_time", txn_ids)
                txns = cur.fetchall()
            human = [json.loads(l["payload"]) for l in logs if l["event_type"] == "人工处置"]
            score_events = [json.loads(l["payload"]) for l in logs
                            if l["event_type"] == "评分变化"]

    L: List[str] = []
    L.append(f"# 案件因果审计报告 —— 案件 #{case_id}\n")
    L.append(f"> **{MANDATORY_NOTICE}**。本报告由系统自动整理，仅供人工研判参考，"
             "不构成任何自动处置依据。\n")

    L.append("## 一、案件基本信息\n")
    L.append("| 项目 | 内容 |")
    L.append("| --- | --- |")
    L.append(f"| 案件编号 | #{case_id} |")
    L.append(f"| 风险等级 | {case['risk_level']} |")
    L.append(f"| 当前状态 | {case['status']} |")
    L.append(f"| 负责人 | {case['assignee'] or '未分配'} |")
    L.append(f"| 客户姓名 | {customer['name']} |")
    L.append(f"| 身份证号 | {mask_text(customer['id_card'])} |")
    L.append(f"| 手机号 | {mask_text(customer['phone'])} |")
    L.append(f"| 涉案账号 | {mask_text(account['account_no'])} |\n")

    L.append("## 二、命中的规则版本\n")
    L.append("| 项目 | 内容 |")
    L.append("| --- | --- |")
    L.append(f"| 规则名称 | {hit.get('rule_name', rule['rule_name'])} |")
    L.append(f"| **命中时规则版本** | **{hit.get('rule_version', rule['version'])}** |")
    L.append(f"| 命中条件 | `{hit.get('condition_str', rule['condition_str'])}` |")
    matched = hit.get("matched_conditions") or []
    if matched:
        L.append(f"| 实际满足的原子条件 | {'；'.join(matched)} |")
    feats = hit.get("features", {})
    if feats:
        L.append(f"| 命中时 24h 特征 | 笔数 {feats.get('txn_count_24h')}、"
                 f"总额 {feats.get('total_amount_24h')}、对手数 "
                 f"{feats.get('counterparty_count_24h')}、夜间笔数 "
                 f"{feats.get('night_count_24h')} |")
    if score_events:
        L.append(f"| 风险评分 | {score_events[-1].get('new_score')} 分 |")
    L.append("")

    L.append("## 三、触发的原始交易明细（已脱敏）\n")
    L.append("| 交易ID | 时间 | 金额(元) | 对手方账号 |")
    L.append("| --- | --- | ---: | --- |")
    total = 0.0
    for t in txns:
        total += t["amount"]
        L.append(f"| {t['id']} | {t['tx_time']} | {t['amount']:,.2f} | "
                 f"{mask_text(t['counterparty_account'])} |")
    L.append(f"\n合计 {len(txns)} 笔，总金额 {total:,.2f} 元。\n")

    L.append("## 四、因果链路描述\n")
    L.append("```")
    L.append(f"交易流水 {txn_ids}")
    L.append(f"  └─(24h 滑动窗口聚合 {hit.get('window', {}).get('start', '?')} ~ "
             f"{hit.get('window', {}).get('end', '?')})")
    L.append(f"      └─ 特征满足条件: {' AND '.join(matched) if matched else hit.get('condition_str', '?')}")
    L.append(f"          └─ Semantica Reasoner 命中: {hit.get('semantica_conclusion', '?')}"
             f"（规则 {hit.get('rule_name', '?')} {hit.get('rule_version', '?')}）")
    L.append(f"              └─ 风险评分 {score_events[-1].get('new_score') if score_events else '?'} 分"
             f" -> 生成案件 #{case_id}（状态: {case['status']}）")
    L.append("```\n")
    L.append("### 全生命周期事件流\n")
    L.append("| 时间 | 事件 | 摘要 |")
    L.append("| --- | --- | --- |")
    for l in logs:
        p = json.loads(l["payload"])
        if l["event_type"] == "数据进入":
            n = len(p.get("trigger_tx_ids", p.get("transaction_ids", [])))
            brief = f"进入 {n or p.get('tx_count', '?')} 笔交易快照"
        elif l["event_type"] == "规则命中":
            brief = f"命中 {p.get('rule_name')} {p.get('rule_version')}"
        elif l["event_type"] == "评分变化":
            brief = f"评分 {p.get('old_score')} -> {p.get('new_score')}（{p.get('reason', '')}）"
        else:
            brief = f"{p.get('operator', '')}: {str(p.get('conclusion', ''))[:40]}"
        L.append(f"| {l['event_time']} | {l['event_type']} | {brief} |")
    L.append("")

    L.append("## 五、人工处置结论\n")
    if human:
        for h in human:
            L.append(f"- **{h.get('operator', '未知')}**（{h.get('timestamp', '')}）："
                     f"{h.get('conclusion', '')}（状态：{h.get('old_status', '?')} "
                     f"-> {h.get('new_status', '?')}）")
    else:
        L.append("- 暂无人工处置结论。")
    L.append(f"\n---\n*{MANDATORY_NOTICE}* ｜ 数据来源: MySQL {DB_PATH} ｜ "
             "身份证、手机号、银行账号已按监管要求脱敏。")
    return "\n".join(L)
