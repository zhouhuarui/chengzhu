"""元信息接口：免责声明等。"""

from flask import Blueprint, jsonify

from ..constants import DISCLAIMER, DATA_NOTICE, SCENARIO_BANNER, PRODUCT_NAME_ZH, PRODUCT_NAME_EN
from datetime import datetime

meta_bp = Blueprint('meta', __name__)


@meta_bp.route('/disclaimer', methods=['GET'])
def disclaimer():
    return jsonify({
        'success': True,
        'data': {
            'product_zh': PRODUCT_NAME_ZH,
            'product_en': PRODUCT_NAME_EN,
            'disclaimer': DISCLAIMER,
            'data_notice': DATA_NOTICE.format(
                timestamp=datetime.now().astimezone().isoformat(timespec='seconds')
            ),
            'scenario_banner': SCENARIO_BANNER,
        },
    })
