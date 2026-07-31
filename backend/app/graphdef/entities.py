"""固定核心本体：实体与关系 Pydantic 模型。"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class Company(BaseModel):
    """上市公司。"""
    stock_code: Optional[str] = None
    industry: Optional[str] = None
    market_cap_band: Optional[str] = None


class Person(BaseModel):
    """相关人物。"""
    title: Optional[str] = None
    company: Optional[str] = None


class Disclosure(BaseModel):
    """公告/财报披露。"""
    disclosure_type: Optional[str] = None
    report_period: Optional[str] = None
    disclose_date: Optional[str] = None
    url: Optional[str] = None


class FinancialMetric(BaseModel):
    """财务指标。"""
    metric_name: Optional[str] = None
    report_period: Optional[str] = None
    value: Optional[str] = None
    unit: Optional[str] = None
    yoy: Optional[str] = None


class Event(BaseModel):
    """公司事件。"""
    event_type: Optional[str] = None
    event_date: Optional[str] = None


class Opinion(BaseModel):
    """机构观点（与事实区分）。"""
    institution: Optional[str] = None
    rating: Optional[str] = None
    opinion_date: Optional[str] = None


class Industry(BaseModel):
    """行业。"""
    name: Optional[str] = None


class Product(BaseModel):
    """产品/业务线。"""
    name: Optional[str] = None


class RiskFactor(BaseModel):
    """风险因素。"""
    description: Optional[str] = None


class DISCLOSES(BaseModel):
    """公司→披露。"""
    note: Optional[str] = None


class REPORTS(BaseModel):
    """披露→指标。"""
    note: Optional[str] = None


class INVOLVES(BaseModel):
    """事件→公司/人。"""
    note: Optional[str] = None


class RATES(BaseModel):
    """机构→公司。"""
    rating: Optional[str] = None


class COMPETES_WITH(BaseModel):
    note: Optional[str] = None


class BELONGS_TO(BaseModel):
    note: Optional[str] = None


class SUPPLIES(BaseModel):
    note: Optional[str] = None


class WARNS(BaseModel):
    note: Optional[str] = None


ENTITY_TYPES = {
    'Company': Company,
    'Person': Person,
    'Disclosure': Disclosure,
    'FinancialMetric': FinancialMetric,
    'Event': Event,
    'Opinion': Opinion,
    'Industry': Industry,
    'Product': Product,
    'RiskFactor': RiskFactor,
}

EDGE_TYPES = {
    'DISCLOSES': DISCLOSES,
    'REPORTS': REPORTS,
    'INVOLVES': INVOLVES,
    'RATES': RATES,
    'COMPETES_WITH': COMPETES_WITH,
    'BELONGS_TO': BELONGS_TO,
    'SUPPLIES': SUPPLIES,
    'WARNS': WARNS,
}
