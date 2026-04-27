#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PTrade 研究环境工具：筛选高流动性全 A 股票池。

筛选流程：
    1. 获取全 A 股票池
    2. 剔除 ST
    3. 剔除上市不足 250 个交易日
    4. 剔除停牌
    5. 剔除 20 日均成交额低于 3000 万
    6. 按 60 日平均成交额降序排序
    7. 取前 TOP_N 支股票代码生成 JSON

注意：
    * 本脚本面向 PTrade 研究环境，依赖环境注入的 get_Ashares / get_history 等 API。
    * 不使用 os 模块，避免 PTrade 环境限制。
"""

import json


# =============================================================================
# 配置区
# =============================================================================

# 最终输出股票数量。
TOP_N = 500

# 查询日期。None 表示使用 PTrade 当前研究环境日期；也可写成 "2024-01-31"。
QUERY_DATE = None

# 输出文件相对 PTrade 研究目录的路径。
OUTPUT_REL_PATH = "liquid_ashares_top.json"

# 是否输出带元信息的 JSON。False 时输出纯 list，方便直接复制到策略 config。
WRAP_WITH_METADATA = False

# 上市交易日过滤阈值。
MIN_LISTED_TRADING_DAYS = 250

# 成交额过滤 / 排序窗口。
MIN_20D_AVG_MONEY = 30000000.0
FILTER_AVG_MONEY_DAYS = 20
SORT_AVG_MONEY_DAYS = 60

# get_history 批量查询大小。真实 PTrade 环境如果批量过大，可调小。
BATCH_SIZE = 500

# 成交额字段名。PTrade / 本地 SimTradeLab 通常为 money。
MONEY_FIELD = "money"

# 输出每个批次解析到成交额数据的股票数，便于发现 PTrade 返回格式不兼容。
LOG_HISTORY_PARSE_SUMMARY = True


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


def _chunked(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _safe_status_filter(securities, statuses):
    """优先使用 PTrade 的状态过滤接口，失败时回退到 get_stock_status。"""
    if not securities:
        return []

    try:
        filtered = filter_stock_by_status(securities, statuses)  # noqa: F821 - PTrade 注入
        if filtered is not None:
            return list(filtered)
    except Exception as e:
        _log_info("[select_liquid_ashares_tool] filter_stock_by_status failed: {}".format(e))

    result = list(securities)
    for status in statuses:
        try:
            status_map = get_stock_status(result, status, QUERY_DATE)  # noqa: F821 - PTrade 注入
            result = [s for s in result if not status_map.get(s, False)]
        except Exception as e:
            _log_info("[select_liquid_ashares_tool] get_stock_status {} failed: {}".format(status, e))
    return result


def _get_query_date_from_trade_days():
    try:
        days = get_trade_days(end_date=QUERY_DATE, count=1)  # noqa: F821 - PTrade 注入
        if days:
            return str(days[-1])
    except Exception:
        pass
    return QUERY_DATE


def _listed_cutoff_date():
    """返回满足上市满 MIN_LISTED_TRADING_DAYS 的最晚上市日期。"""
    try:
        days = get_trade_days(  # noqa: F821 - PTrade 注入
            end_date=QUERY_DATE,
            count=MIN_LISTED_TRADING_DAYS + 1,
        )
        if len(days) >= MIN_LISTED_TRADING_DAYS + 1:
            return str(days[0])
    except Exception as e:
        _log_info("[select_liquid_ashares_tool] get_trade_days failed: {}".format(e))
    return None


def _stock_info_map(securities):
    """兼容 SimTradeLab get_stock_info 与部分 PTrade 环境的 get_security_info。"""
    try:
        return get_stock_info(securities, ["stock_name", "listed_date"])  # noqa: F821 - PTrade 注入
    except Exception:
        pass

    result = {}
    for security in securities:
        try:
            info = get_security_info(security)  # noqa: F821 - PTrade 注入
            result[security] = {
                "stock_name": getattr(info, "display_name", security),
                "listed_date": str(getattr(info, "start_date", "")),
            }
        except Exception:
            result[security] = {
                "stock_name": security,
                "listed_date": "",
            }
    return result


def _normalize_date(value):
    text = str(value or "").strip()
    if not text:
        return ""
    return text[:10].replace("/", "-")


def _filter_listed_days(securities):
    cutoff = _listed_cutoff_date()
    if cutoff is None:
        _log_info("[select_liquid_ashares_tool] skip listed-days filter: no cutoff date")
        return securities

    info_map = _stock_info_map(securities)
    result = []
    for security in securities:
        listed_date = _normalize_date(info_map.get(security, {}).get("listed_date"))
        if listed_date and listed_date <= cutoff:
            result.append(security)
    return result


def _history_to_money_map(history, securities):
    """
    将不同返回形态统一成 {security: [money, ...]}。

    常见形态：
        * dict[security] -> DataFrame
        * PanelLike / dict with field key -> DataFrame
        * DataFrame，列为各股票代码
    """
    result = {}
    if history is None:
        return result

    # SimTradeLab / PTrade PanelLike: history[MONEY_FIELD] 是 DataFrame。
    try:
        money_df = history[MONEY_FIELD]
        if len(securities) == 1:
            values = _series_values(money_df)
            if values:
                result[securities[0]] = values
        else:
            for security in securities:
                if security in money_df:
                    result[security] = _series_values(money_df[security])
        if result:
            return result
    except Exception:
        pass

    # PTrade 有些环境会返回长表：每行包含 stock/security/code + money。
    try:
        columns = [str(c) for c in history.columns]
        if MONEY_FIELD in columns:
            security_col = _find_security_column(columns)
            if security_col:
                grouped = {}
                for _, row in history.iterrows():
                    security = str(row.get(security_col, "")).strip()
                    if security not in securities:
                        continue
                    grouped.setdefault(security, []).append(row.get(MONEY_FIELD))
                for security, values in grouped.items():
                    parsed = _series_values(values)
                    if parsed:
                        result[security] = parsed
                if result:
                    return result
            elif len(securities) == 1:
                values = _series_values(history[MONEY_FIELD])
                if values:
                    result[securities[0]] = values
                    return result
    except Exception:
        pass

    # dict[security] -> DataFrame 或 dict[security] -> list。
    if isinstance(history, dict):
        for security in securities:
            data = history.get(security)
            if data is None:
                continue
            try:
                result[security] = _series_values(data[MONEY_FIELD])
            except Exception:
                result[security] = _series_values(data)
        if result:
            return result

    # DataFrame: 单字段批量查询通常是列为股票代码。
    try:
        for security in securities:
            if security in history:
                result[security] = _series_values(history[security])
    except Exception:
        pass
    return result


def _find_security_column(columns):
    for name in ("security", "stock", "code", "order_book_id", "symbol"):
        if name in columns:
            return name
    return None


def _series_values(series_like):
    values = []
    try:
        iterator = series_like.tolist()
    except Exception:
        iterator = series_like
    for value in iterator:
        try:
            if value is None:
                continue
            number = float(value)
            if number == number:
                values.append(number)
        except Exception:
            continue
    return values


def _average_tail(values, count):
    tail = values[-count:]
    if len(tail) < count:
        return None
    return sum(tail) / float(count)


def _load_money_scores(securities):
    scores = []
    history_count = max(FILTER_AVG_MONEY_DAYS, SORT_AVG_MONEY_DAYS)

    for batch_index, batch in enumerate(_chunked(securities, BATCH_SIZE), 1):
        _log_info(
            "[select_liquid_ashares_tool] loading history batch {} | size={}".format(
                batch_index, len(batch)
            )
        )
        try:
            history = get_history(  # noqa: F821 - PTrade 注入
                history_count,
                "1d",
                MONEY_FIELD,
                batch,
                fq=None,
                include=False,
                is_dict=False,
            )
        except TypeError:
            history = get_history(history_count, "1d", MONEY_FIELD, batch)  # noqa: F821 - PTrade 注入
        except Exception as e:
            _log_info("[select_liquid_ashares_tool] batch history failed: {}".format(e))
            history = None

        money_map = _history_to_money_map(history, batch)
        if LOG_HISTORY_PARSE_SUMMARY:
            _log_info(
                "[select_liquid_ashares_tool] parsed money batch {} | securities={}/{}".format(
                    batch_index, len(money_map), len(batch)
                )
            )

        # 部分环境批量返回不稳定时，逐只股票兜底。
        missing = [s for s in batch if s not in money_map]
        for security in missing:
            try:
                single_history = get_history(  # noqa: F821 - PTrade 注入
                    history_count,
                    "1d",
                    MONEY_FIELD,
                    security,
                    fq=None,
                    include=False,
                    is_dict=False,
                )
                single_map = _history_to_money_map(single_history, [security])
                if security in single_map:
                    money_map[security] = single_map[security]
            except Exception:
                continue

        for security, values in money_map.items():
            avg_20 = _average_tail(values, FILTER_AVG_MONEY_DAYS)
            avg_60 = _average_tail(values, SORT_AVG_MONEY_DAYS)
            if avg_20 is None or avg_60 is None:
                continue
            if avg_20 < MIN_20D_AVG_MONEY:
                continue
            scores.append({
                "security": security,
                "avg_20_money": avg_20,
                "avg_60_money": avg_60,
            })

    scores.sort(key=lambda row: row["avg_60_money"], reverse=True)
    return scores


def select_liquid_ashares():
    """执行筛选并写入 JSON，返回最终股票代码列表。"""
    if QUERY_DATE:
        securities = get_Ashares(QUERY_DATE)  # noqa: F821 - PTrade 注入
    else:
        securities = get_Ashares()  # noqa: F821 - PTrade 注入
    securities = sorted(list(set(securities or [])))
    _log_info("[select_liquid_ashares_tool] all A shares: {}".format(len(securities)))

    securities = _safe_status_filter(securities, ["ST"])
    _log_info("[select_liquid_ashares_tool] after ST filter: {}".format(len(securities)))

    securities = _filter_listed_days(securities)
    _log_info("[select_liquid_ashares_tool] after listed-days filter: {}".format(len(securities)))

    securities = _safe_status_filter(securities, ["HALT"])
    _log_info("[select_liquid_ashares_tool] after HALT filter: {}".format(len(securities)))

    scores = _load_money_scores(securities)
    selected_scores = scores[:TOP_N]
    selected = [row["security"] for row in selected_scores]

    if WRAP_WITH_METADATA:
        payload = {
            "query_date": _get_query_date_from_trade_days(),
            "count": len(selected),
            "top_n": TOP_N,
            "filters": {
                "min_listed_trading_days": MIN_LISTED_TRADING_DAYS,
                "min_20d_avg_money": MIN_20D_AVG_MONEY,
                "sort_by": "{}d_avg_money_desc".format(SORT_AVG_MONEY_DAYS),
            },
            "securities": selected,
            "scores": selected_scores,
        }
    else:
        payload = selected

    output_path = _join_research_path(OUTPUT_REL_PATH)
    f = open(output_path, "w", encoding="utf-8")
    try:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    finally:
        f.close()

    _log_info("[select_liquid_ashares_tool] exported {} securities to {}".format(len(selected), output_path))
    return selected


if __name__ == "__main__":
    select_liquid_ashares()
