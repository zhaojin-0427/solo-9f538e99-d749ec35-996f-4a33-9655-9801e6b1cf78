"""需量核账核心算法。

设计要点（满足"不逐分钟铺开缺读段，也不逐 Wh 分配"）：
- 时间轴只按"相邻实读之间的段(segment)"和"15 分钟窗(window)"的交集切块(piece)。
  块数为 O(读数数 + 窗数)，与缺读段内有多少分钟、电量有多少 Wh 无关。
- 每块 x∈[L,H]，L=max(0, d-(n-1)U)，H=min(U,d)，都是整数；
  块的箱约束取整后，总和可行当且仅当 ΣL ≤ d ≤ ΣH（区间内每个整数都可达到，
  系数矩阵为区间/二部图结构，全幺模，整数解与 LP 解等价）。
- 用带下界的可行流（Dinic）判可行、二分求所报窗可达值；
- 用最小费用流（势 + Dijkstra 的 SSP，按路径批量增广）求最小越限方案，
  找出被迫超过 P 的其它窗。
"""
from __future__ import annotations

import sys
from collections import deque
from dataclasses import dataclass
from heapq import heappop, heappush
from typing import List, Optional, Sequence, Tuple

# 31 天最多 2976 个窗，Dinic 的 DFS 深度约为节点数级别，放开递归深度
sys.setrecursionlimit(50000)

WINDOW_MINUTES = 15
MAX_PERIOD_DAYS = 31
MAX_PERIOD_MINUTES = MAX_PERIOD_DAYS * 24 * 60


class SolverError(ValueError):
    """输入数据不满足建模前提。"""


# --------------------------------------------------------------------------- #
# 周期与切块
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Piece:
    seg_index: int
    window_index: int
    n: int          # 块内分钟数
    lower: int      # 该块电量下界（整 Wh）
    upper: int      # 该块电量上界（整 Wh）


def validate_period(readings: Sequence[Tuple[int, int]]) -> None:
    """校验账期：读数单调不回卷、整分钟、15 分钟对齐、最长 31 天。"""
    if len(readings) < 2:
        raise SolverError("至少需要两个读数才能确定账期")
    prev_t = None
    prev_v = None
    for t, v in readings:
        if t < 0:
            raise SolverError("时间戳非法")
        if prev_t is not None and t <= prev_t:
            raise SolverError("读数时间必须严格递增")
        if prev_v is not None and v < prev_v:
            raise SolverError(
                f"电表回卷冲突：{prev_t} 读数 {prev_v}，其后读数 {v}")
        prev_t, prev_v = t, v
    t0, t1 = readings[0][0], readings[-1][0]
    span = t1 - t0
    if span == 0:
        raise SolverError("首末读数不能在同一分钟")
    if span > MAX_PERIOD_MINUTES:
        raise SolverError(f"账期超过最长 {MAX_PERIOD_DAYS} 天")
    if t0 % WINDOW_MINUTES or t1 % WINDOW_MINUTES:
        raise SolverError("账期两端必须对齐 15 分钟边界")
    if span % WINDOW_MINUTES:
        raise SolverError("账期长度必须是 15 分钟的整数倍")


def build_pieces(readings: Sequence[Tuple[int, int]], upper: int,
                 num_windows: int) -> Tuple[List[Piece], List[int]]:
    """按 段×窗 交集切块。返回 (块列表, 每段固定电量 d)。"""
    pieces: List[Piece] = []
    segment_d: List[int] = []
    for s in range(len(readings) - 1):
        ta, va = readings[s]
        tb, vb = readings[s + 1]
        n = tb - ta
        d = vb - va
        if d < 0:
            raise SolverError(f"第 {s + 1} 段电表回卷")
        if d > n * upper:
            raise SolverError(
                f"第 {s + 1} 段（分钟偏移 {ta}..{tb}）用电 {d} Wh，"
                f"{n} 分钟最多 {n * upper} Wh，超过每分钟上限 {upper} Wh")
        segment_d.append(d)
        wa = ta // WINDOW_MINUTES
        wb = (tb - 1) // WINDOW_MINUTES
        for w in range(wa, wb + 1):
            ws = w * WINDOW_MINUTES
            lo = max(ta, ws)
            hi = min(tb, ws + WINDOW_MINUTES)
            k = hi - lo
            if k <= 0:
                continue
            lower = max(0, d - (n - k) * upper)
            cap = min(k * upper, d)
            pieces.append(Piece(s, w, k, lower, cap))
    if not pieces:
        raise SolverError("账期内没有可分配的时段")
    return pieces, segment_d


# --------------------------------------------------------------------------- #
# Dinic 最大流（整数容量）
# --------------------------------------------------------------------------- #
class _Edge:
    __slots__ = ("to", "rev", "cap")

    def __init__(self, to: int, rev: int, cap: int):
        self.to = to
        self.rev = rev
        self.cap = cap


class Dinic:
    def __init__(self, n: int):
        self.n = n
        self.g: List[List[_Edge]] = [[] for _ in range(n)]

    def add(self, u: int, v: int, cap: int) -> None:
        self.g[u].append(_Edge(v, len(self.g[v]), cap))
        self.g[v].append(_Edge(u, len(self.g[u]) - 1, 0))

    def max_flow(self, s: int, t: int, limit: Optional[int] = None) -> int:
        flow = 0
        n = self.n
        while True:
            level = [-1] * n
            level[s] = 0
            q = deque([s])
            while q:
                u = q.popleft()
                for e in self.g[u]:
                    if e.cap > 0 and level[e.to] < 0:
                        level[e.to] = level[u] + 1
                        q.append(e.to)
            if level[t] < 0:
                return flow
            it = [0] * n

            def dfs(u: int, pushed: int) -> int:
                if u == t:
                    return pushed
                while it[u] < len(self.g[u]):
                    e = self.g[u][it[u]]
                    if e.cap > 0 and level[e.to] == level[u] + 1:
                        r = dfs(e.to, min(pushed, e.cap))
                        if r:
                            e.cap -= r
                            self.g[e.to][e.rev].cap += r
                            return r
                    it[u] += 1
                return 0

            while True:
                if limit is not None and flow >= limit:
                    return flow
                pushed = dfs(s, limit - flow if limit is not None else 10**18)
                if not pushed:
                    break
                flow += pushed
        return flow


def _bounded_feasible(seg_count: int, win_count: int,
                      pieces: Sequence[Piece], segment_d: Sequence[int],
                      win_bounds: Sequence[Tuple[int, int]]) -> bool:
    """带下界二部流可行性。

    S->段 容量固定为 d（下界=上界=d）；块->窗 带 [L,H] 下界；窗->T 带界。
    """
    S_node = seg_count + win_count
    T_node = S_node + 1
    SS = T_node + 1
    TT = SS + 1
    n = TT + 1
    din = Dinic(n)
    balance = [0] * n

    def lower_edge(u: int, v: int, low: int, high: int) -> None:
        if high > low:
            din.add(u, v, high - low)
        balance[u] -= low
        balance[v] += low

    # S -> 段：固定 d
    for s, d in enumerate(segment_d):
        lower_edge(S_node, s, d, d)
    # 块：段 -> 窗
    for p in pieces:
        wn = seg_count + p.window_index
        lower_edge(p.seg_index, wn, p.lower, p.upper)
    # 窗 -> T
    for w, (lo, hi) in enumerate(win_bounds):
        lower_edge(seg_count + w, T_node, lo, hi)
    # T -> S 无限回流，转成循环流可行问题
    din.add(T_node, S_node, 10**18)

    need = 0
    for i, b in enumerate(balance):
        if b > 0:
            din.add(SS, i, b)
            need += b
        elif b < 0:
            din.add(i, TT, -b)
    return din.max_flow(SS, TT, need) == need


def window_extreme(readings: Sequence[Tuple[int, int]], upper: int,
                   num_windows: int, target_window: int,
                   pieces: Sequence[Piece], segment_d: Sequence[int],
                   find_max: bool) -> int:
    """求所报窗在满足全部实读前提下的最小/最大可达电量。"""
    total = segment_d and sum(segment_d)

    def feasible(lo: int, hi: int) -> bool:
        bounds = [(0, 10**9)] * num_windows
        bounds[target_window] = (lo, hi)
        return _bounded_feasible(len(readings) - 1, num_windows,
                                 pieces, segment_d, bounds)

    if find_max:
        lo, hi = 0, min(upper * WINDOW_MINUTES, total)
        best = 0
        while lo <= hi:
            mid = (lo + hi) // 2
            if feasible(mid, 10**9):
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return best
    lo, hi = 0, total
    best = total
    while lo <= hi:
        mid = (lo + hi) // 2
        if feasible(0, mid):
            best = mid
            hi = mid - 1
        else:
            lo = mid + 1
    return best


# --------------------------------------------------------------------------- #
# 最小费用流（势 + Dijkstra，批量增广）
# --------------------------------------------------------------------------- #
class _CEdge:
    __slots__ = ("to", "rev", "cap", "cost")

    def __init__(self, to: int, rev: int, cap: int, cost: int):
        self.to = to
        self.rev = rev
        self.cap = cap
        self.cost = cost


class MinCostFlow:
    def __init__(self, n: int):
        self.n = n
        self.g: List[List[_CEdge]] = [[] for _ in range(n)]

    def add(self, u: int, v: int, cap: int, cost: int):
        fe = _CEdge(v, len(self.g[v]), cap, cost)
        re = _CEdge(u, len(self.g[u]), 0, -cost)
        self.g[u].append(fe)
        self.g[v].append(re)
        return fe, cap

    def flow(self, s: int, t: int, want: int) -> Tuple[int, int]:
        n = self.n
        pot = [0] * n
        flow = cost = 0
        INF = 10**18
        while flow < want:
            dist = [INF] * n
            pv = [-1] * n
            pe = [-1] * n
            dist[s] = 0
            pq = [(0, s)]
            while pq:
                d, u = heappop(pq)
                if d != dist[u]:
                    continue
                for i, e in enumerate(self.g[u]):
                    if e.cap <= 0:
                        continue
                    nd = d + e.cost + pot[u] - pot[e.to]
                    if nd < dist[e.to]:
                        dist[e.to] = nd
                        pv[e.to], pe[e.to] = u, i
                        heappush(pq, (nd, e.to))
            if pv[t] < 0:
                break
            for i in range(n):
                if dist[i] < INF:
                    pot[i] += dist[i]
            add = want - flow
            v = t
            while v != s:
                add = min(add, self.g[pv[v]][pe[v]].cap)
                v = pv[v]
            v = t
            while v != s:
                e = self.g[pv[v]][pe[v]]
                e.cap -= add
                self.g[v][e.rev].cap += add
                v = pv[v]
            flow += add
            cost += add * pot[t]
        return flow, cost


def min_overflow_plan(readings: Sequence[Tuple[int, int]], upper: int,
                      num_windows: int, target_window: int, peak: int,
                      pieces: Sequence[Piece], segment_d: Sequence[int]
                      ) -> Tuple[int, List[int]]:
    """求"其它窗超出 P 总量最小"的方案，返回 (最小越限总量, 各窗电量)。

    每个其它窗 w 拆成 y_w∈[0,P]（费用 0）与 o_w≥0（费用 1），
    窗总量 = y_w + o_w；所报窗强制恰为 P（Case B 已证实可达）。
    带 [L,H] 下界的边用独立超源 SS / 超汇 TT 做标准下界转换。
    """
    seg_count = len(readings) - 1
    y_node = {w: seg_count + 2 * w for w in range(num_windows)}
    o_node = {w: seg_count + 2 * w + 1 for w in range(num_windows)}
    base = seg_count + 2 * num_windows
    S = base
    T = base + 1
    SS = base + 2
    TT = base + 3
    n = TT + 1
    mcf = MinCostFlow(n)
    balance = [0] * n

    def lower_edge(u: int, v: int, low: int, high: int, cost: int):
        if high > low:
            e = mcf.add(u, v, high - low, cost)
        else:
            e = None
        balance[u] -= low
        balance[v] += low
        return e

    # S -> 段：固定 d
    for s, d in enumerate(segment_d):
        lower_edge(S, s, d, d, 0)
    # 段 -> 窗入口：块 [L,H]，记录残量边以便事后统计实际流量
    piece_refs = []
    for p in pieces:
        resid_idx = lower_edge(p.seg_index, y_node[p.window_index],
                               p.lower, p.upper, 0)
        piece_refs.append((p.window_index, p.lower, resid_idx))
    # 窗出口
    for w in range(num_windows):
        if w == target_window:
            lower_edge(y_node[w], T, peak, peak, 0)       # 强制恰为 P
        else:
            mcf.add(y_node[w], T, peak, 0)                # 守限部分
            mcf.add(y_node[w], o_node[w], 10**9, 0)
            mcf.add(o_node[w], T, 10**9, 1)               # 越限部分 1/Wh
    # 回流边，把 s-t 流转为循环流
    mcf.add(T, S, 10**9, 0)
    # 下界平衡
    need = 0
    for i, b in enumerate(balance):
        if b > 0:
            mcf.add(SS, i, b, 0)
            need += b
        elif b < 0:
            mcf.add(i, TT, -b, 0)

    flow, cost = mcf.flow(SS, TT, need)
    if flow != need:
        raise SolverError("内部错误：最小越限流未找到可行方案")

    # 块实际流量 = 下界 + (原容量 - 残量)；按窗汇总即各窗总电量
    window_totals = [0] * num_windows
    for w, low, resid in piece_refs:
        used = low if resid is None else low + (resid[1] - resid[0].cap)
        window_totals[w] += used
    return cost, window_totals


# --------------------------------------------------------------------------- #
# 顶层核对
# --------------------------------------------------------------------------- #
@dataclass
class AuditResult:
    status: str                       # feasible | target_unreachable | others_breach
    target_window: int
    target_min: int
    target_max: int
    window_totals: List[int]          # 可行时：一组满足条件的各窗电量
    breach_windows: List[int]         # others_breach 时被迫越限的窗
    min_overflow: int
    message: str


def audit(readings: Sequence[Tuple[int, int]], upper: int, peak: int,
          target_window: int) -> AuditResult:
    """执行完整核对。readings 为 (分钟偏移, 累计 Wh)，偏移相对账期起点。"""
    validate_period(readings)
    if upper <= 0:
        raise SolverError("每分钟用电上限必须为正整数")
    if peak < 0:
        raise SolverError("账单峰值不能为负")
    span = readings[-1][0] - readings[0][0]
    num_windows = span // WINDOW_MINUTES
    if not (0 <= target_window < num_windows):
        raise SolverError("所报 15 分钟时段不在账期内")

    pieces, segment_d = build_pieces(readings, upper, num_windows)

    # Case A：所报窗本身能否达到 peak（且其余窗仅受每分钟上限约束）
    bounds = [(0, 10**9)] * num_windows
    bounds[target_window] = (peak, peak)
    if not _bounded_feasible(len(readings) - 1, num_windows,
                             pieces, segment_d, bounds):
        tmin = window_extreme(readings, upper, num_windows, target_window,
                              pieces, segment_d, False)
        tmax = window_extreme(readings, upper, num_windows, target_window,
                              pieces, segment_d, True)
        return AuditResult(
            status="target_unreachable",
            target_window=target_window,
            target_min=tmin, target_max=tmax,
            window_totals=[], breach_windows=[], min_overflow=0,
            message=(f"所报时段本身无法达到 {peak} Wh："
                     f"该时段可达区间为 [{tmin}, {tmax}] Wh"))

    # Case B：所报窗固定为 peak，其余窗 ≤ peak 是否同时成立
    bounds = [(0, peak)] * num_windows
    bounds[target_window] = (peak, peak)
    if _bounded_feasible(len(readings) - 1, num_windows,
                         pieces, segment_d, bounds):
        # 再跑一次最小越限（费用为 0 的解）拿到一组具体各窗电量
        _, totals = min_overflow_plan(readings, upper, num_windows,
                                      target_window, peak, pieces, segment_d)
        return AuditResult(
            status="feasible",
            target_window=target_window,
            target_min=peak, target_max=peak,
            window_totals=totals, breach_windows=[], min_overflow=0,
            message=("核对通过：存在整数分配使所报时段恰为 "
                     f"{peak} Wh，其余不重叠时段均不超过该值"))

    overflow, totals = min_overflow_plan(
        readings, upper, num_windows, target_window, peak,
        pieces, segment_d)
    breach = [w for w, v in enumerate(totals)
              if w != target_window and v > peak]
    return AuditResult(
        status="others_breach",
        target_window=target_window,
        target_min=peak, target_max=peak,
        window_totals=totals, breach_windows=breach,
        min_overflow=overflow,
        message=(f"所报时段可达到 {peak} Wh，但其余时段无法同时守住上限："
                 f"至少超量 {overflow} Wh，被迫越限窗索引 {breach}"))
