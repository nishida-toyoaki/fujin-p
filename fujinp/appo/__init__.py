"""
appo - あぽる
オフィスアワー（対面／オンライン）を公開し，招待と請求でミーティングを成立させる会員制の面会調整アプリ．
排他的（1対1）と非排他的（懇談）を区別し，直前の予約・キャンセルとサブチャンネルでの連絡を扱う．
"""
from flask import Blueprint

appo_bp = Blueprint(
    'appo',
    __name__,
    template_folder='templates',
    url_prefix='/appo'
)

from . import routes
