#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
对照"保守撮合 vs 理想撮合"两种 PnL 假设，把当前 trade.csv 的策略表现
扩展为"实盘可达上界"作为分析对照，配合论文/分析里的 conservative-lower-bound
讨论。**不依赖 PTrade**，只读策略写出的 trade.csv。

背景
====
策略在回测中：
  * `check_backtest_exits` 用「上一根已完成 K 线 high / low」判定止盈 / 止损成立；
  * 卖出走 `order(security, -qty)` 市价单，由 PTrade 在下个交易日撮合；
  * 因此 trade.csv 中 PROFIT_TARGET / STOP_LOSS 行的 `sell_price` 与
    `take_profit_price` / `stop_loss_price` 在跳空场景下会脱钩，单笔收益
    被系统性压低（止盈少赚、止损多亏）。

本工具做什么
============
对 trade.csv 每一行：
  * `sell_reason == PROFIT_TARGET`  →  ideal_sell_price = take_profit_price
  * `sell_reason == STOP_LOSS`      →  ideal_sell_price = stop_loss_price
  * 其他原因（TREND_REVERSAL / MAX_HOLDING_DAYS / EXTERNAL_ABORT / 未平仓 / 未成交）
    →  保持原 `sell_price`（这些原因下成交价与触发价没有约束关系）

随后用 `ideal_sell_price - sell_price` 这一份"价位差"重算 ideal_price_pnl /
ideal_pnl。**手续费、分红、除权送股、部分成交分摊** 全部沿用 trade.csv 中原
有的 `pnl / price_pnl / buy_commission / sell_commission / dividend_income`，
只把"价位偏差"叠加进去——避免重新构造分摊逻辑出错。

输出
====
在 `<run_folder>/pnl_ideal/` 下生成：
  1. `trade_ideal.csv`                    - 原 trade.csv 全字段 + 4 列 ideal_*
  2. `pnl_ideal_summary.csv`              - 按 sell_reason 分组的对照摘要
  3. `sell_vs_trigger_distribution.csv`   - sell_price 相对触发价的偏移分布
  4. `cum_pnl_compare.png`                - 按时间累计的"保守 vs 理想"PnL 曲线
  5. 控制台打印：总体差额、按 sell_reason 分组的差额、单笔 gap Top N、
     以及 sell_price vs 触发价的"高于 / 等于 / 低于"分布与百分位
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


# =============================================================================
# Config
# =============================================================================

TARGET_RUN_FOLDER = "../research_path/2016-01-01"
OUTPUT_FOLDER_NAME = "pnl_ideal"

# 哪些 sell_reason 视为"触发价位有明确预期、值得做理想撮合替换"
TRIGGER_PRICE_REASONS = {
    "PROFIT_TARGET": "take_profit_price",
    "STOP_LOSS": "stop_loss_price",
}

# 控制台打印的最大"单笔差额 top N"
TOP_N_GAP = 10


# =============================================================================
# Helpers
# =============================================================================

def script_dir() -> Path:
    return Path(__file__).resolve().parent


def resolve_path(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = script_dir() / path
    return path.resolve()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="生成 trade.csv 的『保守撮合 vs 理想撮合』PnL 对照",
    )
    parser.add_argument("run_folder", nargs="?", default=TARGET_RUN_FOLDER,
                        help="回测输出目录，包含根目录 trade.csv")
    return parser.parse_args()


def read_csv_rows(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"找不到 trade.csv: {path}")
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def write_csv_rows(path: Path, rows: List[Dict[str, object]], header: List[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def parse_float(value: object) -> Optional[float]:
    text = str(value or "").strip()
    if text == "":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_int(value: object) -> Optional[int]:
    f = parse_float(value)
    return int(round(f)) if f is not None else None


def parse_datetime(value: object) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def is_truthy_text(value: object) -> bool:
    text = str(value or "").strip().lower()
    return text in {"true", "1", "yes", "y"}


def iter_progress(items: Iterable, desc: str, total: Optional[int] = None) -> Iterable:
    """tqdm 可选——没装就退回普通 iter，但仍打印阶段提示。"""
    try:
        from tqdm import tqdm  # type: ignore
        return tqdm(items, desc=desc, total=total, ncols=80)
    except Exception:
        print(f"[{desc}]")
        return items


# =============================================================================
# 核心计算
# =============================================================================

def is_closed_trade(row: Dict[str, str]) -> bool:
    """该行 trade 是否为"已成交并已平仓"的完整生命周期。"""
    if not is_truthy_text(row.get("bought", "")):
        return False
    if not str(row.get("sell_date", "")).strip():
        return False
    if parse_float(row.get("sell_price")) is None:
        return False
    if parse_int(row.get("buy_quantity")) in (None, 0):
        return False
    return True


def compute_ideal_row(row: Dict[str, str]) -> Optional[Dict[str, object]]:
    """
    返回一份新的 row（原字段 + ideal_* 4 列）。
    对触发出场行重算 ideal_sell_price / ideal_price_pnl / ideal_pnl / pnl_gap；
    对非触发原因（TREND_REVERSAL 等）保持原 sell_price，gap=0。
    若行不是"已平仓"，直接返回 None（不参与对照）。
    """
    if not is_closed_trade(row):
        return None

    sell_reason = str(row.get("sell_reason", "")).strip()
    quantity = parse_int(row.get("buy_quantity")) or 0
    sell_price = parse_float(row.get("sell_price")) or 0.0
    buy_price = parse_float(row.get("buy_price")) or 0.0
    price_pnl = parse_float(row.get("price_pnl")) or 0.0
    pnl = parse_float(row.get("pnl")) or 0.0

    trigger_field = TRIGGER_PRICE_REASONS.get(sell_reason)
    if trigger_field is not None:
        trigger_price = parse_float(row.get(trigger_field))
        if trigger_price is None or trigger_price <= 0:
            ideal_sell_price = sell_price
        else:
            ideal_sell_price = trigger_price
    else:
        ideal_sell_price = sell_price

    delta = ideal_sell_price - sell_price
    adj = delta * quantity
    ideal_price_pnl = price_pnl + adj
    ideal_pnl = pnl + adj

    out: Dict[str, object] = dict(row)
    out["ideal_sell_price"] = round(ideal_sell_price, 4)
    out["ideal_price_pnl"] = round(ideal_price_pnl, 4)
    out["ideal_pnl"] = round(ideal_pnl, 4)
    out["pnl_gap"] = round(adj, 4)
    # 标识本行是否真正使用了理想价位替换（非触发原因 / 触发价位缺失时为 False）
    out["ideal_applied"] = bool(trigger_field is not None
                                and parse_float(row.get(trigger_field)) is not None
                                and parse_float(row.get(trigger_field, 0)) > 0
                                and abs(delta) > 1e-9)
    return out


# =============================================================================
# 汇总
# =============================================================================

def summarize_by_reason(rows: List[Dict[str, object]]) -> List[Dict[str, object]]:
    """按 sell_reason 分组汇总：trade 数、保守/理想 累计 PnL、差额。"""
    groups: Dict[str, Dict[str, float]] = {}
    for row in rows:
        reason = str(row.get("sell_reason", "")).strip() or "UNKNOWN"
        g = groups.setdefault(reason, {
            "count": 0.0, "pnl_conservative": 0.0, "pnl_ideal": 0.0, "gap": 0.0,
            "win_conservative": 0.0, "win_ideal": 0.0,
        })
        g["count"] += 1
        pnl_c = parse_float(row.get("pnl")) or 0.0
        pnl_i = parse_float(row.get("ideal_pnl")) or 0.0
        g["pnl_conservative"] += pnl_c
        g["pnl_ideal"] += pnl_i
        g["gap"] += (pnl_i - pnl_c)
        if pnl_c > 0:
            g["win_conservative"] += 1
        if pnl_i > 0:
            g["win_ideal"] += 1

    out: List[Dict[str, object]] = []
    total_count = sum(g["count"] for g in groups.values()) or 1.0
    for reason, g in sorted(groups.items(), key=lambda x: -x[1]["count"]):
        count = g["count"]
        out.append({
            "sell_reason": reason,
            "trade_count": int(count),
            "share_pct": round(100.0 * count / total_count, 2),
            "pnl_conservative": round(g["pnl_conservative"], 2),
            "pnl_ideal": round(g["pnl_ideal"], 2),
            "pnl_gap": round(g["gap"], 2),
            "win_rate_conservative_pct": round(100.0 * g["win_conservative"] / max(count, 1), 2),
            "win_rate_ideal_pct": round(100.0 * g["win_ideal"] / max(count, 1), 2),
        })

    # 顶部加一行 TOTAL
    total = {
        "sell_reason": "TOTAL",
        "trade_count": int(total_count),
        "share_pct": 100.0,
        "pnl_conservative": round(sum(g["pnl_conservative"] for g in groups.values()), 2),
        "pnl_ideal": round(sum(g["pnl_ideal"] for g in groups.values()), 2),
        "pnl_gap": round(sum(g["gap"] for g in groups.values()), 2),
        "win_rate_conservative_pct": round(
            100.0 * sum(g["win_conservative"] for g in groups.values()) / total_count, 2),
        "win_rate_ideal_pct": round(
            100.0 * sum(g["win_ideal"] for g in groups.values()) / total_count, 2),
    }
    out.insert(0, total)
    return out


def _percentile(sorted_arr: List[float], p: float) -> float:
    """简单线性插值百分位（p ∈ [0,1]），无外部依赖。空数组返回 0。"""
    if not sorted_arr:
        return 0.0
    if len(sorted_arr) == 1:
        return float(sorted_arr[0])
    pos = max(0.0, min(1.0, p)) * (len(sorted_arr) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_arr) - 1)
    frac = pos - lo
    return float(sorted_arr[lo] * (1 - frac) + sorted_arr[hi] * frac)


def summarize_sell_vs_trigger(rows: List[Dict[str, object]]) -> List[Dict[str, object]]:
    """
    针对 `ideal_applied=True` 的行（即真正发生了理想化替换的 PROFIT_TARGET /
    STOP_LOSS 行），按 sell_reason 分组统计 `sell_price - trigger_price`
    的分布。trigger_price = take_profit_price (止盈) 或 stop_loss_price (止损)。

    用途：判断回测的"延迟撮合"对策略到底是系统性有利还是不利——
      * above_trigger_pct 越高，意味着 sell_price 经常**高于**触发价
          - 止盈：相当于"卖在了 take_profit 之上"，回测多赚（延迟红利）
          - 止损：相当于"卖在了 stop_loss 之上"，回测亏少了（延迟红利）
      * 反过来 below_trigger_pct 越高，说明回测把价格压低了，实盘相对受益。
    """
    groups: Dict[str, List[Tuple[float, float]]] = {}
    for row in rows:
        if not row.get("ideal_applied"):
            continue
        reason = str(row.get("sell_reason", "")).strip()
        trigger_field = TRIGGER_PRICE_REASONS.get(reason)
        if trigger_field is None:
            continue
        sell_price = parse_float(row.get("sell_price"))
        trigger_price = parse_float(row.get(trigger_field))
        if sell_price is None or trigger_price is None or trigger_price <= 0:
            continue
        diff = sell_price - trigger_price
        diff_pct = diff / trigger_price * 100.0
        groups.setdefault(reason, []).append((diff, diff_pct))

    out: List[Dict[str, object]] = []
    tol = 1e-6
    for reason in sorted(groups.keys()):
        samples = groups[reason]
        diffs = [d for d, _ in samples]
        diffs_pct = [p for _, p in samples]
        n = len(diffs)
        above = sum(1 for d in diffs if d > tol)
        below = sum(1 for d in diffs if d < -tol)
        equal = n - above - below

        sorted_diffs = sorted(diffs)
        sorted_pct = sorted(diffs_pct)
        out.append({
            "sell_reason": reason,
            "trade_count": n,
            "above_trigger_count": above,
            "below_trigger_count": below,
            "equal_count": equal,
            "above_trigger_pct": round(100.0 * above / n, 2),
            "below_trigger_pct": round(100.0 * below / n, 2),
            "equal_pct": round(100.0 * equal / n, 2),
            "mean_diff_abs": round(sum(diffs) / n, 4),
            "median_diff_abs": round(_percentile(sorted_diffs, 0.5), 4),
            "min_diff_abs": round(sorted_diffs[0], 4),
            "max_diff_abs": round(sorted_diffs[-1], 4),
            "mean_diff_pct": round(sum(diffs_pct) / n, 3),
            "median_diff_pct": round(_percentile(sorted_pct, 0.5), 3),
            "p5_diff_pct": round(_percentile(sorted_pct, 0.05), 3),
            "p25_diff_pct": round(_percentile(sorted_pct, 0.25), 3),
            "p75_diff_pct": round(_percentile(sorted_pct, 0.75), 3),
            "p95_diff_pct": round(_percentile(sorted_pct, 0.95), 3),
        })
    return out


def build_cumulative_series(rows: List[Dict[str, object]]) -> Tuple[List[datetime], List[float], List[float]]:
    """按 sell_date 排序后构造累计 PnL 序列。返回 (dates, cum_conservative, cum_ideal)。"""
    pairs: List[Tuple[datetime, float, float]] = []
    for row in rows:
        dt = parse_datetime(row.get("sell_date"))
        if dt is None:
            continue
        c = parse_float(row.get("pnl")) or 0.0
        i = parse_float(row.get("ideal_pnl")) or 0.0
        pairs.append((dt, c, i))
    pairs.sort(key=lambda x: x[0])

    dates: List[datetime] = []
    cum_c: List[float] = []
    cum_i: List[float] = []
    acc_c = 0.0
    acc_i = 0.0
    for dt, c, i in pairs:
        acc_c += c
        acc_i += i
        dates.append(dt)
        cum_c.append(acc_c)
        cum_i.append(acc_i)
    return dates, cum_c, cum_i


# =============================================================================
# 图表
# =============================================================================

def plot_cumulative(dates: List[datetime], cum_c: List[float], cum_i: List[float],
                    output_path: Path) -> None:
    """画"保守 vs 理想"累计 PnL 曲线，并填充两条线之间的差值区域。"""
    if not dates:
        print("[WARN] 没有可画的累计 PnL 序列（trade.csv 里没有已平仓行）")
        return

    import matplotlib
    matplotlib.use("Agg", force=True)
    import matplotlib.pyplot as plt
    from matplotlib.dates import DateFormatter, AutoDateLocator

    fig, ax = plt.subplots(figsize=(12, 6), dpi=140)
    ax.plot(dates, cum_c, label="Conservative (PTrade 实际撮合)",
            color="#1f77b4", linewidth=1.8)
    ax.plot(dates, cum_i, label="Ideal (PROFIT_TARGET / STOP_LOSS 按触发价成交)",
            color="#d62728", linewidth=1.8)
    ax.fill_between(dates, cum_c, cum_i,
                    where=[i >= c for c, i in zip(cum_c, cum_i)],
                    color="#d62728", alpha=0.10, interpolate=True,
                    label="Ideal − Conservative (≥0)")
    ax.fill_between(dates, cum_c, cum_i,
                    where=[i < c for c, i in zip(cum_c, cum_i)],
                    color="#1f77b4", alpha=0.10, interpolate=True)

    final_c = cum_c[-1] if cum_c else 0.0
    final_i = cum_i[-1] if cum_i else 0.0
    gap_pct = ((final_i - final_c) / abs(final_c) * 100.0) if final_c else 0.0
    ax.set_title(
        "Cumulative PnL: Conservative vs Ideal Fill\n"
        f"final conservative={final_c:,.2f}   final ideal={final_i:,.2f}   "
        f"gap={final_i - final_c:,.2f} ({gap_pct:+.2f}% over conservative)",
        fontsize=11, fontweight="bold",
    )
    ax.set_xlabel("Sell Date")
    ax.set_ylabel("Cumulative PnL (RMB)")
    ax.grid(axis="y", linestyle=":", alpha=0.45)
    ax.legend(loc="upper left", fontsize=9, framealpha=0.92)

    ax.xaxis.set_major_locator(AutoDateLocator())
    ax.xaxis.set_major_formatter(DateFormatter("%Y-%m"))
    fig.autofmt_xdate(rotation=30)

    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches="tight")
    plt.close(fig)
    print(f"[OK] 累计 PnL 对照图: {output_path}")


# =============================================================================
# 控制台报告
# =============================================================================

def _print_table(header: List[str], rows: List[Dict[str, object]],
                 min_col_width: int = 16, float_fmt: str = ",.2f") -> None:
    """通用行打印工具：根据 header 找列，每列右对齐，浮点统一格式化。"""
    col_w = {h: max(len(h), min_col_width) for h in header}
    print("  " + "  ".join(h.rjust(col_w[h]) for h in header))
    for row in rows:
        line_parts = []
        for h in header:
            val = row.get(h, "")
            if isinstance(val, float):
                line_parts.append(f"{val:>{col_w[h]}{float_fmt}}")
            else:
                line_parts.append(str(val).rjust(col_w[h]))
        print("  " + "  ".join(line_parts))


def print_report(rows: List[Dict[str, object]], summary: List[Dict[str, object]],
                 distribution: List[Dict[str, object]]) -> None:
    print()
    print("==== 总体对照（按 sell_reason 分组） ====")
    _print_table(
        header=["sell_reason", "trade_count", "share_pct",
                "pnl_conservative", "pnl_ideal", "pnl_gap",
                "win_rate_conservative_pct", "win_rate_ideal_pct"],
        rows=summary,
        min_col_width=18,
    )

    # Top N 单笔 gap
    triggered = [r for r in rows if r.get("ideal_applied")]
    if triggered:
        print()
        print(f"==== 单笔 |pnl_gap| Top {TOP_N_GAP}（仅 PROFIT_TARGET / STOP_LOSS） ====")
        triggered_sorted = sorted(
            triggered,
            key=lambda r: abs(parse_float(r.get("pnl_gap")) or 0.0),
            reverse=True,
        )
        _print_table(
            header=["security", "buy_date", "sell_date", "sell_reason",
                    "buy_price", "sell_price", "ideal_sell_price",
                    "pnl", "ideal_pnl", "pnl_gap"],
            rows=triggered_sorted[:TOP_N_GAP],
            min_col_width=14,
            float_fmt=",.4f",
        )

    # sell_price vs 触发价的分布（"延迟红利"检验的核心输出）
    if distribution:
        print()
        print("==== sell_price 相对触发价的分布 ====")
        print("  说明：above_pct 高 → 实际成交价系统性 > 触发价（回测里有『延迟红利』，实盘大概率更差）")
        print("       below_pct 高 → 实际成交价系统性 < 触发价（实盘理论会更好）")
        print()
        print("  -- 占比表 --")
        _print_table(
            header=["sell_reason", "trade_count",
                    "above_trigger_pct", "below_trigger_pct", "equal_pct"],
            rows=distribution,
            min_col_width=16,
        )
        print()
        print("  -- 偏移量统计（mean / median 单位 RMB/股；pct 相对触发价的百分比）--")
        _print_table(
            header=["sell_reason",
                    "mean_diff_abs", "median_diff_abs",
                    "min_diff_abs", "max_diff_abs",
                    "mean_diff_pct", "median_diff_pct",
                    "p5_diff_pct", "p25_diff_pct", "p75_diff_pct", "p95_diff_pct"],
            rows=distribution,
            min_col_width=14,
            float_fmt=",.4f",
        )


# =============================================================================
# main
# =============================================================================

def main() -> None:
    args = parse_args()
    run_dir = resolve_path(args.run_folder)
    if not run_dir.exists() or not run_dir.is_dir():
        raise FileNotFoundError(f"回测输出目录不存在: {run_dir}")
    trade_csv = run_dir / "trade.csv"
    output_dir = run_dir / OUTPUT_FOLDER_NAME

    print(f"读取: {trade_csv}")
    raw_rows = read_csv_rows(trade_csv)
    print(f"trade.csv 行数: {len(raw_rows)}")

    if not raw_rows:
        print("[FATAL] trade.csv 是空的——没有可对照的内容")
        sys.exit(1)

    ideal_rows: List[Dict[str, object]] = []
    skipped = 0
    for row in iter_progress(raw_rows, desc="Computing ideal pnl", total=len(raw_rows)):
        result = compute_ideal_row(row)
        if result is None:
            skipped += 1
            continue
        ideal_rows.append(result)
    print(f"已平仓行: {len(ideal_rows)} | 跳过(未成交/未平仓): {skipped}")

    if not ideal_rows:
        print("[FATAL] 没有已平仓的 trade，无法做对照分析")
        sys.exit(1)

    # 1) 写 trade_ideal.csv
    base_header = list(raw_rows[0].keys())
    ext_header = base_header + [
        "ideal_sell_price", "ideal_price_pnl", "ideal_pnl", "pnl_gap", "ideal_applied",
    ]
    write_csv_rows(output_dir / "trade_ideal.csv", ideal_rows, ext_header)
    print(f"[OK] {output_dir / 'trade_ideal.csv'}")

    # 2) 按 sell_reason 汇总
    summary = summarize_by_reason(ideal_rows)
    summary_header = list(summary[0].keys())
    write_csv_rows(output_dir / "pnl_ideal_summary.csv", summary, summary_header)
    print(f"[OK] {output_dir / 'pnl_ideal_summary.csv'}")

    # 3) sell_price 与触发价的分布（"延迟红利"检验）
    distribution = summarize_sell_vs_trigger(ideal_rows)
    if distribution:
        dist_header = list(distribution[0].keys())
        write_csv_rows(output_dir / "sell_vs_trigger_distribution.csv",
                       distribution, dist_header)
        print(f"[OK] {output_dir / 'sell_vs_trigger_distribution.csv'}")
    else:
        print("[WARN] 没有任何 PROFIT_TARGET / STOP_LOSS 行可用于分布统计")

    # 4) 累计 PnL 曲线对照图
    dates, cum_c, cum_i = build_cumulative_series(ideal_rows)
    plot_cumulative(dates, cum_c, cum_i, output_dir / "cum_pnl_compare.png")

    # 5) 控制台简报
    print_report(ideal_rows, summary, distribution)

    print()
    print("Done.")


if __name__ == "__main__":
    main()
