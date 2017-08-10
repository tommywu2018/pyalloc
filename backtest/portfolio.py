import abc
import logging
import copy

from collections import namedtuple

from pyalloc.backtest.context import Context

# 用于记录当期损益和权重
PInfo = namedtuple("PInfo", ["time", "pct_change", "turnover", "cost", "cash_weight", "asset_weight"])

class Portfolio:

    def __init__(self, init_weight: dict, init_cash_weight: float, trading_cost_ratio: float):
        # 当前剩余资金
        self._cash_weight = copy.deepcopy(init_cash_weight)

        self._pct_change = .0
        self._cost = .0
        self._turnover = .0
        self._trading_cost_ratio = trading_cost_ratio

        # 复制初始权重，使得TradingEnvironment里的初始权重不变
        self._weight = copy.deepcopy(init_weight)

    def __getitem__(self, sid):
        """
        返回证券 sid 的当前最新weight
        """
        if sid not in self._weight.keys():
            # self._weight[sid] = .0    # 如果 sid 不存在，则创建默认的Weight
            raise Exception('{0}不在交易标的中'.format(sid))
        return self._weight[sid]

    def update_by_price(self):
        """根据行情调整组合权重"""

        # print("******using update_by_price")

        total_asset_weight = .0
        self._pct_change = .0
        self._cost = .0
        self._turnover = .0

        # 计算收益
        for sid in self._weight.keys():
            # 当期收益
            self._pct_change += self._weight[sid] * Context.cur_quotes[sid].pct_change
            # 权重因为收益的变化
            self._weight[sid] = self._weight[sid] * (1 + Context.cur_quotes[sid].pct_change)
            # 总权重
            total_asset_weight += self._weight[sid]

        total_asset_weight += self._cash_weight

        # 重新计算权重
        for sid in self._weight.keys():
            self._weight[sid] = self._weight[sid] / total_asset_weight

        # 更新现金权重
        self._cash_weight = self._cash_weight / total_asset_weight

    def update_by_rebalance(self, target_weight: dict):
        """根据目标权重调整组合权重，开盘调仓"""

        # print("******using update_by_rebalance")
        assert self._weight.keys() == target_weight.keys(), "调仓标的与现有标的不符合！"

        total_asset_weight = .0
        self._pct_change = .0
        self._cost = .0
        self._turnover = .0

        for sid in self._weight.keys():
            # 计算换手比例
            self._turnover += abs(self._weight[sid] - target_weight[sid])
            # 开盘更新权重, 不支持资产卖空
            self._weight[sid] = target_weight[sid] - self._turnover * self._trading_cost_ratio
            # 当期收益
            self._pct_change += self._weight[sid] * Context.cur_quotes[sid].pct_change
            # 权重因为收益的变化
            self._weight[sid]= self._weight[sid] * (1 + Context.cur_quotes[sid].pct_change)
            # 总权重
            total_asset_weight += self._weight[sid]

        # 交易成本
        self._cost = self._turnover * self._trading_cost_ratio

        total_asset_weight += self._cash_weight
        # 重新计算权重
        for sid in self._weight.keys():
            self._weight[sid] = self._weight[sid] / total_asset_weight

        # 更新现金权重
        self._cash_weight = self._cash_weight / total_asset_weight


    def __repr__(self):
        return "PInfo({0})".format({
                                           k: v
                                           for k, v in self.__dict__.items()
                                           # if not k.startswith("_")
                                           })

    def get_portfolio_info(self) -> PInfo:
        """获得组合当期的收益率、交易损耗、资产权重以及cash"""
        return PInfo(Context.cur_time, self._pct_change, self._turnover, self._cost,
                     self._cash_weight, tuple(self._weight.items()))

    @property
    def asset_weight(self):
        return dict(tuple(self._weight.items()))