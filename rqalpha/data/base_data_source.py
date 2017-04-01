# -*- coding: utf-8 -*-
#
# Copyright 2017 Ricequant, Inc
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import os

import six
import numpy as np

from ..utils.py2 import lru_cache
from ..utils.datetime_func import convert_date_to_int, convert_int_to_date
from ..interface import AbstractDataSource
from .future_info_cn import CN_FUTURE_INFO
from .converter import StockBarConverter, IndexBarConverter
from .converter import FutureDayBarConverter, FundDayBarConverter
from .daybar_store import DayBarStore
from .date_set import DateSet
from .dividend_store import DividendStore
from .instrument_store import InstrumentStore
from .trading_dates_store import TradingDatesStore
from .yield_curve_store import YieldCurveStore
from .simple_factor_store import SimpleFactorStore


class BaseDataSource(AbstractDataSource):
    def __init__(self, path):
        def _p(name):
            return os.path.join(path, name)

        self._day_bars = [
            DayBarStore(_p('stocks.bcolz'), StockBarConverter),
            DayBarStore(_p('indexes.bcolz'), IndexBarConverter),
            DayBarStore(_p('futures.bcolz'), FutureDayBarConverter),
            DayBarStore(_p('funds.bcolz'), FundDayBarConverter),
        ]

        self._instruments = InstrumentStore(_p('instruments.pk'))
        self._dividends = DividendStore(_p('original_dividends.bcolz'))
        self._trading_dates = TradingDatesStore(_p('trading_dates.bcolz'))
        self._yield_curve = YieldCurveStore(_p('yield_curve.bcolz'))
        self._split_factor = SimpleFactorStore(_p('split_factor.bcolz'))
        self._ex_cum_factor = SimpleFactorStore(_p('ex_cum_factor.bcolz'))

        self._st_stock_days = DateSet(_p('st_stock_days.bcolz'))
        self._suspend_days = DateSet(_p('suspended_days.bcolz'))

        self.get_yield_curve = self._yield_curve.get_yield_curve
        self.get_risk_free_rate = self._yield_curve.get_risk_free_rate

    def get_dividend(self, order_book_id):
        return self._dividends.get_dividend(order_book_id)

    def get_trading_minutes_for(self, order_book_id, trading_dt):
        raise NotImplementedError

    def get_trading_calendar(self):
        return self._trading_dates.get_trading_calendar()

    def get_all_instruments(self):
        return self._instruments.get_all_instruments()

    def is_suspended(self, order_book_id, dt):
        return self._suspend_days.contains(order_book_id, dt)

    def is_st_stock(self, order_book_id, dt):
        return self._st_stock_days.contains(order_book_id, dt)

    INSTRUMENT_TYPE_MAP = {
        'CS': 0,
        'INDX': 1,
        'Future': 2,
        'ETF': 3,
        'LOF': 3,
        'FenjiA': 3,
        'FenjiB': 3,
        'FenjiMu': 3,
    }

    def _index_of(self, instrument):
        return self.INSTRUMENT_TYPE_MAP[instrument.type]

    @lru_cache(None)
    def _all_day_bars_of(self, instrument):
        i = self._index_of(instrument)
        return self._day_bars[i].get_bars(instrument.order_book_id, fields=None)

    @lru_cache(None)
    def _filtered_day_bars(self, instrument):
        bars = self._all_day_bars_of(instrument)
        if bars is None:
            return None
        return bars[bars['volume'] > 0]

    def get_bar(self, instrument, dt, frequency):
        if frequency != '1d':
            raise NotImplementedError

        bars = self._all_day_bars_of(instrument)
        if bars is None:
            return
        dt = np.uint64(convert_date_to_int(dt))
        pos = bars['datetime'].searchsorted(dt)
        if pos >= len(bars) or bars['datetime'][pos] != dt:
            return None

        return bars[pos]

    def get_settle_price(self, instrument, date):
        bar = self.get_bar(instrument, date, '1d')
        if bar is None:
            return np.nan
        return bar['settlement']

    @staticmethod
    def _are_fields_valid(fields, valid_fields):
        if fields is None:
            return True
        if isinstance(fields, six.string_types):
            return fields in valid_fields
        for field in fields:
            if field not in valid_fields:
                return False
        return True

    @staticmethod
    def _factor_for_date(dates, factors, d):
        if d > dates[-1]:
            return factors[-1]
        pos = dates.searchsorted(d, side='right')
        return factors[pos-1]

    PRICE_FIELDS = {
        'open', 'close', 'high', 'low', 'limit_up', 'limit_down', 'acc_net_value', 'unit_net_value'
    }

    FIELDS_REQUIRE_ADJUSTMENT = PRICE_FIELDS.copy().add('volume')

    def _adjust_bar(self, order_book_id, bars, fields):
        ex_factors = self._ex_cum_factor.get_factors(order_book_id)
        if ex_factors is None:
            return bars if fields is None else bars[fields]

        start_date = bars['datetime'][0]
        end_date = bars['datetime'][-1]

        dates = ex_factors['start_date']
        ex_cum_factors = ex_factors['ex_cum_factor']

        if (self._factor_for_date(dates, ex_cum_factors, start_date) ==
                self._factor_for_date(dates, ex_cum_factors, end_date)):
            return bars if fields is None else bars[fields]

        factors = np.array([self._factor_for_date(dates, ex_cum_factors, d) for d in bars['datetime']],
                           dtype=np.float64)

        # 前复权
        factors /= factors[-1]
        if isinstance(fields, str):
            if fields in self.PRICE_FIELDS:
                return bars[fields] * factors
            elif fields == 'volume':
                return bars[fields] / factors
            # should not got here
            return bars[fields]

        result = np.copy(bars if fields is None else bars[fields])
        for f in result.dtype.names:
            if f in self.PRICE_FIELDS:
                result[f] *= factors
            elif f == 'volume':
                result[f] /= factors
        return result

    def history_bars(self, instrument, bar_count, frequency, fields, dt, skip_suspended=True):
        if frequency != '1d':
            raise NotImplementedError

        if skip_suspended and instrument.type == 'CS':
            bars = self._filtered_day_bars(instrument)
        else:
            bars = self._all_day_bars_of(instrument)

        if bars is None or not self._are_fields_valid(fields, bars.dtype.names):
            return None

        dt = np.uint64(convert_date_to_int(dt))
        i = bars['datetime'].searchsorted(dt, side='right')
        left = i - bar_count if i >= bar_count else 0
        bars = bars[left:i]
        if instrument.type in {'Future', 'INDX'} or len(bars) == 1:
            # 期货及指数无需复权
            return bars if fields is None else bars[fields]

        if isinstance(fields, str) and fields not in self.FIELDS_REQUIRE_ADJUSTMENT:
            return bars if fields is None else bars[fields]

        return self._adjust_bar(instrument.order_book_id, bars, fields)

    def get_yield_curve(self, start_date, end_date, tenor=None):
        return self._yield_curve.get_yield_curve(start_date, end_date, tenor)

    def get_risk_free_rate(self, start_date, end_date):
        return self._yield_curve.get_risk_free_rate(start_date, end_date)

    def current_snapshot(self, instrument, frequency, dt):
        raise NotImplementedError

    def get_split(self, order_book_id):
        return self._split_factor.get_factors(order_book_id)

    def available_data_range(self, frequency):
        if frequency == '1d':
            s, e = self._day_bars[self.INSTRUMENT_TYPE_MAP['INDX']].get_date_range('000001.XSHG')
            return convert_int_to_date(s).date(), convert_int_to_date(e).date()

        raise NotImplementedError

    def get_future_info(self, instrument, hedge_type):
        return CN_FUTURE_INFO[instrument.underlying_symbol][hedge_type.value]
