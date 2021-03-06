import abc
import logging
from typing import Union, Dict

import numpy as np
import pandas as pd
import matplotlib as mpl
import matplotlib.pyplot as plt
import seaborn as sns

from pyalloc.backtest.datasource import DataSource, HDFDataSource, DataFrameSource
from pyalloc.backtest.portfolio import Portfolio, Context
from pyalloc.backtest.report import BacktestReport

# 解决中文显示问题
mpl.rcParams['font.sans-serif'] = ['Microsoft YaHei']  # 指定默认字体
mpl.rcParams['axes.unicode_minus'] = False  # 解决保存图像是负号'-'显示为方块的问题
sns.set_style("whitegrid")

logger = logging.getLogger(__name__)


class StrategyEnvironment:
    """策略运行环境"""
    def __init__(self, sids: list,
                 start: Union[pd.Timestamp, str], end: Union[pd.Timestamp, str],
                 init_weight: Dict[str, float], init_cash_weight: float,
                 trading_cost_ratio=0.003,
                 benchmark=None, risk_free=None, hdfpath=None, out_df=None
                 ):

        self.benchmark = benchmark
        self.risk_free = risk_free

        start_ = pd.to_datetime(start)
        end_ = pd.to_datetime(end)
        self.start = start_
        self.end = end_

        if hdfpath is not None:
            self.__datasource = HDFDataSource(hdfpath, sids, start_, end_, benchmark, risk_free, align='intersect')
        elif out_df is not None:
            self.__datasource = DataFrameSource(out_df, sids, start_, end_, benchmark, risk_free, align='intersect')
        else:
            raise Exception("You should assign the path of hdf or out DataFrame!")


        self._init_weight = init_weight
        self._init_cash_weight = init_cash_weight
        self._trading_cost_ratio = trading_cost_ratio
        self._sids = self.datasource.sids

    @property
    def datasource(self) -> DataSource:
        return self.__datasource

    def __repr__(self):
        return ', '.join("%s:%s" % item for item in self.__dict__.items())

    def _add_rolling_indicator(self, indicator_name: str, source_name: str, window: int, func):
        if indicator_name in self.datasource.columns:
            raise Exception("需要创建的指标 {0} 已经存在".format(indicator_name))

        if source_name not in self.datasource.columns:
            raise Exception("源指标 {0} 不存在".format(source_name))

        for sid in self.datasource.sids:
            df = self.datasource[sid]
            # 直接修改，无需重新赋值
            df[indicator_name] = df[source_name].rolling(window).apply(func)


class Strategy(metaclass=abc.ABCMeta):
    """策略基类，同时也是回测的驱动"""

    def __init__(self, env: StrategyEnvironment, name: str):
        self._name = name
        self._env = env
        self._portfolio = Portfolio(
            env._init_weight.copy(), env._init_cash_weight, env._trading_cost_ratio
        )

        # 回测参数
        self._cursor = 0  # 游标，用于控制行情遍历的位置，获取历史行情
        self._datasource = self._env.datasource
        self._sids = self._datasource.sids
        self._records = []

        # 回测结果重组
        self._s_ret = None
        self._weight = None
        self._turnover = None
        self._cost = None
        self._nv = None
        self._rebalance_weight = None

        # 回测结果分析
        self._report = None
        self._xary = None

    @property
    def current_portfolio_info(self):
        """返回当前时刻的投资组合信息"""
        return self._portfolio.get_portfolio_info()

    def _get_hist(self, length: int) -> dict:
        """获取当前时刻指定长度的历史数据"""
        return self._env.datasource.get_hist(self._cursor, length)

    @abc.abstractmethod
    def _on_data(self):
        """抽象方法，实现全部配置逻辑"""
        raise NotImplementedError('method not defined!')

    def _on_quotes(self, quotes: dict):
        """单次行情触发"""

        # 当前行情
        Context.cur_quotes = quotes
        # 当前时间
        Context.cur_time = quotes['time']

        # 当前游标
        Context.cursor = self._cursor

        # print('***单次行情触发', Context.cur_time.strftime('%Y-%m-%d'))

        # 执行策略
        self._on_data()

        # 记录时间
        Context.pre_quotes = quotes
        # 增加记录
        # print('增加记录', self._portfolio.current_portfolio_info())

        self._records.append(self._portfolio.get_portfolio_info())

        # print('当前记录', self._records)

    def run(self, report=True) -> None:
        """回放行情进行回测"""

        # 控制一个策略只能回测一次
        assert self._cursor == 0, "策略已经运行完毕，不能再次运行"

        # 重设全局变量中权重的记录
        Context.rebalance = []

        # 遍历所有的行情
        for quotes in self._env.datasource:
            try:
                self._on_quotes(quotes)
            except Exception as e:
                print('Exception time: ', Context.cur_time)
                raise e

            self._cursor += 1

        # 重新组合weight
        self._weight = pd.DataFrame({
            record.time: pd.Series(
                [item[1] for item in record.asset_weight],
                index=[item[0] for item in record.asset_weight]
                                     ) for record in self._records}).T
        self._weight['cash'] = pd.Series([record.cash_weight for record in self._records],
                                         index=[record.time for record in self._records])

        # 重新组合每日收益
        self._s_ret = pd.Series(
            [record.pct_change for record in self._records],
            index=[record.time for record in self._records]).dropna()

        # 从每日收益计算策略净值曲线
        self._nv = (self._s_ret + 1).cumprod()
        self._nv = add_value_on_first(self._nv, 1, '1D').dropna()

        # 重新组合每次换手
        self._turnover = pd.Series(
            [record.turnover for record in self._records],
            index=[record.time for record in self._records]
        ).dropna()

        # 重新组合每次交易成本
        self._cost = pd.Series(
            [record.cost for record in self._records],
            index=[record.time for record in self._records]
        ).dropna()

        # 重新组合调仓权重
        self._rebalance_weight = pd.DataFrame(
            {item[0]: pd.Series(item[1]) for item in Context.rebalance}
        ).T
        self._rebalance_weight['cash'] = self._rebalance_weight.sum(axis=1) - 1

        if report:
            self.report()

    def report(self, plot_nv=True, figure_size=(12, 6)):
        # 回测的数据分析

        assert self._cursor != 0, "策略尚未回测，请先进行run方法!"

        if self._env.benchmark is not None:
            bench_ret = self._datasource.data[self._env.benchmark]['pct_change']
            bench_nv = (bench_ret+1).cumprod()
            bench_nv = add_value_on_first(bench_nv, 1, '1D').fillna(method='ffill')
        else:
            bench_nv = None

        if self._env.risk_free is not None:
            ts_rf = self._datasource.data[self._env.risk_free]['pct_change']
            ts_rf = add_value_on_first(ts_rf, 0, '1D').fillna(0)
        else:
            ts_rf = None

        # print(len(ts_rf),ts_rf.count())
        self._report = BacktestReport(self._nv, bench_nv, ts_rf, true_beta=False)

        if plot_nv:
            self._nv.plot(label=self._name)
            if self._env.benchmark is not None:
                bench_nv.plot(label='Benchmark', figsize=figure_size)

            sns.plt.title('Net Value')
            sns.plt.legend()
            sns.plt.show()

        self._report.output()

    @property
    def weight(self):
        assert self._cursor != 0, "策略尚未回测，请先进行run方法!"
        return self._weight

    @property
    def turnover(self):
        assert self._cursor != 0, "策略尚未回测，请先进行run方法!"
        return self._turnover

    @property
    def pct_change(self):
        assert self._cursor != 0, "策略尚未回测，请先进行run方法!"
        return self._s_ret

    @property
    def cost(self):
        assert self._cursor != 0, "策略尚未回测，请先进行run方法!"
        return self._cost

    @property
    def net_value(self):
        assert self._cursor != 0, "策略尚未回测，请先进行run方法!"
        return self._nv

    @property
    def daily_return(self):
        assert self._cursor != 0, "策略尚未回测，请先进行run方法!"
        return self._s_ret

    @property
    def name(self):
        assert self._cursor != 0, "策略尚未回测，请先进行run方法!"
        return self._name


def add_value_on_first(s: pd.Series, value: float, time_delta='1D') -> pd.Series:
    """为了解决nv和ret在转换时候的不匹配问题"""
    time = s.index[0] - pd.Timedelta(time_delta)
    res = s.copy()
    res[time] = value
    res = res.sort_index()
    return res
