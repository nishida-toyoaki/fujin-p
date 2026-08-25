"""
image_archiver - いめくら
Google Driveをバックエンドとしたファイルホスティングサービス。
タイムスタンプ+ランダムラベルでファイルにURLを割り当て、
ブラウザから直接アクセス可能にする。
"""
from flask import Blueprint

image_archiver_bp = Blueprint(
    'image_archiver',
    __name__,
    template_folder='templates',
    url_prefix='/image_archiver'
)

from . import routes