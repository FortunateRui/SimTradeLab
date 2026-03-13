"""
文件名：BarsDataManager.py
功能：管理股票的K线数据类，用于存储股票最新的 n 根 K 线数据，并提供相关方法
作者：熊瑞

实现说明：
- 使用「环形数组 + 哈希表」的组合结构实现ring buffer数据结构
- 环形数组保存最近的固定数量 Bar，按照时间顺序覆盖旧数据。
- 哈希表将 datetime 映射到环形数组下标，提供 O(1) 级别按时间查询能力。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class Bar:
    """单根 K 线数据。"""

    security: str          # 股票代码
    frequency: str         # 数据频率，例如 "1m" / "1d"
    datetime: str          # 日期时间（字符串即可，由上层保证有序性）

    open: float            # 开盘价
    high: float            # 最高价
    low: float             # 最低价
    close: float           # 收盘价
    volume: float = 0.0    # 成交量


class BarsDataManager:
    """
    管理单个标的的一段时间内的 K 线数据。

    核心结构：
    - `_buffer`: 固定长度环形数组，保存最近的 N 根 Bar。
    - `_index_by_datetime`: datetime -> buffer 下标，便于按时间快速查找。

    约定：
    - 假定外部以时间顺序追加 Bar（不做严格校验）。
    - 同一 datetime 的 Bar 再次写入视为更新，而不是新增。
    """

    def __init__(self, security: str, frequency: str, capacity: int = 300):
        """
        :param capacity: 环形数组容量，即最多保存的 K 线数量（>0），默认300根
        :param security: 股票代码
        :param frequency: 频率标识
        """
        if capacity <= 0:
            raise ValueError("capacity 必须为正整数")

        self._capacity: int = capacity
        self.security: str = security
        self.frequency: str = frequency

        # 环形数组
        self._buffer: List[Optional[Bar]] = [None] * capacity
        # 下一次写入的位置
        self._head: int = 0
        # 当前实际存储的 Bar 数量，最大为 capacity
        self._size: int = 0
        # datetime -> buffer 下标
        self._index_by_datetime: Dict[str, int] = {}

    # ------------------------- 基础属性 ------------------------- #
    @property
    def capacity(self) -> int:
        return self._capacity

    def __len__(self) -> int:
        """当前已保存的 Bar 数量。"""
        return self._size

    def is_full(self) -> bool:
        """环形数组是否已满。"""
        return self._size == self._capacity

    # ------------------------- 写入逻辑 ------------------------- #
    def add_bar(self, bar: Bar) -> None:
        """
        追加或更新一根 Bar。

        规则：
        - 若 datetime 已存在，则在原位置直接覆盖（不移动 head / size）。
        - 若 datetime 不存在，则在 head 位置写入：
          - 若该位置已有旧 Bar，则从哈希表中移除它的 datetime。
          - 写入新 Bar 后，更新哈希表索引，移动 head，并在未满时增加 size。
        """
        if bar.security != self.security or bar.frequency != self.frequency:
            # 上层通常一次只管理一个标的，一个频率；这里做基本防御性检查
            raise ValueError("Bar 的 security / frequency 与 BarsDataManager 不一致")

        dt = bar.datetime
        if dt in self._index_by_datetime:
            # 更新已存在的 Bar
            idx = self._index_by_datetime[dt]
            self._buffer[idx] = bar
            return

        # 写入到 head 位置（可能覆盖旧 Bar）
        overwrite_idx = self._head
        old_bar = self._buffer[overwrite_idx]
        if old_bar is not None:
            # 删除旧 datetime 的索引
            old_dt = old_bar.datetime
            # 安全删除：只在映射到相同下标时删除，避免极端情况下的误删
            if self._index_by_datetime.get(old_dt) == overwrite_idx:
                self._index_by_datetime.pop(old_dt, None)

        # 写入新 Bar
        self._buffer[overwrite_idx] = bar
        self._index_by_datetime[dt] = overwrite_idx

        # 更新 head / size
        self._head = (self._head + 1) % self._capacity
        if self._size < self._capacity:
            self._size += 1

    # ------------------------- 查询逻辑 ------------------------- #
    def get_latest(self) -> Optional[Bar]:
        """获取最新一根 Bar（若尚无数据则返回 None）。"""
        if self._size == 0:
            return None
        # 最新一根在 head 前一位
        idx = (self._head - 1) % self._capacity
        return self._buffer[idx]

    def get_last_n(self, n: int) -> List[Bar]:
        """
        获取最近 n 根 Bar（按时间从旧到新排序）。
        若 n 大于当前已有数量，则返回全部。
        """
        if n <= 0 or self._size == 0:
            return []
        n = min(n, self._size)

        result: List[Bar] = []
        # 最新 idx
        latest_idx = (self._head - 1) % self._capacity
        # 起始偏移（从最新往前数 n-1 根）
        start_offset = n - 1
        for offset in range(start_offset, -1, -1):
            idx = (latest_idx - offset) % self._capacity
            bar = self._buffer[idx]
            if bar is not None:
                result.append(bar)
        return result

    def get_by_offset(self, offset: int) -> Optional[Bar]:
        """
        按相对偏移获取 Bar。

        :param offset: 0 表示最新一根，1 表示前一根，以此类推。
        :return: 对应的 Bar 或 None（越界时）
        """
        if offset < 0 or offset >= self._size:
            return None
        latest_idx = (self._head - 1) % self._capacity
        idx = (latest_idx - offset) % self._capacity
        return self._buffer[idx]

    def get_by_datetime(self, dt: str) -> Optional[Bar]:
        """按 datetime 查找对应的 Bar，若不存在则返回 None。"""
        idx = self._index_by_datetime.get(dt)
        if idx is None:
            return None
        return self._buffer[idx]

    def all_bars(self) -> List[Bar]:
        """
        返回当前所有 Bar，按时间从旧到新排序。
        """
        return self.get_last_n(self._size)
