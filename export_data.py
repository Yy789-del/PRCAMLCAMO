# -*- coding: utf-8 -*-
"""export_data.py — 把 MySQL risk_audit 库的六张表导出为落盘的 Excel 文件。

产物: risk_audit_data.xlsx（与本脚本同目录），多 Sheet、中文列名、敏感字段已脱敏。
用途: 课程答辩时直接双击用 Excel 打开，向老师展示系统内的原始输入数据与运行结果。

运行: python export_data.py
"""
import datetime
import json
import os

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from risk_engine import DB_PATH, MANDATORY_NOTICE, get_engine, mask_text

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(BASE_DIR, "risk_audit_data.xlsx")

FONT_NAME = "微软雅黑"
HEADER_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(name=FONT_NAME, size=11, bold=True, color="FFFFFF")
TITLE_FONT = Font(name=FONT_NAME, size=14, bold=True, color="1F3864")
BODY_FONT = Font(name=FONT_NAME, size=10)
ZEBRA_FILL = PatternFill("solid", fgColor="F7F9FC")
KPI_FILL = PatternFill("solid", fgColor="FFF2CC")
_THIN = Side(style="thin", color="D9DEE7")
BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
LEVEL_COLORS = {"HIGH": "C00000", "MEDIUM": "BF6B00", "LOW": "1F7A1F"}


def _disp_width(text) -> int:
    """中文字符按 2 个宽度计。"""
    s = "" if text is None else str(text)
    return sum(2 if ord(ch) > 127 else 1 for ch in s)


def _write_table(ws, headers, rows, wrap_cols=(), freeze="A2"):
    """写入表头 + 数据，并套用斑马纹 / 细边框 / 自适应列宽。"""
    ws.append(headers)
    for c in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=c)
        cell.fill, cell.font, cell.alignment, cell.border = HEADER_FILL, HEADER_FONT, CENTER, BORDER
    ws.row_dimensions[1].height = 26

    for i, row in enumerate(rows, start=2):
        ws.append(row)
        for c in range(1, len(headers) + 1):
            cell = ws.cell(row=i, column=c)
            cell.font, cell.border = BODY_FONT, BORDER
            cell.alignment = Alignment(vertical="center",
                                       wrap_text=(c in wrap_cols),
                                       horizontal="center" if c in wrap_cols else "left")
            if i % 2 == 0:
                cell.fill = ZEBRA_FILL

    # 自适应列宽（上限 60，带 wrap 的列固定宽）
    for c, head in enumerate(headers, start=1):
        width = _disp_width(head)
        for row in rows:
            width = max(width, _disp_width(row[c - 1]))
        col = get_column_letter(c)
        ws.column_dimensions[col].width = 60 if c in wrap_cols else min(max(width + 3, 10), 46)

    ws.freeze_panes = freeze
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{len(rows) + 1}"
    return ws


def main():
    eng = get_engine()

    # ---------------- 六张表读取 -----------------------------------------
    customers = pd.read_sql_query(
        "SELECT id, name, id_card, phone FROM customers ORDER BY id", eng)
    accounts = pd.read_sql_query(
        "SELECT id, customer_id, account_no FROM accounts ORDER BY id", eng)
    transactions = pd.read_sql_query(
        "SELECT t.id, t.account_id, a.account_no, c.name AS customer_name, "
        "t.counterparty_account, t.amount, t.tx_time "
        "FROM transactions t "
        "LEFT JOIN accounts a ON a.id = t.account_id "
        "LEFT JOIN customers c ON c.id = a.customer_id ORDER BY t.id", eng)
    rules = pd.read_sql_query(
        "SELECT id, rule_name, condition_str, risk_level, version, updated_by, "
        "updated_at, is_active FROM rules ORDER BY id", eng)
    cases = pd.read_sql_query(
        "SELECT c.id, c.account_id, c.rule_id, r.rule_name, c.risk_level, c.status, "
        "c.assignee, c.created_at FROM cases c LEFT JOIN rules r ON r.id = c.rule_id "
        "ORDER BY c.id", eng)
    audit = pd.read_sql_query(
        "SELECT id, case_id, event_type, payload, event_time FROM audit_logs ORDER BY id", eng)

    # ---------------- 案件 -> 命中时规则版本（取最近一次"规则命中"事件） -------
    hit_ver = {}
    for _, row in audit[audit["event_type"] == "规则命中"].iterrows():
        try:
            hit_ver[int(row["case_id"])] = json.loads(row["payload"]).get("rule_version", "-")
        except Exception:
            hit_ver[int(row["case_id"])] = "-"

    # ---------------- 组装中文列（敏感字段脱敏） --------------------------
    def fmt_dt(v):
        return v.strftime("%Y-%m-%d %H:%M:%S") if hasattr(v, "strftime") else v

    cust_rows = [[int(r["id"]), r["name"], mask_text(r["id_card"]), mask_text(r["phone"])]
                 for _, r in customers.iterrows()]
    acct_rows = [[int(r["id"]), int(r["customer_id"]), mask_text(r["account_no"])]
                 for _, r in accounts.iterrows()]
    txn_rows = [[int(r["id"]), int(r["account_id"]), mask_text(r["account_no"]),
                 r["customer_name"], mask_text(r["counterparty_account"]),
                 float(r["amount"]), fmt_dt(r["tx_time"])]
                for _, r in transactions.iterrows()]
    rule_rows = [[int(r["id"]), r["rule_name"], r["condition_str"], r["risk_level"],
                  r["version"], r["updated_by"], fmt_dt(r["updated_at"]),
                  "启用" if int(r["is_active"]) == 1 else "停用"]
                 for _, r in rules.iterrows()]
    case_rows = [[int(r["id"]), int(r["account_id"]), int(r["rule_id"]), r["rule_name"],
                  r["risk_level"], r["status"], r["assignee"], fmt_dt(r["created_at"]),
                  hit_ver.get(int(r["id"]), "-")]
                 for _, r in cases.iterrows()]
    audit_rows = [[int(r["id"]), int(r["case_id"]), r["event_type"],
                   str(r["payload"])[:30000], fmt_dt(r["event_time"])]
                  for _, r in audit.iterrows()]

    # ---------------- 建工作簿 -------------------------------------------
    wb = Workbook()
    wb.remove(wb.active)

    # 1) 说明页
    ws0 = wb.create_sheet("说明")
    ws0["A1"] = "可穿透风控与反洗钱因果审计中台 —— 数据导出总览"
    ws0["A1"].font = TITLE_FONT
    ws0.merge_cells("A1:E1")
    ws0["A2"] = f"数据来源：MySQL 业务库 {DB_PATH}"
    ws0["A3"] = f"导出时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    ws0["A4"] = ("合规声明：本文件中的身份证号 / 手机号 / 账号已按监管要求脱敏（中间位以 * 替代）；"
                 f"{MANDATORY_NOTICE}；系统不含任何自动冻结、自动拒贷逻辑。")
    for k in ("A2", "A3", "A4"):
        ws0[k].font = BODY_FONT
    ws0["A4"].alignment = Alignment(vertical="center", wrap_text=True)

    ws0.append([])
    ws0.append(["序号", "表名（Sheet）", "中文表名", "记录行数", "说明"])
    tables = [
        ("客户表", "客户信息表 customers", "=COUNTA('客户表'!A2:A100000)", "客户基础信息，证件号与手机号已脱敏"),
        ("账户表", "账户信息表 accounts", "=COUNTA('账户表'!A2:A100000)", "客户名下的银行账户，账号已脱敏"),
        ("交易流水表", "交易流水表 transactions", "=COUNTA('交易流水表'!A2:A100000)",
         "全部交易流水（含违规 / 正常 / 边界三类场景）"),
        ("规则表", "规则配置表 rules", "=COUNTA('规则表'!A2:A100000)", "20 条风控规则，含条件表达式与版本留痕"),
        ("案件表", "预警案件表 cases", "=COUNTA('案件表'!A2:A100000)", "规则命中后生成的预警案件，含命中时规则版本"),
        ("审计日志表", "审计日志表 audit_logs", "=COUNTA('审计日志表'!A2:A100000)",
         "全生命周期事件流：数据进入 → 规则命中 → 评分变化 → 人工处置"),
    ]
    for i, (sheet, cn, formula, note) in enumerate(tables, start=1):
        ws0.append([i, sheet, cn, formula, note])

    header_row = 6
    for c in range(1, 6):
        cell = ws0.cell(row=header_row, column=c)
        cell.fill, cell.font, cell.alignment, cell.border = HEADER_FILL, HEADER_FONT, CENTER, BORDER
    for r in range(header_row + 1, header_row + 1 + len(tables)):
        for c in range(1, 6):
            cell = ws0.cell(row=r, column=c)
            cell.font, cell.border = BODY_FONT, BORDER
            cell.alignment = Alignment(vertical="center",
                                       horizontal="center" if c in (1, 2, 4) else "left",
                                       wrap_text=(c == 5))
            if r % 2 == 0:
                cell.fill = ZEBRA_FILL
    ws0.row_dimensions[header_row].height = 26
    for c, w in zip(range(1, 6), (6, 14, 30, 12, 56)):
        ws0.column_dimensions[get_column_letter(c)].width = w

    kpi_row = header_row + len(tables) + 2
    ws0.cell(row=kpi_row, column=1, value="核心指标").font = Font(
        name=FONT_NAME, size=11, bold=True, color="1F3864")
    ws0.cell(row=kpi_row + 1, column=1, value="总交易流水（笔）")
    ws0.cell(row=kpi_row + 1, column=2, value="=COUNTA('交易流水表'!A2:A100000)")
    ws0.cell(row=kpi_row + 2, column=1, value="预警案件数（件）")
    ws0.cell(row=kpi_row + 2, column=2, value="=COUNTA('案件表'!A2:A100000)")
    ws0.cell(row=kpi_row + 3, column=1, value="规则数（条）")
    ws0.cell(row=kpi_row + 3, column=2, value="=COUNTA('规则表'!A2:A100000)")
    for r in range(kpi_row + 1, kpi_row + 4):
        for c in (1, 2):
            cell = ws0.cell(row=r, column=c)
            cell.font = Font(name=FONT_NAME, size=10, bold=True, color="1F3864")
            cell.fill, cell.border = KPI_FILL, BORDER
            cell.alignment = Alignment(horizontal="center" if c == 2 else "left", vertical="center")
    ws0.cell(row=kpi_row + 5, column=1,
             value=f"注：{MANDATORY_NOTICE}").font = Font(
        name=FONT_NAME, size=10, bold=True, color="C00000")

    # 2) 六张数据表
    _write_table(wb.create_sheet("客户表"),
                 ["客户ID", "姓名", "身份证号（已脱敏）", "手机号（已脱敏）"], cust_rows)
    _write_table(wb.create_sheet("账户表"),
                 ["账户ID", "客户ID", "账号（已脱敏）"], acct_rows)
    _write_table(wb.create_sheet("交易流水表"),
                 ["交易ID", "账户ID", "账号（已脱敏）", "客户姓名",
                  "对手方账号（已脱敏）", "金额(元)", "交易时间"], txn_rows)
    _write_table(wb.create_sheet("规则表"),
                 ["规则ID", "规则名称", "条件表达式", "风险等级", "版本",
                  "修改人", "修改时间", "是否启用"], rule_rows)
    _write_table(wb.create_sheet("案件表"),
                 ["案件ID", "账户ID", "规则ID", "命中规则", "风险等级",
                  "状态", "负责人", "创建时间", "命中版本"], case_rows)
    _write_table(wb.create_sheet("审计日志表"),
                 ["日志ID", "案件ID", "事件类型", "载荷JSON", "事件时间"],
                 audit_rows, wrap_cols=(4,))

    # 3) 语义化格式：风险等级着色、金额千分位
    for sheet_name, level_col, amount_col in (("案件表", 5, None),
                                              ("规则表", 4, None),
                                              ("交易流水表", None, 6)):
        ws = wb[sheet_name]
        if level_col:
            for r in range(2, ws.max_row + 1):
                cell = ws.cell(row=r, column=level_col)
                color = LEVEL_COLORS.get(str(cell.value))
                if color:
                    cell.font = Font(name=FONT_NAME, size=10, bold=True, color=color)
                    cell.alignment = Alignment(horizontal="center", vertical="center")
        if amount_col:
            for r in range(2, ws.max_row + 1):
                cell = ws.cell(row=r, column=amount_col)
                cell.number_format = "#,##0.00"
                cell.alignment = Alignment(horizontal="right", vertical="center")

    # 4) 打开文件时直接落在【交易流水表】（逐条明细），总览页放到最后
    wb.move_sheet("交易流水表", offset=-3)
    wb.active = wb["交易流水表"]

    wb.save(OUT_PATH)

    print(f"导出完成: {OUT_PATH}")
    print(f"  客户表 {len(cust_rows)} 行 | 账户表 {len(acct_rows)} 行 | "
          f"交易流水表 {len(txn_rows)} 行 | 规则表 {len(rule_rows)} 行 | "
          f"案件表 {len(case_rows)} 行 | 审计日志表 {len(audit_rows)} 行")


if __name__ == "__main__":
    main()
