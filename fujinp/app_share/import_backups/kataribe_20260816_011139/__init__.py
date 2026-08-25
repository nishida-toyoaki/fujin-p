"""
かたりべ (kataribe) - 人×AIコラボのプレゼン語り編集アプリ
発想をストーリー化する：ブロックの登場・退場で語りを組み立てるページレス・プレゼンエディタ．
Claudeとの協働は「依頼文の生成→手渡し→回答スペックの総替え取り込み」の手動往復方式（v1）．
"""
from flask import Blueprint

kataribe_bp = Blueprint(
    'kataribe',
    __name__,
    template_folder='templates',
    url_prefix='/kataribe'
)

from . import routes
