# -*- coding: utf-8 -*-
# SPDX-FileCopyrightText: 2024-2026 Toyoaki Nishida
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# This file is part of FUJIN-P.
# Copyright (C) 2024-2026 Toyoaki Nishida
#
# FUJIN-P is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# FUJIN-P is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with FUJIN-P.  If not, see <https://www.gnu.org/licenses/>.
#
# Source: https://github.com/nishida-toyoaki/fujin-p

"""そらから (Skywalker) - ルート定義

地形レンダリング・飛行はすべてフロントエンド（Three.js / WebGL）で行う。
標高タイルは訪問者のブラウザが地理院タイルサーバから直接取得するため、
サーバ側の負荷はテンプレート配信と地域一覧の取得のみ。

【公開設定】
公開期間中はログイン不要で公開（PUBLIC_ACCESS = True）。
終了後は PUBLIC_ACCESS = False に書き換えてリロードすると、
従来どおり @login_required 相当の保護がかかる。
（閲覧系のみ公開。カスタム地域の追加APIは常にログイン必須。）
"""
import datetime
import json
import logging
from pytz import timezone

from flask import render_template, request, jsonify, session, redirect
import mysql.connector

from config import Config
from db import DatabaseConfig, Tables
from auth import redirect_to_dashboard
from decorators import login_required

from . import sorakara_bp

# ────────────────────────────────────────────
# 公開スイッチ（公開終了後は False に戻す）
# False にすると、ログイン保護がかかると同時に
# ログインページのカードも自動的に非表示になる。
# ────────────────────────────────────────────
PUBLIC_ACCESS = True


@sorakara_bp.record_once
def _register_public_flag(state):
    """公開状態を Flask config に載せる（テンプレートから参照可能にする）。"""
    state.app.config['SORAKARA_PUBLIC'] = PUBLIC_ACCESS


# タイムゾーン設定
JST = timezone('Asia/Tokyo')


def get_jst_now():
    """現在の日時をJSTで取得（naive datetime）"""
    return datetime.datetime.now(JST).replace(tzinfo=None)


def fmt_datetime(d):
    """datetime → 'YYYY-MM-DD HH:MM' 文字列。None は空文字。"""
    if d is None:
        return ''
    if isinstance(d, (datetime.datetime, datetime.date)):
        return d.strftime('%Y-%m-%d %H:%M')
    return str(d)


# ------------------------------------------------------------------
# プリセット地域（当面は福知山市周辺のみ）
# id は負値にして DB 登録地域（正値）と衝突しないようにする
# zoom: 地理院標高タイル dem_png のズーム（最大14）
# radius: 中心タイルから読み込む半径（2 → 5x5 タイル ≒ 40km四方）
# ------------------------------------------------------------------
# lat/lon は「目標そのもの」の座標にする（フライトは中心へ直行→小半径周回するため）。
# 座標は国土地理院 地名検索API で確認済み（2026-07）。
PRESET_REGIONS = [
    {'id': -1, 'name': '福知山市街地・由良川', 'description': '福知山盆地と由良川の蛇行を望む',
     'lat': 35.2966, 'lon': 135.1266, 'zoom': 12, 'radius': 2},   # 福知山市街中心
    {'id': -2, 'name': '大江山連峰', 'description': '鬼伝説の大江山（833m）と雲海の名所',
     'lat': 35.4535, 'lon': 135.1067, 'zoom': 12, 'radius': 2},   # 大江山（千丈ヶ嶽）山頂
    {'id': -3, 'name': '三岳山・雲原', 'description': '福知山市最高峰の三岳山（839m）周辺',
     'lat': 35.3941, 'lon': 135.0577, 'zoom': 12, 'radius': 2},   # 三岳山 山頂
    {'id': -4, 'name': '夜久野高原', 'description': '京都府唯一の火山・宝山と夜久野高原',
     'lat': 35.3364, 'lon': 134.9130, 'zoom': 12, 'radius': 2},   # 夜久野ヶ原（宝山南麓）
]


def get_regions():
    """プリセット地域＋DB登録地域を返す。

    スタブ段階では schema.sql 未適用でも動くよう、
    DBが使えない場合はプリセットのみ返す。
    """
    regions = [dict(r) for r in PRESET_REGIONS]
    try:
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT id, name, description, lat, lon, zoom, radius, created_at
            FROM sorakara_regions
            ORDER BY id
        """)
        for r in cursor.fetchall():
            r['lat'] = float(r['lat'])
            r['lon'] = float(r['lon'])
            r['created_at'] = fmt_datetime(r.get('created_at'))
            regions.append(r)
    except Exception as e:
        logging.warning("sorakara: DB地域一覧を取得できないためプリセットのみ使用: %s", e)
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()
    return regions


def find_region(region_id):
    for r in get_regions():
        if r['id'] == region_id:
            return r
    return None


# ────────────────────────────────────────────
# 閲覧系ビュー（PUBLIC_ACCESS に応じてログイン保護を切り替え）
# ────────────────────────────────────────────

def _index():
    """ダッシュボード：読み込む地域を選んで GO"""
    return render_template('sorakara/index.html', regions=get_regions())


def _flight():
    """フライトシーン"""
    region_id = request.args.get('region_id', type=int)
    region = find_region(region_id) if region_id is not None else None
    if region is None:
        region = dict(PRESET_REGIONS[0])
    quality = request.args.get('quality', 'std')
    if quality not in ('low', 'std'):
        quality = 'std'
    return render_template(
        'sorakara/flight.html',
        region=region,
        region_json=json.dumps(region, ensure_ascii=False),
        quality=quality
    )


def _api_regions():
    """地域一覧API"""
    return jsonify({'success': True, 'regions': get_regions()})


def _maybe_protect(f):
    """PUBLIC_ACCESS が False のときだけ login_required を適用する。"""
    return f if PUBLIC_ACCESS else login_required(f)


# PUBLIC_ACCESS に応じてログイン保護を切り替えて登録する。
# endpoint 名は従来どおり（url_for の参照名は変わらない）。
sorakara_bp.route('/', endpoint='index')(_maybe_protect(_index))
sorakara_bp.route('/flight', endpoint='flight')(_maybe_protect(_flight))
sorakara_bp.route('/api/regions', methods=['GET'],
                  endpoint='api_regions')(_maybe_protect(_api_regions))


# ────────────────────────────────────────────
# 書き込み系API（公開中もログイン必須のまま）
# ────────────────────────────────────────────

@sorakara_bp.route('/api/regions', methods=['POST'])
@login_required
def api_save_region():
    """カスタム地域の追加API（要 schema.sql 適用）"""
    try:
        data = request.json or {}
        user_id = session.get('user_id')
        name = (data.get('name') or '').strip()
        try:
            lat = float(data.get('lat'))
            lon = float(data.get('lon'))
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': '緯度・経度は数値で指定してください'}), 400

        if not name:
            return jsonify({'success': False, 'error': '地域名は必須です'}), 400
        if not (20.0 <= lat <= 46.0 and 122.0 <= lon <= 154.0):
            return jsonify({'success': False, 'error': '日本国内の緯度経度を指定してください'}), 400

        zoom = min(max(int(data.get('zoom', 12)), 10), 14)
        radius = min(max(int(data.get('radius', 2)), 1), 3)

        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO sorakara_regions
                (user_id, name, description, lat, lon, zoom, radius, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (user_id, name, data.get('description', ''), lat, lon,
              zoom, radius, get_jst_now()))
        conn.commit()
        return jsonify({'success': True, 'id': cursor.lastrowid})

    except Exception as e:
        logging.error("sorakara api_save_region error: %s", e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


# ────────────────────────────────────────────
# FUJIN-Pへ戻る
# ────────────────────────────────────────────

@sorakara_bp.route('/return_to_fujin')
def return_to_fujin():
    """ログイン済みならダッシュボードへ、未ログインならトップ（ログインページ）へ戻る。"""
    if session.get('user_id'):
        return redirect_to_dashboard()
    return redirect('/')