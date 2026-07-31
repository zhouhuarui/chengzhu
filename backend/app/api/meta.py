"""元信息接口：免责声明等。"""

from flask import Blueprint, jsonify

from ..config import Config
from ..constants import DISCLAIMER, DATA_NOTICE, SCENARIO_BANNER, PRODUCT_NAME_ZH, PRODUCT_NAME_EN
from datetime import datetime

meta_bp = Blueprint('meta', __name__)


@meta_bp.route('/disclaimer', methods=['GET'])
def disclaimer():
    private_datayes = (
        Config.DATAYES_ENABLED
        and Config.DATAYES_LICENSE_MODE == 'private_derived_only'
    )
    disclaimer_text = (
        DISCLAIMER.replace('基于公开信息', '基于已授权数据与公开信息')
        if private_datayes else DISCLAIMER
    )
    data_notice = DATA_NOTICE.format(
        timestamp=datetime.now().astimezone().isoformat(timespec='seconds')
    )
    if private_datayes:
        data_notice = '数据来源：已授权 Datayes 派生数据及公开渠道；' + data_notice.split('；', 1)[-1]
    return jsonify({
        'success': True,
        'data': {
            'product_zh': PRODUCT_NAME_ZH,
            'product_en': PRODUCT_NAME_EN,
            'disclaimer': disclaimer_text,
            'data_notice': data_notice,
            'scenario_banner': SCENARIO_BANNER,
        },
    })
