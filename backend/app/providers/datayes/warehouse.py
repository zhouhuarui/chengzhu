"""对 Datayes Parquet 仓库的只读 DuckDB 适配器。"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .errors import WarehouseUnavailableError
from .manifest import EndpointSpec, get_endpoint
from .normalization import normalize_rows, to_snake


DATE_COLUMNS = {
    'getTradeCal': 'calendar_date',
    'getMktEqud': 'trade_date',
    'getMktAdjfAf': 'ex_div_date',
    'getFdmtIS': 'end_date',
    'getFdmtBS': 'end_date',
    'getFdmtCF': 'end_date',
}

PARAM_COLUMNS = {
    'secID': 'sec_id', 'ticker': 'ticker', 'partyID': 'party_id',
    'exchangeCD': 'exchange_cd', 'assetClass': 'asset_class',
    'listStatusCD': 'list_status_cd', 'secShortName': 'sec_short_name',
    'tradeDate': 'trade_date', 'exDivDate': 'ex_div_date',
    'reportType': 'report_type', 'fiscalPeriod': 'fiscal_period',
    'publishDateBegin': 'publish_date', 'publishDateEnd': 'publish_date',
    'updateTimeBegin': 'update_time', 'updateTimeEnd': 'update_time',
    'beginDateRep': 'end_date_rep', 'endDateRep': 'end_date_rep',
    'isOpen': 'is_open',
}


def _parse_date(value: Any) -> Optional[date]:
    if value is None or value == '':
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()[:10]
    if len(text) == 8 and text.isdigit():
        text = f'{text[:4]}-{text[4:6]}-{text[6:]}'
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _parse_datetime_value(value: Any, *, end_of_day: bool = False) -> Any:
    if isinstance(value, datetime):
        return value.isoformat(sep=' ', timespec='seconds')
    if isinstance(value, date):
        suffix = '23:59:59' if end_of_day else '00:00:00'
        return f'{value.isoformat()} {suffix}'
    text = str(value or '').strip()
    digits = ''.join(ch for ch in text if ch.isdigit())
    if len(digits) >= 14:
        return (
            f'{digits[:4]}-{digits[4:6]}-{digits[6:8]} '
            f'{digits[8:10]}:{digits[10:12]}:{digits[12:14]}'
        )
    parsed = _parse_date(value)
    if parsed:
        suffix = '23:59:59' if end_of_day else '00:00:00'
        return f'{parsed.isoformat()} {suffix}'
    return value


class DatayesWarehouse:
    def __init__(self, data_dir: str, connection_factory: Optional[Any] = None):
        self.data_dir = os.path.abspath(os.path.expanduser(data_dir or '')) if data_dir else ''
        self._connection_factory = connection_factory

    def _connect(self):
        if self._connection_factory:
            return self._connection_factory()
        try:
            import duckdb
        except ImportError as exc:
            raise WarehouseUnavailableError('duckdb 未安装') from exc
        # 仅在内存中建立连接，所有外部 Parquet 只执行 SELECT。
        return duckdb.connect(database=':memory:')

    def _json_file(self, name: str) -> Dict[str, Any]:
        path = os.path.join(self.data_dir, name)
        if not self.data_dir or not os.path.isfile(path):
            return {}
        try:
            with open(path, 'r', encoding='utf-8') as f:
                value = json.load(f)
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    @property
    def status(self) -> Dict[str, Any]:
        return self._json_file('_status.json')

    @property
    def watermarks(self) -> Dict[str, Any]:
        return self._json_file('_watermark.json')

    def watermark(self, api: str) -> Optional[str]:
        spec = get_endpoint(api)
        if not spec.table:
            return None
        status = self.status.get(spec.table) or {}
        expected_field = spec.warehouse_watermark_field
        # 旧财务仓库的水位基于 publish_date，不能误当 update_time 用于增量。
        if expected_field and status.get('date_col') != expected_field:
            return None
        value = self.watermarks.get(spec.table)
        if value is None:
            value = status.get('data_end')
        return str(value) if value not in (None, '') else None

    def parquet_glob(self, api: str) -> Optional[str]:
        spec = get_endpoint(api)
        if not spec.table or not self.data_dir:
            return None
        folder = os.path.join(self.data_dir, spec.table)
        if not os.path.isdir(folder):
            return None
        # 不用 glob 模块展开百万级文件；DuckDB 只读扫描审核表目录。
        return os.path.join(folder, '**', '*.parquet')

    def available(self, api: str) -> bool:
        path = self.parquet_glob(api)
        if not path:
            return False
        folder = os.path.dirname(os.path.dirname(path))
        for _, _, files in os.walk(folder):
            if any(name.endswith('.parquet') for name in files):
                return True
        return False

    def latest_open_trading_day(self, today: Optional[date] = None) -> Optional[date]:
        if not self.available('getTradeCal'):
            return None
        today = today or date.today()
        calendar_watermark = _parse_date(self.watermark('getTradeCal'))
        # 只有日历覆盖查询日（周末也需被覆盖）时，才能证明最近交易日。
        if not calendar_watermark or calendar_watermark < today:
            return None
        rows = self.query(
            'getTradeCal',
            {'exchangeCD': 'XSHG', 'endDate': today.isoformat(), 'isOpen': 1},
            limit=1,
        )
        value = rows[0].get('calendar_date') if rows else None
        return _parse_date(value)

    def covers(self, api: str, end_date: Any) -> bool:
        end = _parse_date(end_date)
        wm = _parse_date(self.watermark(api))
        return bool(self.available(api) and end and wm and end <= wm)

    def fresh(self, api: str, *, latest: bool = False, now: Optional[datetime] = None) -> bool:
        spec = get_endpoint(api)
        if not self.available(api):
            return False
        if api == 'getTradeCal':
            return True
        if latest and api == 'getMktEqud':
            expected = self.latest_open_trading_day((now or datetime.now()).date())
            wm = _parse_date(self.watermark(api))
            return bool(expected and wm and wm >= expected)
        status = self.status.get(spec.table or '') or {}
        if (
            spec.warehouse_watermark_field
            and status.get('date_col') != spec.warehouse_watermark_field
        ):
            return False
        if spec.warehouse_watermark_field and not self.watermark(api):
            return False
        last_run = status.get('last_run')
        if not last_run:
            return False
        try:
            stamp = datetime.fromisoformat(str(last_run).replace('T', ' '))
        except ValueError:
            return False
        return (now or datetime.now()) - stamp <= timedelta(hours=spec.freshness_hours)

    def _where(
        self,
        spec: EndpointSpec,
        params: Mapping[str, Any],
        columns: set,
    ) -> Tuple[List[str], List[Any]]:
        clauses: List[str] = []
        values: List[Any] = []
        default_date_col = DATE_COLUMNS.get(spec.api)
        for key, raw in params.items():
            if raw is None or raw == '':
                continue
            if spec.api == 'getMktAdjfAf' and key == 'beginDate':
                # 后复权因子是有效期稀疏表：区间开始前的因子仍可能持续生效。
                if 'end_date' in columns:
                    clauses.append('(end_date IS NULL OR end_date >= ?)')
                    parsed = _parse_date(raw)
                    values.append(parsed.isoformat() if parsed else raw)
                continue
            if spec.api == 'getMktAdjfAf' and key == 'endDate':
                col, op = 'ex_div_date', '<='
            elif key == 'beginDate' and default_date_col:
                col, op = default_date_col, '>='
            elif key == 'endDate' and default_date_col:
                col, op = default_date_col, '<='
            elif key.endswith('Begin') and key in PARAM_COLUMNS:
                col, op = PARAM_COLUMNS[key], '>='
            elif key.endswith('End') and key in PARAM_COLUMNS:
                col, op = PARAM_COLUMNS[key], '<='
            else:
                col, op = PARAM_COLUMNS.get(key, to_snake(key)), '='
            if col not in columns:
                continue
            items = list(raw) if isinstance(raw, (list, tuple, set)) else str(raw).split(',')
            items = [item for item in items if str(item) != '']
            dtype = str(spec.input_types.get(key) or '').strip().lower()
            if dtype == 'datetime':
                items = [
                    _parse_datetime_value(item, end_of_day=key.endswith('End'))
                    for item in items
                ]
            elif dtype == 'date':
                converted = []
                for item in items:
                    parsed = _parse_date(item)
                    converted.append(parsed.isoformat() if parsed else item)
                items = converted
            if len(items) > 1 and op == '=':
                clauses.append(f'{col} IN ({",".join("?" for _ in items)})')
                values.extend(items)
            elif items:
                clauses.append(f'{col} {op} ?')
                values.append(items[0])
        return clauses, values

    def query(self, api: str, params: Mapping[str, Any], *, limit: int = 5000) -> List[Dict[str, Any]]:
        spec = get_endpoint(api)
        spec.validate_params(params)
        parquet = self.parquet_glob(api)
        if not parquet:
            raise WarehouseUnavailableError(f'{api} 本地仓库不可用')
        safe_path = parquet.replace("'", "''")
        source = f"read_parquet('{safe_path}', union_by_name=true, hive_partitioning=true)"
        con = self._connect()
        try:
            desc = con.execute(f'DESCRIBE SELECT * FROM {source}').fetchall()
            available_columns = {str(row[0]) for row in desc}
            selected = [to_snake(name) for name in spec.fields if to_snake(name) in available_columns]
            if not selected:
                raise WarehouseUnavailableError(f'{api} 仓库字段与审核契约不匹配')
            clauses, values = self._where(spec, params, available_columns)
            order_col = spec.watermark_field or DATE_COLUMNS.get(api)
            order_sql = f' ORDER BY {order_col} DESC NULLS LAST' if order_col in available_columns else ''
            where_sql = f' WHERE {" AND ".join(clauses)}' if clauses else ''
            query = (
                f'SELECT {", ".join(selected)} FROM {source}'
                f'{where_sql}{order_sql} LIMIT ?'
            )
            cursor = con.execute(query, [*values, max(1, min(int(limit), 100000))])
            names = [item[0] for item in cursor.description]
            rows = [dict(zip(names, row)) for row in cursor.fetchall()]
            return normalize_rows(spec, rows)
        except WarehouseUnavailableError:
            raise
        except Exception as exc:
            raise WarehouseUnavailableError(f'{api} 仓库查询失败: {type(exc).__name__}') from exc
        finally:
            try:
                con.close()
            except Exception:
                pass


# 计划/验收使用的公共命名。
DatayesWarehouseProvider = DatayesWarehouse
