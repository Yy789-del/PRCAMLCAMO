# -*- coding: utf-8 -*-
"""
app.py — 可穿透风控与反洗钱因果审计中台（Streamlit 前端）

四个 Tab：
1. 数据概览          —— 总交易数 / 客户数 / 账户数 / 案件数
2. 规则配置          —— 展示 20 条规则；修改阈值后 version+0.1 并留痕（updated_by/updated_at）
3. 案件工作台与回放  —— 案件列表 + 时间轴回放全生命周期 + 人工处置录入
4. 一键因果报告      —— 生成并下载 Markdown 因果审计报告

合规约束（硬性）：
- 所有证件号 / 手机号 / 账号展示前均经 mask_* 脱敏；
- 所有结论展示区强制包含「系统辅助研判，需人工确认」；
- 本系统不含任何自动冻结、自动拒贷逻辑，处置结论一律由人工录入。
启动: streamlit run app.py
"""
import json
import os
import re
import sys
from datetime import datetime

import pandas as pd
import streamlit as st

from risk_engine import (DB_PATH, FEATURE_VOCAB_DOC, MANDATORY_NOTICE,
                         RiskEngine, build_case_report, get_conn, get_engine,
                         mask_dataframe, mask_text)


def _ensure_streamlit_runtime() -> None:
    """如果被 `python app.py` 直接执行（无 Streamlit 运行时），自动改用 streamlit run 重启。

    这样在 IDE 里点"运行"按钮也能正常启动，无需手动敲命令。
    """
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
        if get_script_run_ctx() is not None:
            return  # 已经运行在 Streamlit 服务器中
    except Exception:
        pass
    print("检测到未经 streamlit 启动，正在自动以 streamlit run 方式重启...")
    print("启动后请访问: http://localhost:8501")
    subprocess_result = __import__("subprocess").run(
        [sys.executable, "-m", "streamlit", "run", os.path.abspath(__file__)])
    sys.exit(subprocess_result.returncode)


_ensure_streamlit_runtime()

st.set_page_config(page_title="可穿透风控与反洗钱因果审计中台",
                   page_icon=None, layout="wide")

FMT = "%Y-%m-%d %H:%M:%S"


# ---------------------------------------------------------------------------
# 数据访问（数据量小，直接实时查询，写入后 st.rerun 刷新）
# ---------------------------------------------------------------------------
def query_df(sql: str, params: tuple = ()) -> pd.DataFrame:
    return pd.read_sql_query(sql, get_engine(), params=params)


def execute(sql: str, params: tuple = ()) -> None:
    # pymysql 的 with 上下文管理器只关闭连接、不会自动提交，这里必须显式 commit
    conn = get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


def bump_version(version: str) -> str:
    """v1.2 -> v1.3；无法解析时回退为 v1.1。"""
    m = re.match(r"v?(\d+)(?:\.(\d+))?", str(version))
    if not m:
        return "v1.1"
    return f"v{m.group(1)}.{int(m.group(2) or 0) + 1}"


def get_case_hit_version(case_id: int) -> str:
    """取案件最近一次【规则命中】事件中的规则版本（命中当时的版本，而非当前版本）。"""
    df = query_df(
        "SELECT payload FROM audit_logs WHERE case_id = %s AND event_type = '规则命中' "
        "ORDER BY id DESC LIMIT 1", (case_id,))
    if df.empty:
        return "-"
    return json.loads(df.iloc[0]["payload"]).get("rule_version", "-")


# ---------------------------------------------------------------------------
# 页面头部与侧边栏
# ---------------------------------------------------------------------------
st.title("可穿透风控与反洗钱因果审计中台")
st.caption("规则引擎与因果追踪由 Semantica 驱动 ｜ 数据存储: MySQL ｜ 本系统仅做预警研判辅助")

with st.sidebar:
    st.header("合规声明")
    st.warning(MANDATORY_NOTICE)
    st.caption("系统不包含任何自动冻结、自动拒贷逻辑；"
               "所有处置结论必须由人工录入并留痕。")
    st.divider()
    st.caption(f"业务库: {DB_PATH}")
    if st.button("重新扫描全量交易", use_container_width=True):
        try:
            with st.spinner("Semantica 引擎扫描中..."):
                summary = RiskEngine().run_full_scan(operator="manual-rescan")
            st.success(f"扫描完成：命中 {summary['hits']}，新建案件 {summary['created']}，"
                       f"跳过重复 {summary['skipped']}")
        except Exception as exc:  # 引擎异常直接可见，便于课堂演示排错
            st.error(f"扫描失败: {exc}")

tab1, tab2, tab3, tab4 = st.tabs(
    ["数据概览", "规则配置", "案件工作台与回放", "一键因果报告"])

# ---------------------------------------------------------------------------
# Tab 1: 数据概览
# ---------------------------------------------------------------------------
with tab1:
    txn_cnt = query_df("SELECT COUNT(*) c FROM transactions").iloc[0]["c"]
    cust_cnt = query_df("SELECT COUNT(*) c FROM customers").iloc[0]["c"]
    acct_cnt = query_df("SELECT COUNT(*) c FROM accounts").iloc[0]["c"]
    case_cnt = query_df("SELECT COUNT(*) c FROM cases").iloc[0]["c"]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("总交易数", int(txn_cnt))
    c2.metric("客户数", int(cust_cnt))
    c3.metric("账户数", int(acct_cnt))
    c4.metric("拦截/预警案件数", int(case_cnt))

    st.divider()
    left, right = st.columns(2)
    with left:
        st.subheader("案件状态分布")
        status_df = query_df(
            "SELECT status AS 状态, COUNT(*) AS 案件数 FROM cases GROUP BY status")
        if status_df.empty:
            st.info("暂无案件")
        else:
            st.bar_chart(status_df.set_index("状态"))
    with right:
        st.subheader("每日交易量")
        daily = query_df(
            "SELECT substr(tx_time, 1, 10) AS 日期, COUNT(*) AS 笔数 "
            "FROM transactions GROUP BY 日期 ORDER BY 日期")
        st.bar_chart(daily.set_index("日期"))

    st.subheader("规则命中情况")
    hit_df = query_df(
        "SELECT r.rule_name AS 规则, COUNT(*) AS 案件数 "
        "FROM cases c JOIN rules r ON r.id = c.rule_id GROUP BY r.rule_name")
    if hit_df.empty:
        st.info("暂无命中记录")
    else:
        st.dataframe(hit_df, use_container_width=True, hide_index=True)

# ---------------------------------------------------------------------------
# Tab 2: 规则配置（修改留痕：version+0.1、记录操作人与时间）
# ---------------------------------------------------------------------------
with tab2:
    st.subheader("规则列表")
    rules_df = query_df(
        "SELECT id, rule_name, condition_str, version, updated_by, updated_at, is_active "
        "FROM rules ORDER BY id")
    rules_show = rules_df.rename(columns={
        "id": "规则ID", "rule_name": "规则名称", "condition_str": "条件表达式",
        "version": "版本", "updated_by": "修改人", "updated_at": "修改时间",
        "is_active": "是否启用"})
    rules_show["是否启用"] = rules_show["是否启用"].map({1: "启用", 0: "停用"}).fillna("停用")
    st.dataframe(rules_show, use_container_width=True, hide_index=True)

    with st.expander("条件表达式语法说明"):
        st.markdown(FEATURE_VOCAB_DOC)

    st.subheader("修改规则阈值")
    rule_ids = rules_df["id"].tolist()
    selected_id = st.selectbox(
        "选择要修改的规则", rule_ids,
        format_func=lambda i: f"#{i} {rules_df.loc[rules_df['id'] == i, 'rule_name'].iloc[0]}")
    cur = rules_df.loc[rules_df["id"] == selected_id].iloc[0]

    with st.form("rule_edit_form"):
        st.markdown(f"当前规则：**{cur['rule_name']}**（版本 `{cur['version']}`，"
                    f"最近由 {cur['updated_by']} 于 {cur['updated_at']} 修改）")
        new_cond = st.text_input("条件表达式", value=cur["condition_str"])
        new_active = st.checkbox("启用该规则", value=bool(cur["is_active"]))
        operator_name = st.text_input("操作人", value="")
        submitted = st.form_submit_button("保存（自动 version+0.1 并留痕）")
        if submitted:
            if not operator_name.strip():
                st.error("请填写操作人，用于规则修改留痕。")
            elif not new_cond.strip():
                st.error("条件表达式不能为空。")
            else:
                from risk_engine import parse_condition, ConditionParseError
                try:
                    parse_condition(new_cond)  # 保存前校验表达式可解析
                except ConditionParseError as exc:
                    st.error(f"条件表达式不合法: {exc}")
                    st.stop()
                new_version = bump_version(cur["version"])
                execute(
                    "UPDATE rules SET condition_str = %s, version = %s, updated_by = %s, "
                    "updated_at = %s, is_active = %s WHERE id = %s",
                    (new_cond.strip(), new_version, operator_name.strip(),
                     datetime.now().strftime(FMT), int(new_active), int(selected_id)))
                st.success(f"已保存：版本 {cur['version']} -> {new_version}，"
                           f"操作人 {operator_name.strip()}，修改时间已记录。")
                st.rerun()

    st.caption("提示：修改规则后，点击左侧边栏「重新扫描全量交易」可按新阈值重新匹配，"
               "已建案件会追加新版本的命中与评分留痕。")

# ---------------------------------------------------------------------------
# Tab 3: 案件工作台与回放
# ---------------------------------------------------------------------------
with tab3:
    st.warning(MANDATORY_NOTICE)
    cases_df = query_df(
        "SELECT c.id, c.account_id, a.account_no, r.rule_name, c.risk_level, "
        "c.status, c.assignee FROM cases c "
        "JOIN accounts a ON a.id = c.account_id "
        "JOIN rules r ON r.id = c.rule_id ORDER BY c.id")
    if cases_df.empty:
        st.info("暂无案件。请先在侧边栏执行「重新扫描全量交易」。")
        st.stop()

    # 列表展示（账号脱敏 + 命中时规则版本）
    list_df = cases_df.copy()
    list_df["account_no"] = list_df["account_no"].map(mask_text)
    list_df["命中版本"] = list_df["id"].map(get_case_hit_version)
    list_df.columns = ["案件ID", "账户ID", "涉案账号", "命中规则", "风险等级",
                       "状态", "负责人", "命中版本"]
    st.subheader(f"案件列表（共 {len(list_df)} 件）")
    st.dataframe(list_df, use_container_width=True, hide_index=True)

    # 案件选择
    case_id = st.selectbox(
        "选择案件进入详情回放", cases_df["id"].tolist(),
        format_func=lambda i: (
            f"案件#{i} ｜ {cases_df.loc[cases_df['id'] == i, 'rule_name'].iloc[0]} ｜ "
            f"{cases_df.loc[cases_df['id'] == i, 'risk_level'].iloc[0]}风险 ｜ "
            f"{cases_df.loc[cases_df['id'] == i, 'status'].iloc[0]}"))
    case_row = cases_df.loc[cases_df["id"] == case_id].iloc[0]
    hit_version = get_case_hit_version(case_id)

    # 客户/账户信息（脱敏展示）
    cust = query_df(
        "SELECT cu.name, cu.id_card, cu.phone, a.account_no FROM accounts a "
        "JOIN customers cu ON cu.id = a.customer_id WHERE a.id = %s",
        (int(case_row["account_id"]),)).iloc[0]

    st.subheader(f"案件 #{case_id} 详情")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("风险等级", case_row["risk_level"])
    m2.metric("当前状态", case_row["status"])
    m3.metric("负责人", case_row["assignee"] or "未分配")
    m4.metric("命中规则版本", f"{case_row['rule_name']} {hit_version}")  # 版本留痕硬性展示
    st.caption(f"客户: {cust['name']} ｜ 身份证: {mask_text(cust['id_card'])} ｜ "
               f"手机: {mask_text(cust['phone'])} ｜ 涉案账号: {mask_text(cust['account_no'])}")

    # ---- 时间轴回放 ----
    st.subheader("全生命周期回放")
    logs_df = query_df(
        "SELECT event_type, payload, event_time FROM audit_logs "
        "WHERE case_id = %s ORDER BY event_time, id", (case_id,))
    for _, ev in logs_df.iterrows():
        payload = json.loads(ev["payload"])
        etype = ev["event_type"]
        with st.container(border=True):
            st.markdown(f"**【{etype}】** `{ev['event_time']}`")
            if etype == "数据进入":
                n_tx = len(payload.get("trigger_tx_ids",
                                       payload.get("transaction_ids", [])))
                st.write(f"进入交易快照 {n_tx or payload.get('tx_count', '?')} 笔"
                         + (f"，窗口 {payload['window']['start']} ~ {payload['window']['end']}"
                            if payload.get("window") else ""))
            elif etype == "规则命中":
                st.write(f"命中规则 **{payload.get('rule_name')}** "
                         f"版本 **{payload.get('rule_version')}**")
                if payload.get("condition_str"):
                    st.write(f"条件: `{payload['condition_str']}`"
                             + (f" ｜ 实际满足: {'；'.join(payload['matched_conditions'])}"
                                if payload.get("matched_conditions") else ""))
                if payload.get("semantica_conclusion"):
                    st.caption(
                        f"Semantica 结论: {payload['semantica_conclusion']} ｜ "
                        f"前提事实: {', '.join(payload.get('semantica_premises', []))}")
                feats = payload.get("features", {})
                if feats:
                    st.write(f"24h 特征: 笔数 {feats.get('txn_count_24h')}、"
                             f"总额 {feats.get('total_amount_24h')}、"
                             f"对手数 {feats.get('counterparty_count_24h')}、"
                             f"单笔最大 {feats.get('max_amount_24h')}、"
                             f"夜间笔数 {feats.get('night_count_24h')}")
                txn_ids = payload.get("trigger_tx_ids",
                                      payload.get("transaction_ids", []))
                if txn_ids:
                    txn_df = query_df(
                        f"SELECT id AS 交易ID, tx_time AS 时间, amount AS 金额, "
                        f"counterparty_account AS 对手方账号 FROM transactions "
                        f"WHERE id IN ({','.join(['%s'] * len(txn_ids))}) ORDER BY 时间",
                        tuple(txn_ids))
                    st.markdown("**触发交易明细（对手方已脱敏）**")
                    st.dataframe(mask_dataframe(txn_df),
                                 use_container_width=True, hide_index=True)
            elif etype == "评分变化":
                st.write(f"评分: {payload.get('old_score')} -> "
                         f"**{payload.get('new_score')}**（{payload.get('risk_level')}）｜ "
                         f"{payload.get('reason', '')} ｜ 规则版本 {payload.get('rule_version')}")
            elif etype == "人工处置":
                st.write(f"**{payload.get('operator')}** 结论: {payload.get('conclusion')}")
                st.caption(f"状态变更: {payload.get('old_status')} -> {payload.get('new_status')}")

    # ---- 人工处置录入 ----
    st.subheader("人工处置")
    st.caption(MANDATORY_NOTICE)
    with st.form("human_handle_form"):
        operator_name = st.text_input("处理人", value=case_row["assignee"])
        conclusion = st.text_area("处置结论（必填，由人工填写）")
        new_status = st.selectbox("更新状态", ["处置中", "已归档"],
                                  index=0 if case_row["status"] != "已归档" else 1)
        saved = st.form_submit_button("保存处置结论")
        if saved:
            if not operator_name.strip() or not conclusion.strip():
                st.error("处理人与处置结论均为必填。")
            else:
                now = datetime.now().strftime(FMT)
                execute("UPDATE cases SET status = %s, assignee = %s WHERE id = %s",
                        (new_status, operator_name.strip(), int(case_id)))
                execute(
                    "INSERT INTO audit_logs (case_id, event_type, payload, event_time) "
                    "VALUES (%s, '人工处置', %s, %s)",
                    (int(case_id), json.dumps({
                        "operator": operator_name.strip(),
                        "conclusion": conclusion.strip(),
                        "old_status": case_row["status"],
                        "new_status": new_status,
                        "timestamp": now,
                        "notice": MANDATORY_NOTICE,
                    }, ensure_ascii=False), now))
                st.success("处置结论已保存并写入审计日志。")
                st.rerun()

# ---------------------------------------------------------------------------
# Tab 4: 一键因果报告
# ---------------------------------------------------------------------------
with tab4:
    st.warning(MANDATORY_NOTICE)
    cases4 = query_df(
        "SELECT c.id, r.rule_name, c.status FROM cases c "
        "JOIN rules r ON r.id = c.rule_id ORDER BY c.id")
    if cases4.empty:
        st.info("暂无案件。")
        st.stop()

    case_id4 = st.selectbox(
        "选择案件", cases4["id"].tolist(),
        format_func=lambda i: (
            f"案件#{i} ｜ {cases4.loc[cases4['id'] == i, 'rule_name'].iloc[0]} ｜ "
            f"{cases4.loc[cases4['id'] == i, 'status'].iloc[0]}"))

    if st.button("生成因果报告", type="primary"):
        try:
            md = build_case_report(int(case_id4))
        except Exception as exc:
            st.error(f"报告生成失败: {exc}")
            st.stop()
        st.session_state["report_md"] = md
        st.session_state["report_case"] = int(case_id4)

    if st.session_state.get("report_md") and st.session_state.get("report_case") == case_id4:
        md = st.session_state["report_md"]
        st.download_button("下载 Markdown 报告", md,
                           file_name=f"case_{case_id4}_causal_report.md",
                           mime="text/markdown")
        st.divider()
        st.markdown(md)
