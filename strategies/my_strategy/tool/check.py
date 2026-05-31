#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
专门用来"用图复盘一条具体 trade.csv 记录"的独立工具脚本。

设计目标
========
1. **不依赖 PTrade / SimTradeLab 运行时**：只读项目里已有的离线数据源
   （`data/stocks/<security>.parquet` + `data/ptrade_adj_pre.parquet`），
   在本地任意 Python 环境就能跑。
2. **聚焦单条 trade**：把 countdown 的 13 根 K 线、买入当日 K 线、卖出日前一日 K 线、
   卖出当日 K 线（共 ~16 根）按发生顺序排列在同一张图里——日历上跨度长达半年
   的非连续 K 线被压缩到等宽 x 轴，避免长空白。
3. **把所有关键价位标到图上**：每根 K 标 O/H/L/C 与 `count_n`，止损 /
   止盈 / 买入价 / 卖出价用水平线 + 箭头双重标注，看图即能验证。

修改下面的 TARGET_TRADE 字典就能换一条 trade 复盘，无需改主逻辑。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
import matplotlib

matplotlib.use("Agg", force=True)
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle


# =============================================================================
# 用户配置区
# =============================================================================
# 1) 待复盘的 trade.csv 行——直接照抄 README.md 里那条 000720.SZ / 2017-10-27 的记录。
TARGET_TRADE: Dict[str, object] = {
    "security": "000720.SZ",
    "setup_completed_at": "2017-04-20",
    "setup_is_perfect": True,
    "setup_highest_high": 6.16,
    # countdown 第 1~13 根计数 K 线日期（注意：count_1_at 与 setup_completed_at 在数据列里都是同一天）
    "count_dates": {
        1: "2017-04-20",
        2: "2017-04-24",
        3: "2017-04-25",
        4: "2017-05-04",
        5: "2017-05-05",
        6: "2017-05-08",
        7: "2017-05-09",
        8: "2017-05-10",
        9: "2017-05-23",
        10: "2017-05-24",
        11: "2017-06-01",
        12: "2017-06-14",
        13: "2017-06-21",
    },
    "count_8_close": 4.9,
    "countdown_is_perfect": False,
    "countdown_status": "NORMAL",
    "buy_date": "2017-06-22",
    "buy_price": 5.0125,
    "buy_quantity": 2000,
    "buy_commission": 5.4882,
    "entry_value": 10025.0,
    "stop_loss_price": 4.41,
    "take_profit_price": 5.9163,
    "sell_date": "2017-10-27",
    "sell_price": 5.2874,
    "sell_commission": 16.0897,
    "sell_reason": "PROFIT_TARGET",
    "pnl": 528.13,
}

# 2) 数据源路径（相对脚本路径解析，方便仓库内任意位置运行）
DATA_STOCK_DIR = "../../../data/stocks"          # 原始未复权日 K
ADJ_PRE_PATH = "../../../data/ptrade_adj_pre.parquet"  # 全市场前复权因子

# 3) 复权口径，对齐策略：strategy 配置 fq="pre" → 前复权
APPLY_PRE_ADJ = True

# 4) 输出目录（脚本同级 check_output/<security>_<sell_date>.png）
OUTPUT_DIR_NAME = "check_output"

# 5) 批量模式：当 BATCH_TRADE_CSV 非空时，自动读取该 trade.csv，对每条已平仓行
#    （bought=True 且 sell_date 非空）各画一张图，忽略 TARGET_TRADE 配置。
#    路径可写相对脚本的相对路径或绝对路径。
BATCH_TRADE_CSV = "../research_path/2016-01-01_1/000720SZ/trade.csv"


# =============================================================================
# 路径辅助
# =============================================================================

def script_dir() -> Path:
    return Path(__file__).resolve().parent


def resolve_under_script(rel: str) -> Path:
    p = Path(rel)
    if not p.is_absolute():
        p = (script_dir() / rel).resolve()
    return p


# =============================================================================
# 数据加载与前复权
# =============================================================================

def load_kline(security: str) -> pd.DataFrame:
    """读未复权日 K，按交易日索引升序返回，至少含 open/high/low/close。"""
    parquet_path = resolve_under_script(DATA_STOCK_DIR) / f"{security}.parquet"
    if not parquet_path.exists():
        raise FileNotFoundError(f"找不到原始 K 线文件: {parquet_path}")
    df = pd.read_parquet(parquet_path)
    if "date" in df.columns:
        df = df.set_index("date")
    df.index = pd.to_datetime(df.index)
    df = df.sort_index()
    needed = {"open", "high", "low", "close"}
    missing = needed - set(df.columns)
    if missing:
        raise ValueError(f"{security} 缺少必要字段: {missing}")
    return df


def load_pre_adj_factors(security: str) -> Optional[pd.DataFrame]:
    """读全市场前复权因子 parquet，过滤出本标的，返回 DataFrame[adj_a, adj_b]，日期索引。"""
    path = resolve_under_script(ADJ_PRE_PATH)
    if not path.exists():
        print(f"[WARN] 找不到前复权因子文件: {path}（将退化为未复权 K）")
        return None
    combined = pd.read_parquet(path)
    if "symbol" not in combined.columns:
        print("[WARN] 复权因子文件无 symbol 列，跳过复权")
        return None
    sub = combined[combined["symbol"] == security].copy()
    if sub.empty:
        print(f"[WARN] 复权因子里没有 {security} 的记录，退化为未复权")
        return None
    if "date" in sub.columns:
        sub = sub.set_index("date")
    elif "index" in sub.columns:
        sub = sub.set_index("index")
    sub.index = pd.to_datetime(sub.index)
    sub = sub.sort_index()
    keep_cols = [c for c in ("adj_a", "adj_b") if c in sub.columns]
    if len(keep_cols) < 2:
        print(f"[WARN] 复权因子缺少 adj_a/adj_b 列，跳过复权")
        return None
    return sub[keep_cols]


def apply_pre_adj(kline: pd.DataFrame, adj: Optional[pd.DataFrame]) -> pd.DataFrame:
    """套用 adj_a / adj_b 做前复权：adj_price = adj_a * raw + adj_b，保留 O/H/L/C。"""
    if adj is None or not APPLY_PRE_ADJ:
        return kline.copy()
    # 复权因子未覆盖的日期前向填充——simtradelab 也是同样口径（默认 1/0）。
    merged = kline.join(adj, how="left")
    merged["adj_a"] = merged["adj_a"].ffill().fillna(1.0)
    merged["adj_b"] = merged["adj_b"].ffill().fillna(0.0)
    out = merged.copy()
    for col in ("open", "high", "low", "close"):
        out[col] = (out["adj_a"] * merged[col] + merged["adj_b"]).round(4)
    return out.drop(columns=["adj_a", "adj_b"])


# =============================================================================
# 批量模式：从 trade.csv 一行还原成 TARGET_TRADE 同款字典
# =============================================================================

def _csv_text(row: Dict[str, str], key: str) -> str:
    return str(row.get(key, "") or "").strip()


def _csv_bool(row: Dict[str, str], key: str) -> bool:
    return _csv_text(row, key).lower() == "true"


def _csv_float(row: Dict[str, str], key: str, default: float = 0.0) -> float:
    text = _csv_text(row, key)
    if not text:
        return default
    try:
        return float(text)
    except ValueError:
        return default


def _csv_int(row: Dict[str, str], key: str, default: int = 0) -> int:
    f = _csv_float(row, key, float(default))
    try:
        return int(round(f))
    except (TypeError, ValueError):
        return default


def parse_count_dates(row: Dict[str, str]) -> Dict[int, str]:
    """从 trade.csv 一行抽取 count_1_at ... count_13_at，输出 {n: 'YYYY-MM-DD'}。"""
    out: Dict[int, str] = {}
    for n in range(1, 14):
        text = _csv_text(row, f"count_{n}_at")
        if text:
            out[n] = text[:10]
    return out


def trade_dict_from_csv_row(row: Dict[str, str]) -> Optional[Dict[str, object]]:
    """
    把 trade.csv 的一行转换为 plot_trade 需要的 TARGET_TRADE 同款字典。
    只对"已成功买入且已平仓"的行返回；其他（CANCEL / 未成交）返回 None。
    """
    if not _csv_bool(row, "bought"):
        return None
    if not _csv_text(row, "sell_date") or not _csv_text(row, "buy_date"):
        return None
    count_dates = parse_count_dates(row)
    if not count_dates:
        return None
    return {
        "security": _csv_text(row, "security"),
        "setup_completed_at": _csv_text(row, "setup_completed_at")[:10],
        "setup_is_perfect": _csv_bool(row, "setup_is_perfect"),
        "setup_highest_high": _csv_float(row, "setup_highest_high"),
        "count_dates": count_dates,
        "count_8_close": _csv_float(row, "count_8_close"),
        "countdown_is_perfect": _csv_bool(row, "countdown_is_perfect"),
        "countdown_status": _csv_text(row, "countdown_status") or "NORMAL",
        "buy_date": _csv_text(row, "buy_date")[:10],
        "buy_price": _csv_float(row, "buy_price"),
        "buy_quantity": _csv_int(row, "buy_quantity"),
        "buy_commission": _csv_float(row, "buy_commission"),
        "entry_value": _csv_float(row, "entry_value"),
        "stop_loss_price": _csv_float(row, "stop_loss_price"),
        "take_profit_price": _csv_float(row, "take_profit_price"),
        "sell_date": _csv_text(row, "sell_date")[:10],
        "sell_price": _csv_float(row, "sell_price"),
        "sell_commission": _csv_float(row, "sell_commission"),
        "sell_reason": _csv_text(row, "sell_reason"),
        "pnl": _csv_float(row, "pnl"),
    }


# =============================================================================
# 选取要画的 K 线序列
# =============================================================================

def build_bar_sequence(
    kline: pd.DataFrame, trade: Dict[str, object]
) -> Tuple[pd.DataFrame, List[str]]:
    """
    按 countdown 1~13 -> buy_date -> sell_date-1 交易日 -> sell_date 的顺序取 K 线。
    去重保序，标签里同时附上 "count_N"、"BUY"、"SELL-1"、"SELL" 角色。
    返回:
        bars_df: 含 open/high/low/close 与原始 date 的 DataFrame（按所选顺序）
        roles:   每根 K 对应的角色标签 list
    """
    count_dates: Dict[int, str] = trade["count_dates"]  # type: ignore[assignment]
    buy_date = pd.Timestamp(trade["buy_date"])  # type: ignore[arg-type]
    sell_date = pd.Timestamp(trade["sell_date"])  # type: ignore[arg-type]

    # 找 sell_date 前一个交易日（在原始 K 线里向前找）
    if sell_date not in kline.index:
        raise ValueError(f"sell_date {sell_date.date()} 不在 K 线索引中——数据可能缺这一天")
    sell_pos = kline.index.get_loc(sell_date)
    if sell_pos == 0:
        raise ValueError("sell_date 是 K 线第一天，无法取前一日")
    sell_prev_date = kline.index[sell_pos - 1]

    ordered: List[Tuple[pd.Timestamp, str]] = []
    seen: set = set()

    for n in sorted(count_dates.keys()):
        d = pd.Timestamp(count_dates[n])
        if d not in kline.index:
            print(f"[WARN] count_{n}={d.date()} 不在 K 线里，跳过")
            continue
        if d in seen:
            # count_1 经常与 setup_completed_at 同一天——保留先出现的标签
            continue
        ordered.append((d, f"count_{n}"))
        seen.add(d)

    for d, label in (
        (buy_date, "BUY"),
        (sell_prev_date, "SELL-1"),
        (sell_date, "SELL"),
    ):
        if d not in kline.index:
            raise ValueError(f"{label} 对应日期 {d.date()} 不在 K 线里")
        if d in seen:
            # 极端情况下 buy 日就是 count_13 之类（理论上不发生）
            for i, (dd, _) in enumerate(ordered):
                if dd == d:
                    ordered[i] = (dd, ordered[i][1] + "+" + label)
                    break
            continue
        ordered.append((d, label))
        seen.add(d)

    dates = [d for d, _ in ordered]
    roles = [r for _, r in ordered]
    bars_df = kline.loc[dates, ["open", "high", "low", "close"]].copy()
    bars_df.insert(0, "date", dates)
    bars_df.reset_index(drop=True, inplace=True)
    return bars_df, roles


# =============================================================================
# 画图
# =============================================================================

CANDLE_BODY_WIDTH = 0.6   # 蜡烛实体宽度（x 单位）
WICK_WIDTH = 0.08         # 上下影线宽度
LABEL_FONT_SIZE = 7


def _candle_color(open_: float, close_: float) -> Tuple[str, str]:
    """A 股习惯：红涨绿跌。返回 (实体填色, 边框色)。"""
    if close_ >= open_:
        return "#d62728", "#d62728"   # 红
    return "#2ca02c", "#2ca02c"       # 绿


def _draw_candle(ax, x: float, o: float, h: float, l: float, c: float):
    body_color, edge_color = _candle_color(o, c)
    # 上下影线
    ax.add_patch(Rectangle(
        (x - WICK_WIDTH / 2, l), WICK_WIDTH, h - l,
        facecolor=edge_color, edgecolor=edge_color, linewidth=0,
    ))
    # 实体
    body_low = min(o, c)
    body_height = max(abs(c - o), 1e-9)
    ax.add_patch(Rectangle(
        (x - CANDLE_BODY_WIDTH / 2, body_low), CANDLE_BODY_WIDTH, body_height,
        facecolor=body_color, edgecolor=edge_color, linewidth=1.0,
    ))


def _annotate_bar(ax, x: float, o: float, h: float, l: float, c: float, role: str):
    """每根 K 周围标注 H/L/O/C 与角色。"""
    pad = (h - l) * 0.08 if h > l else 0.02
    # H 在顶端上方，L 在底端下方
    ax.text(x, h + pad, f"H {h:.2f}", ha="center", va="bottom",
            fontsize=LABEL_FONT_SIZE, color="#333333")
    ax.text(x, l - pad, f"L {l:.2f}", ha="center", va="top",
            fontsize=LABEL_FONT_SIZE, color="#333333")
    # O 标在左侧、C 标在右侧
    ax.text(x - CANDLE_BODY_WIDTH / 2 - 0.05, o, f"O {o:.2f}",
            ha="right", va="center", fontsize=LABEL_FONT_SIZE, color="#555555")
    ax.text(x + CANDLE_BODY_WIDTH / 2 + 0.05, c, f"C {c:.2f}",
            ha="left", va="center", fontsize=LABEL_FONT_SIZE, color="#555555")
    # 角色（count_N / BUY / SELL / SELL-1）标在 K 顶部更高的位置
    role_color = {
        "BUY": "#0066cc", "SELL": "#cc6600", "SELL-1": "#888888",
    }.get(role.split("+")[0], "#222222")
    ax.text(x, h + pad * 3.5, role, ha="center", va="bottom",
            fontsize=LABEL_FONT_SIZE + 1, color=role_color,
            fontweight="bold")


def _draw_buy_marker(ax, x: float, price: float, trade: Dict[str, object]):
    ax.scatter([x], [price], marker="^", s=160, color="#0066cc",
               zorder=5, edgecolor="white", linewidths=1.0)
    qty = trade.get("buy_quantity", "")
    txt = f"BUY @ {price:.4f}\nqty={qty}"
    ax.annotate(
        txt, xy=(x, price), xytext=(x + 0.7, price - 0.15),
        fontsize=8, color="#0066cc", fontweight="bold",
        arrowprops=dict(arrowstyle="->", color="#0066cc", lw=1.0),
    )


def _draw_sell_marker(ax, x: float, price: float, trade: Dict[str, object]):
    ax.scatter([x], [price], marker="v", s=160, color="#cc6600",
               zorder=5, edgecolor="white", linewidths=1.0)
    reason = trade.get("sell_reason", "")
    pnl = trade.get("pnl", "")
    txt = f"SELL @ {price:.4f}\nreason={reason}\npnl={pnl}"
    ax.annotate(
        txt, xy=(x, price), xytext=(x - 1.6, price + 0.20),
        fontsize=8, color="#cc6600", fontweight="bold",
        arrowprops=dict(arrowstyle="->", color="#cc6600", lw=1.0),
    )


def _draw_price_level(ax, y: float, x_min: float, x_max: float,
                      color: str, label: str):
    ax.hlines(y=y, xmin=x_min, xmax=x_max, colors=color,
              linestyles="--", linewidth=1.1, alpha=0.85)
    ax.text(x_max + 0.1, y, f"{label} {y:.4f}",
            ha="left", va="center", fontsize=8.5, color=color, fontweight="bold")


def plot_trade(bars: pd.DataFrame, roles: List[str], trade: Dict[str, object],
               output_path: Path):
    """主画图函数。"""
    n = len(bars)
    if n == 0:
        raise ValueError("没有 K 线可画")

    fig_width = max(12.0, 1.05 * n)
    fig, ax = plt.subplots(figsize=(fig_width, 7.5), dpi=140)

    # 1) 画蜡烛
    for i in range(n):
        row = bars.iloc[i]
        _draw_candle(ax, x=i, o=row["open"], h=row["high"],
                     l=row["low"], c=row["close"])
        _annotate_bar(ax, x=i, o=row["open"], h=row["high"],
                      l=row["low"], c=row["close"], role=roles[i])

    # 2) 找 buy / sell K 的 x 坐标
    def find_role_x(role: str) -> Optional[int]:
        for i, r in enumerate(roles):
            if r == role or r.startswith(role + "+") or r.endswith("+" + role):
                return i
        return None

    buy_x = find_role_x("BUY")
    sell_x = find_role_x("SELL")

    # 3) 止损 / 止盈水平线（贯穿整张图）
    stop_loss = float(trade.get("stop_loss_price") or 0)
    take_profit = float(trade.get("take_profit_price") or 0)
    x_min, x_max = -0.5, n - 0.5
    if stop_loss > 0:
        _draw_price_level(ax, stop_loss, x_min, x_max,
                          color="#9c1818", label="STOP_LOSS")
    if take_profit > 0:
        _draw_price_level(ax, take_profit, x_min, x_max,
                          color="#127a12", label="TAKE_PROFIT")

    # 4) 买卖点箭头
    if buy_x is not None:
        _draw_buy_marker(ax, x=buy_x, price=float(trade["buy_price"]), trade=trade)  # type: ignore[arg-type]
    if sell_x is not None:
        _draw_sell_marker(ax, x=sell_x, price=float(trade["sell_price"]), trade=trade)  # type: ignore[arg-type]

    # 5) 在 count_13 与 SELL-1 之间画"时间裁剪"提示
    sell_prev_x = find_role_x("SELL-1")
    if buy_x is not None and sell_prev_x is not None and sell_prev_x > buy_x + 1:
        mid_x = (buy_x + sell_prev_x) / 2.0
        y_low = bars["low"].min()
        y_high = bars["high"].max()
        ax.axvspan(buy_x + 0.5, sell_prev_x - 0.5, color="#f7f7f7", alpha=0.6, zorder=0)
        gap_days = (pd.Timestamp(bars.iloc[sell_prev_x]["date"])
                    - pd.Timestamp(bars.iloc[buy_x]["date"])).days
        ax.text(mid_x, (y_low + y_high) / 2,
                f"... {gap_days} 日历日省略 ...",
                ha="center", va="center", fontsize=10, color="#888888",
                style="italic", alpha=0.7)

    # 6) x 轴标签 = "YYYY-MM-DD / role"
    xtick_labels = []
    for i in range(n):
        d = pd.Timestamp(bars.iloc[i]["date"]).strftime("%Y-%m-%d")
        xtick_labels.append(f"{d}\n{roles[i]}")
    ax.set_xticks(range(n))
    ax.set_xticklabels(xtick_labels, rotation=45, ha="right", fontsize=8)

    # 7) y 轴范围：把止损/止盈/全部 K 线都包进来 + 适度留白
    all_lows = [bars["low"].min(), stop_loss if stop_loss > 0 else float("inf")]
    all_highs = [bars["high"].max(), take_profit if take_profit > 0 else float("-inf")]
    y_low = min(v for v in all_lows if np.isfinite(v))
    y_high = max(v for v in all_highs if np.isfinite(v))
    y_pad = (y_high - y_low) * 0.18
    ax.set_ylim(y_low - y_pad, y_high + y_pad)
    ax.set_xlim(x_min - 0.5, x_max + 1.8)   # 右侧多留位置给止损/止盈文字

    # 8) 标题与说明
    sec = trade["security"]
    sell_d = trade["sell_date"]
    setup_perfect = "perfect" if trade.get("setup_is_perfect") else "normal"
    cd_perfect = "perfect" if trade.get("countdown_is_perfect") else "normal"
    title = (f"{sec}  TD9-13 trade replay  |  "
             f"setup={setup_perfect}  countdown={cd_perfect}  "
             f"setup@{trade.get('setup_completed_at')}  "
             f"sell@{sell_d}  reason={trade.get('sell_reason')}")
    ax.set_title(title, fontsize=11, fontweight="bold")
    ax.set_ylabel("Price (前复权)" if APPLY_PRE_ADJ else "Price (未复权)")
    ax.grid(axis="y", linestyle=":", alpha=0.45)
    ax.set_axisbelow(True)

    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path, bbox_inches="tight")
    plt.close(fig)
    print(f"[OK] 已生成图表: {output_path}")


# =============================================================================
# main
# =============================================================================

def diagnose_take_profit(bars: pd.DataFrame, roles: List[str],
                         trade: Dict[str, object]) -> None:
    """
    控制台输出"为什么 sell_reason=PROFIT_TARGET 但 sell_price < take_profit"
    的自动检查结论，对应策略 backtest.py: check_backtest_exits 的触发口径。
    """
    take_profit = float(trade.get("take_profit_price") or 0)
    if take_profit <= 0:
        return
    print()
    print("==== 止盈触发自检 ====")
    print(f"take_profit_price = {take_profit:.4f}")
    print(f"sell_price        = {float(trade['sell_price']):.4f}")
    print(f"sell_reason       = {trade['sell_reason']}")
    print(f"sell_price >= take_profit ? "
          f"{float(trade['sell_price']) >= take_profit}")

    # 触发判断：sell_date 前一根 K 线（策略里 bars[-1]）的 high
    try:
        sell_prev_idx = roles.index("SELL-1")
    except ValueError:
        return
    sell_prev_high = float(bars.iloc[sell_prev_idx]["high"])
    sell_high = float(bars.iloc[sell_prev_idx + 1]["high"]) if sell_prev_idx + 1 < len(bars) else float("nan")
    print(f"sell_date-1 K.high = {sell_prev_high:.4f} "
          f"(>= take_profit? {sell_prev_high >= take_profit})")
    print(f"sell_date    K.high = {sell_high:.4f} "
          f"(>= take_profit? {sell_high >= take_profit if not np.isnan(sell_high) else 'N/A'})")
    if sell_prev_high >= take_profit:
        print("→ 触发条件成立于「sell_date 前一交易日 K 线 high」；"
              "卖出在 sell_date 市价撮合，故 sell_price 与 take_profit 价位不绑定。")
    elif not np.isnan(sell_high) and sell_high >= take_profit:
        print("→ 触发条件成立于「sell_date 当日 K 线 high」；"
              "若你的策略实际触发时取的是 sell_date 而非前一交易日，请核对 handle_data 中 bars[-1] 的取值。")
    else:
        print("→ 警告：上一交易日与当天 K 线 high 都没到 take_profit。"
              "请检查：1) 除权回溯是否同步 2) take_profit 是否被异常 fill 价导致退化为反向止盈 "
              "（即 stop_loss > buy_price，让 take_profit 落到 buy_price 下方）。")


def _resolve_kline(cache: Dict[str, "pd.DataFrame"], security: str) -> "pd.DataFrame":
    """同标的多笔交易时共享同一份 K 线缓存，避免重复读 parquet + 复权计算。"""
    if security not in cache:
        raw = load_kline(security)
        adj = load_pre_adj_factors(security) if APPLY_PRE_ADJ else None
        cache[security] = apply_pre_adj(raw, adj)
    return cache[security]


def run_single(trade: Dict[str, object]) -> None:
    """单笔模式：保留原有的『打印 K 线明细 + 诊断 + 出图』完整输出。"""
    security = str(trade["security"])
    print(f"加载 K 线: {security}")
    cache: Dict[str, "pd.DataFrame"] = {}
    kline = _resolve_kline(cache, security)
    print(f"K 线区间: {kline.index.min().date()} ~ {kline.index.max().date()}  共 {len(kline)} 条")

    bars, roles = build_bar_sequence(kline, trade)

    pd.set_option("display.precision", 4)
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 160)
    print()
    print("==== 选中的 K 线 ====")
    listing = bars.copy()
    listing["role"] = roles
    print(listing.to_string(index=False))

    diagnose_take_profit(bars, roles, trade)

    output_dir = script_dir() / OUTPUT_DIR_NAME
    sell_d = str(trade["sell_date"]).replace(" ", "_").replace(":", "")[:10]
    output_path = output_dir / f"{security.replace('.', '')}_{sell_d}.png"
    plot_trade(bars, roles, trade, output_path)


def run_batch(csv_path: Path) -> None:
    """
    批量模式：读 trade.csv → 对每条已平仓行各画一张图。
    控制台只打印一行/笔的总结；同标的多笔共享 K 线缓存；单条失败不影响其他条。
    """
    import csv as csv_mod
    if not csv_path.exists():
        raise FileNotFoundError(f"找不到 trade.csv: {csv_path}")
    print(f"批量模式: {csv_path}")
    with csv_path.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv_mod.DictReader(f))
    print(f"trade.csv 行数: {len(rows)}")

    output_dir = script_dir() / OUTPUT_DIR_NAME
    cache: Dict[str, "pd.DataFrame"] = {}

    rendered = 0
    skipped_unclosed = 0
    errors: List[Tuple[str, str]] = []

    for idx, row in enumerate(rows, 1):
        trade = trade_dict_from_csv_row(row)
        if trade is None:
            skipped_unclosed += 1
            continue
        security = str(trade["security"])
        sell_d = str(trade["sell_date"])[:10]
        tag = f"{security} buy={trade['buy_date']} sell={sell_d} reason={trade['sell_reason']}"
        try:
            kline = _resolve_kline(cache, security)
            bars, roles = build_bar_sequence(kline, trade)
            output_path = output_dir / f"{security.replace('.', '')}_{sell_d}.png"
            plot_trade(bars, roles, trade, output_path)
            rendered += 1
            print(f"  [{rendered:02d}] {tag} -> {output_path.name}")
        except Exception as e:
            errors.append((tag, str(e)))
            print(f"  [ERR] {tag}: {e}")

    print()
    print(f"完成: rendered={rendered}, skipped(未平仓/未成交)={skipped_unclosed}, errors={len(errors)}")
    if errors:
        print("失败明细:")
        for tag, err in errors:
            print(f"  - {tag}  ::  {err}")
    print(f"图表目录: {output_dir}")


def main():
    csv_rel = (BATCH_TRADE_CSV or "").strip()
    if csv_rel:
        csv_path = Path(csv_rel)
        if not csv_path.is_absolute():
            csv_path = (script_dir() / csv_rel).resolve()
        run_batch(csv_path)
        return
    run_single(TARGET_TRADE)


if __name__ == "__main__":
    main()
