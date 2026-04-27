# -*- coding: utf-8 -*-
"""
PTrade 研究环境工具：导出全部 A 股股票代码列表。

用途：
    在 PTrade 研究环境中运行本脚本，调用 get_Ashares 获取指定日期可交易 A 股列表，
    可按配置选择是否过滤停牌、退市、ST，然后生成 JSON 文件。

注意：
    * 本脚本依赖 PTrade 研究环境注入的 get_Ashares / get_research_path。
    * 不使用 os 模块，避免 PTrade 环境限制。
"""

import json


# =============================================================================
# 配置区
# =============================================================================

# 输出文件相对 PTrade 研究目录的路径。
OUTPUT_REL_PATH = "ashares_list.json"

# 查询日期。None 表示使用 PTrade 当前研究环境日期；也可写成 "2024-01-31"。
QUERY_DATE = None

# 默认输出纯 list，例如 ["000001.SZ", "000002.SZ", ...]。
# 如果希望带上 count/query_date 等元信息，可改为 True。
WRAP_WITH_METADATA = False

# 是否过滤停牌股票。默认 False，保持“不过滤停牌”的行为。
FILTER_HALT = False

# 是否过滤退市 / 退市整理股票。
FILTER_DELISTING = True

# 是否过滤 ST 股票。
FILTER_ST = True


def _join_research_path(rel_path):
    """拼接 PTrade 研究目录路径。"""
    try:
        base = get_research_path()  # noqa: F821 - PTrade 注入
    except Exception:
        base = "./"
    if not base.endswith("/") and not base.endswith("\\"):
        base = base + "/"
    return base + rel_path


def _log_info(message):
    """兼容 PTrade log 和普通 Python print。"""
    try:
        log.info(message)  # noqa: F821 - PTrade 注入
    except Exception:
        print(message)


def _apply_status_filters(securities):
    """
    根据配置过滤停牌、退市、ST。

    PTrade 中 filter_stock_by_status 会返回过滤后的股票列表。
    如果当前环境不支持该接口或调用失败，则保守返回原列表，避免工具中断。
    """
    status_list = []
    if FILTER_HALT:
        status_list.append("HALT")
    if FILTER_DELISTING:
        status_list.append("DELISTING")
    if FILTER_ST:
        status_list.append("ST")

    if not status_list or not securities:
        return securities

    try:
        filtered = filter_stock_by_status(securities, status_list)  # noqa: F821 - PTrade 注入
        if filtered is None:
            return securities
        _log_info(
            "[get_ashares_tool] status filters applied: {}, before={}, after={}".format(
                ",".join(status_list), len(securities), len(filtered)
            )
        )
        return filtered
    except Exception as e:
        _log_info("[get_ashares_tool] filter_stock_by_status failed, keep original list | err={}".format(e))
        return securities


def export_ashares():
    """
    获取 A 股列表并写入 JSON。

    返回：
        list[str] 股票代码列表
    """
    if QUERY_DATE:
        securities = get_Ashares(QUERY_DATE)  # noqa: F821 - PTrade 注入
    else:
        securities = get_Ashares()  # noqa: F821 - PTrade 注入

    if securities is None:
        securities = []

    securities = _apply_status_filters(securities)

    # 去重并排序，保证输出稳定。
    securities = sorted(list(set(securities)))

    if WRAP_WITH_METADATA:
        payload = {
            "query_date": QUERY_DATE,
            "count": len(securities),
            "filters": {
                "halt": FILTER_HALT,
                "delisting": FILTER_DELISTING,
                "st": FILTER_ST,
            },
            "securities": securities,
        }
    else:
        payload = securities

    output_path = _join_research_path(OUTPUT_REL_PATH)
    f = open(output_path, "w", encoding="utf-8")
    try:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    finally:
        f.close()

    _log_info("[get_ashares_tool] exported {} securities to {}".format(len(securities), output_path))
    return securities


if __name__ == "__main__":
    export_ashares()
