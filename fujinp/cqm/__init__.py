"""こんか（CQM / conversation quanta） — 会話量子の倉庫と，親子のあいだの二方向のやり取り。

倉庫は種別と作法しか知らない。様式の語彙（項目名・欄の名前）は
すべてデータの側にあり，このパッケージのコードには現れない。
"""

from flask import Blueprint

cqm_bp = Blueprint(
    'cqm', __name__,
    url_prefix='/cqm',
    template_folder='templates',
)

from . import routes  # noqa: E402,F401
