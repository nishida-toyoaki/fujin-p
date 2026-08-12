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

"""lookout（みはらし） - ルート定義

地理院標高タイル（DEM）による地形パノラマ計算。

データの持ち方（すべて本アプリのディレクトリ配下）:
  lookout/dem_data/meta.json    ... モザイクの範囲・進捗
  lookout/dem_data/mosaic.npy   ... 約40m格子の標高モザイク（float32, memmap）
  lookout/dem_data/tiles/       ... 視点標高用の10m解像度タイルキャッシュ(.npy)

外部アクセスは地理院タイル（cyberjapandata.gsi.go.jp）の標高タイル
（dem, z=14, テキスト形式）のみ。出典表示はフロント側で行う。
"""
import datetime
import json
import logging
import math
import os
import time
import urllib.request
import urllib.error
import urllib.parse
import fcntl

import numpy as np
from pytz import timezone
from flask import render_template, request, jsonify, session
import mysql.connector

from config import Config
from db import DatabaseConfig, Tables
from auth import redirect_to_dashboard
from decorators import login_required, admin_required

from . import lookout_bp

# ------------------------------------------------------------------
# 日時ヘルパー（実装ガイド5.2）
# ------------------------------------------------------------------
JST = timezone('Asia/Tokyo')


def get_jst_now():
    """現在の日時をJSTで取得（naive datetime）。INSERT/UPDATEに使う。"""
    return datetime.datetime.now(JST).replace(tzinfo=None)


def fmt_datetime(d):
    """datetime → 'YYYY-MM-DD HH:MM' 文字列。None は空文字。"""
    if d is None:
        return ''
    if isinstance(d, (datetime.datetime, datetime.date)):
        return d.strftime('%Y-%m-%d %H:%M')
    return str(d)


# ------------------------------------------------------------------
# 地域・計算パラメータ（プラットフォーム非依存の定数）
# ------------------------------------------------------------------
ZOOM = 14                            # 地理院 dem タイル（10m元データ）のズーム
POOL = 4                             # 4x4平均 → 約40m格子
CELL = 256 // POOL                   # プールド後の1タイルあたり画素数(64)
R_MAX = 40000.0                      # 視程計算の最大距離 [m]
AZ_STEP = 0.5                        # 方位刻み [deg]（720本）
BAND_NEAR, BAND_MID = 6000.0, 15500.0  # 距離3層の境界 [m]
RE_EFF = 6371000.0 / (1.0 - 0.13)    # 大気差込みの実効地球半径
TILE_URL = 'https://cyberjapandata.gsi.go.jp/xyz/dem/{z}/{x}/{y}.txt'
FETCH_BUDGET_SEC = 15.0              # 1回のbuild/stepで使う時間の上限
FETCH_MAX_TILES = 25                 # 1回のbuild/stepで取得する最大タイル数

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'dem_data')
META_PATH = os.path.join(DATA_DIR, 'meta.json')
MOSAIC_PATH = os.path.join(DATA_DIR, 'mosaic.npy')
TILE_DIR = os.path.join(DATA_DIR, 'tiles')
MOUNTAINS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              'data_for_distribution', 'mountains.csv')
REGIONS_DIR = os.path.join(DATA_DIR, 'regions')
REGION_LOG = os.path.join(DATA_DIR, 'region_log.txt')
RADIUS_MAX_KM = 30.0
TILE_DL_MB = 0.40     # 1タイルあたり平均受信量の目安[MB]（山地の実測より）
OVERPASS_URLS = ['https://overpass.osm.jp/api/interpreter',
                 'https://overpass-api.de/api/interpreter',
                 'https://overpass.kumi.systems/api/interpreter']
PEAKS_MAX = 400       # 1地域あたり保存する山名の上限（標高降順）
TILE_DISK_KB = 16.0   # モザイク内寄与[KB]
TILE_SEC = 0.45       # 1タイルあたり所要時間の目安[秒]

_cache = {'mosaics': {}, 'tiles': {}, 'mts': None}
LOCK_PATH = os.path.join(DATA_DIR, 'build.lock')

# ------------------------------------------------------------------
# Webメルカトル座標ユーティリティ
# ------------------------------------------------------------------
NPIX = (2 ** ZOOM) * 256  # 世界全体のピクセル数


def _merc_xy(lon, lat):
    """経緯度 → z=14の世界ピクセル座標（numpy対応）"""
    lon = np.asarray(lon, dtype=np.float64)
    lat_r = np.radians(np.asarray(lat, dtype=np.float64))
    xg = (lon + 180.0) / 360.0 * NPIX
    yg = (1.0 - np.log(np.tan(lat_r) + 1.0 / np.cos(lat_r)) / math.pi) / 2.0 * NPIX
    return xg, yg


def _tile_of(lon, lat):
    xg, yg = _merc_xy(lon, lat)
    return int(xg // 256), int(yg // 256)


# ------------------------------------------------------------------
# メタ・モザイク管理
# ------------------------------------------------------------------

def _load_mountains():
    """mountains.csv を読む（mtimeが変わったら再読込）"""
    try:
        mtime = os.path.getmtime(MOUNTAINS_PATH)
    except OSError:
        return []
    if _cache['mts'] is not None and _cache['mts'][0] == mtime:
        return _cache['mts'][1]
    mts = []
    try:
        with open(MOUNTAINS_PATH, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split(',')
                if len(parts) < 4:
                    continue
                mts.append({'name': parts[0].strip(),
                            'lat': float(parts[1]), 'lon': float(parts[2]),
                            'elev': float(parts[3])})
    except Exception as e:
        logging.error('lookout mountains.csv parse error: %s', e)
    _cache['mts'] = (mtime, mts)
    return mts


def _finite(x, alt=0.0):
    """JSONに載せる数値の安全化（NaN/Infを既定値に）"""
    try:
        x = float(x)
        return x if math.isfinite(x) else alt
    except Exception:
        return alt


def _region_dir(rid):
    return os.path.join(REGIONS_DIR, rid)


def _region_meta_path(rid):
    return os.path.join(_region_dir(rid), 'meta.json')


def _load_region_meta(rid):
    p = _region_meta_path(rid)
    if os.path.exists(p):
        with open(p, 'r', encoding='utf-8') as f:
            return json.load(f)
    return None


def _save_region_meta(rid, meta):
    tmp = _region_meta_path(rid) + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False)
    os.replace(tmp, _region_meta_path(rid))


def _open_region_mosaic(rid, mode='r'):
    if mode == 'r' and rid in _cache['mosaics']:
        return _cache['mosaics'][rid]
    arr = np.load(os.path.join(_region_dir(rid), 'mosaic.npy'), mmap_mode=mode)
    if mode == 'r':
        _cache['mosaics'][rid] = arr
    return arr


def _tile_lat(y):
    """タイル番号y（上端）の緯度"""
    n = 2 ** ZOOM
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))


def _region_bbox(meta):
    """metaのタイル矩形から (lat_min, lon_min, lat_max, lon_max)"""
    n = 2 ** ZOOM
    lon_min = meta['x0'] / n * 360.0 - 180.0
    lon_max = (meta['x0'] + meta['nx_t']) / n * 360.0 - 180.0
    lat_max = _tile_lat(meta['y0'])
    lat_min = _tile_lat(meta['y0'] + meta['ny_t'])
    return [round(lat_min, 5), round(lon_min, 5), round(lat_max, 5), round(lon_max, 5)]


def _migrate_legacy():
    """旧・単一モザイク（dem_data直下）を地域『北近畿（初期領域）』として取り込む"""
    if not os.path.exists(META_PATH):
        return
    os.makedirs(REGIONS_DIR, exist_ok=True)
    lock_f = open(LOCK_PATH, 'w')
    try:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        if not os.path.exists(META_PATH):
            return
        dst = _region_dir('legacy')
        if os.path.exists(dst):
            return
        os.makedirs(dst)
        with open(META_PATH, 'r', encoding='utf-8') as f:
            meta = json.load(f)
        bbox = _region_bbox(meta)
        meta.setdefault('name', '北近畿（初期領域）')
        meta.setdefault('center_lat', round((bbox[0] + bbox[2]) / 2, 5))
        meta.setdefault('center_lon', round((bbox[1] + bbox[3]) / 2, 5))
        meta.setdefault('radius_km', None)
        with open(os.path.join(dst, 'meta.json'), 'w', encoding='utf-8') as f:
            json.dump(meta, f, ensure_ascii=False)
        os.replace(MOSAIC_PATH, os.path.join(dst, 'mosaic.npy'))
        os.remove(META_PATH)
        logging.info('lookout: legacy mosaic migrated to regions/legacy')
    finally:
        lock_f.close()


def _list_regions():
    """全地域のmeta＋派生情報"""
    _migrate_legacy()
    out = []
    if not os.path.isdir(REGIONS_DIR):
        return out
    for rid in sorted(os.listdir(REGIONS_DIR)):
        meta = _load_region_meta(rid)
        if meta is None:
            continue
        out.append({'id': rid,
                    'name': meta.get('name') or rid,
                    'center_lat': meta.get('center_lat'),
                    'center_lon': meta.get('center_lon'),
                    'radius_km': meta.get('radius_km'),
                    'done': bool(meta.get('done')),
                    'next_i': meta.get('next_i', 0),
                    'total': meta.get('total', 0),
                    'bytes': meta.get('bytes', 0),
                    'disk_mb': round(meta.get('total', 0) * TILE_DISK_KB / 1024, 1),
                    'peaks_count': meta.get('peaks_count', -1),
                    'bbox': _region_bbox(meta)})
    return out


def _log_region(action, rid, detail):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(REGION_LOG, 'a', encoding='utf-8') as f:
            f.write('%s | %s | %s | %s | user_id=%s\n' % (
                fmt_datetime(get_jst_now()), action, rid, detail,
                session.get('user_id')))
    except Exception as e:
        logging.error('lookout region log error: %s', e)


def _region_peaks_path(rid):
    return os.path.join(_region_dir(rid), 'peaks.json')


def _load_region_peaks(rid):
    """地域の自動取得山名（無ければ空）．mtimeが変わったら再読込"""
    p = _region_peaks_path(rid)
    try:
        mtime = os.path.getmtime(p)
    except OSError:
        return []
    key = 'peaks_' + rid
    c = _cache.get(key)
    if c is not None and c[0] == mtime:
        return c[1]
    try:
        with open(p, encoding='utf-8') as f:
            peaks = json.load(f).get('peaks', [])
    except Exception as e:
        logging.error('lookout peaks.json parse error: %s', e)
        peaks = []
    _cache[key] = (mtime, peaks)
    return peaks


def _parse_ele(v):
    try:
        import re as _re
        return float(_re.sub(r'[^0-9.]', '', str(v)))
    except Exception:
        return None


def _fetch_peaks_from_osm(bbox):
    """OpenStreetMapから名前つき山頂（natural=peak）を取得（ミラー3系統を順に試行）"""
    q = ('[out:json][timeout:25];'
         'node["natural"="peak"]["name"](%f,%f,%f,%f);'
         'out qt %d;' % (bbox[0], bbox[1], bbox[2], bbox[3], PEAKS_MAX * 3))
    body = urllib.parse.urlencode({'data': q}).encode('utf-8')
    errs = []
    for url in OVERPASS_URLS:
        try:
            req = urllib.request.Request(url, data=body,
                                         headers={'User-Agent': 'FUJIN-P lookout'})
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode('utf-8'))
            peaks = []
            for el in data.get('elements', []):
                name = (el.get('tags') or {}).get('name')
                if not name or el.get('lat') is None:
                    continue
                peaks.append({'name': name.strip(), 'lat': float(el['lat']),
                              'lon': float(el['lon']),
                              'elev': _parse_ele((el.get('tags') or {}).get('ele'))})
            return peaks
        except Exception as e:
            errs.append('%s: %s' % (url.split('/')[2], e))
            logging.error('lookout overpass error: %s: %s', url, e)
    raise RuntimeError(' / '.join(errs))


def _fetch_tile_txt(x, y, timeout=20):
    """z=14 標高タイル1枚を取得して ((256,256) float32, 取得バイト数) を返す。
    海などの欠損は404 → (NaN, 0)。"""
    url = TILE_URL.format(z=ZOOM, x=x, y=y)
    req = urllib.request.Request(url, headers={'User-Agent': 'FUJIN-P lookout'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return np.full((256, 256), np.nan, dtype=np.float32), 0
        raise
    text = raw.decode('ascii')
    rows = []
    for line in text.strip().split('\n'):
        rows.append([np.nan if v == 'e' else float(v) for v in line.split(',')])
    return np.asarray(rows, dtype=np.float32), len(raw)


def _pool_tile(t):
    """(256,256) → (64,64) 4x4 nanmean"""
    with np.errstate(all='ignore'):
        p = np.nanmean(t.reshape(CELL, POOL, CELL, POOL), axis=(1, 3))
    return p.astype(np.float32)


def _get_view_tile(x, y):
    """視点標高用の10m解像度タイル（キャッシュ付き）。取得失敗はNone。"""
    key = (x, y)
    if key in _cache['tiles']:
        return _cache['tiles'][key]
    os.makedirs(TILE_DIR, exist_ok=True)
    path = os.path.join(TILE_DIR, f'{x}_{y}.npy')
    try:
        if os.path.exists(path):
            t = np.load(path)
        else:
            t, _nb = _fetch_tile_txt(x, y, timeout=6)
            np.save(path, t)
    except Exception as e:
        logging.error('lookout view tile fetch error: %s', e)
        return None
    _cache['tiles'][key] = t
    return t


def _bilinear(arr, col, row):
    """arr[row, col]（実数座標）の双一次補間。NaNは0（海面）扱い。範囲外はクランプ。"""
    ny, nx = arr.shape
    c = np.clip(col, 0.0, nx - 1.001)
    r = np.clip(row, 0.0, ny - 1.001)
    c0 = np.floor(c).astype(np.int64)
    r0 = np.floor(r).astype(np.int64)
    fc = c - c0
    fr = r - r0
    v00 = np.nan_to_num(arr[r0, c0])
    v01 = np.nan_to_num(arr[r0, c0 + 1])
    v10 = np.nan_to_num(arr[r0 + 1, c0])
    v11 = np.nan_to_num(arr[r0 + 1, c0 + 1])
    return v00 * (1 - fc) * (1 - fr) + v01 * fc * (1 - fr) \
        + v10 * (1 - fc) * fr + v11 * fc * fr


def _ground_elev(lat, lon):
    """クリック地点の標高。10m解像度タイルで正確に求める（失敗時はNone→呼び元でモザイク）。"""
    x, y = _tile_of(lon, lat)
    t = _get_view_tile(x, y)
    xg, yg = _merc_xy(lon, lat)
    if t is not None:
        col = float(xg) - x * 256 - 0.5
        row = float(yg) - y * 256 - 0.5
        v = _bilinear(t, np.array([col]), np.array([row]))[0]
        return float(v), 'dem10'
    return None, 'none'


# ------------------------------------------------------------------
# 画面
# ------------------------------------------------------------------

@lookout_bp.route('/')
@login_required
def index():
    """メイン画面"""
    is_admin = (session.get('user_category') == 'admin')
    return render_template('lookout/index.html', is_admin=is_admin)


@lookout_bp.route('/return_to_fujin')
@login_required
def return_to_fujin():
    """FUJINダッシュボードに戻る"""
    return redirect_to_dashboard()


# ------------------------------------------------------------------
# 地域管理（一覧は全ユーザ，追加・取得・削除は管理者）
# ------------------------------------------------------------------

@lookout_bp.route('/api/regions', methods=['GET'])
@login_required
def api_regions():
    """適用地域の一覧と状態"""
    try:
        return jsonify({'success': True, 'regions': _list_regions()})
    except Exception as e:
        logging.error('lookout regions error: %s', e)
        return jsonify({'success': False, 'error': str(e)}), 500


def _tiles_for(lat, lon, radius_km):
    """中心＋半径の円に外接するタイル矩形"""
    dlat = radius_km * 1000.0 / 111132.0
    dlon = radius_km * 1000.0 / (111320.0 * math.cos(math.radians(lat)))
    x0, y0 = _tile_of(lon - dlon, lat + dlat)
    x1, y1 = _tile_of(lon + dlon, lat - dlat)
    return x0, y0, x1 - x0 + 1, y1 - y0 + 1


def _estimate(lat, lon, radius_km):
    x0, y0, nx_t, ny_t = _tiles_for(lat, lon, radius_km)
    tiles = nx_t * ny_t
    return {'tiles': tiles, 'nx_t': nx_t, 'ny_t': ny_t,
            'dl_mb': round(tiles * TILE_DL_MB, 1),
            'disk_mb': round(tiles * TILE_DISK_KB / 1024, 1),
            'minutes': round(tiles * TILE_SEC / 60.0, 1)}


def _check_region_input(data):
    lat = float(data.get('lat'))
    lon = float(data.get('lon'))
    radius = float(data.get('radius_km'))
    if not (20.0 <= lat <= 46.0 and 122.0 <= lon <= 154.0):
        raise ValueError('中心が日本の範囲外です')
    if not (1.0 <= radius <= RADIUS_MAX_KM):
        raise ValueError('半径は1〜%.0fkmで指定してください' % RADIUS_MAX_KM)
    return lat, lon, radius


@lookout_bp.route('/api/region/estimate', methods=['POST'])
@admin_required
def api_region_estimate():
    """タイル数・通信量・保存量・所要時間の見積り"""
    try:
        lat, lon, radius = _check_region_input(request.json or {})
        est = _estimate(lat, lon, radius)
        est['success'] = True
        return jsonify(est)
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 400


@lookout_bp.route('/api/region/create', methods=['POST'])
@admin_required
def api_region_create():
    """地域を登録してモザイクの器を作る（取得はbuild_stepで）"""
    try:
        data = request.json or {}
        lat, lon, radius = _check_region_input(data)
        name = (data.get('name') or '').strip() or \
            ('北緯%.3f 東経%.3f 半径%.0fkm' % (lat, lon, radius))
        x0, y0, nx_t, ny_t = _tiles_for(lat, lon, radius)
        rid = 'r' + get_jst_now().strftime('%Y%m%d%H%M%S')
        os.makedirs(_region_dir(rid))
        arr = np.lib.format.open_memmap(
            os.path.join(_region_dir(rid), 'mosaic.npy'), mode='w+',
            dtype=np.float32, shape=(ny_t * CELL, nx_t * CELL))
        arr[:] = np.nan
        arr.flush()
        del arr
        meta = {'z': ZOOM, 'pool': POOL, 'x0': x0, 'y0': y0,
                'nx_t': nx_t, 'ny_t': ny_t,
                'next_i': 0, 'total': nx_t * ny_t, 'done': False, 'bytes': 0,
                'name': name, 'center_lat': lat, 'center_lon': lon,
                'radius_km': radius,
                'created_at': fmt_datetime(get_jst_now())}
        _save_region_meta(rid, meta)
        _log_region('add', rid, '%s (%.4f,%.4f) r=%skm tiles=%d'
                    % (name, lat, lon, radius, nx_t * ny_t))
        return jsonify({'success': True, 'id': rid})
    except Exception as e:
        logging.error('lookout region create error: %s', e)
        return jsonify({'success': False, 'error': str(e)}), 500


@lookout_bp.route('/api/region/build_step', methods=['POST'])
@admin_required
def api_region_build_step():
    """指定地域のタイルを少しずつ取得（完了までクライアントが反復呼出）"""
    try:
        rid = (request.json or {}).get('id', '')
        meta = _load_region_meta(rid)
        if meta is None:
            return jsonify({'success': False, 'error': '地域がありません'}), 404
        if meta.get('done'):
            return jsonify({'success': True, 'state': 'ready', 'id': rid,
                            'next_i': meta['total'], 'total': meta['total'],
                            'bytes': meta.get('bytes', 0)})
        lock_f = open(os.path.join(_region_dir(rid), 'build.lock'), 'w')
        try:
            fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            lock_f.close()
            return jsonify({'success': True, 'state': 'building', 'busy': True,
                            'id': rid, 'next_i': meta.get('next_i', 0),
                            'total': meta.get('total', 0),
                            'bytes': meta.get('bytes', 0)})
        meta = _load_region_meta(rid)  # ロック取得後に読み直す
        if meta.get('done'):
            lock_f.close()
            return jsonify({'success': True, 'state': 'ready', 'id': rid,
                            'next_i': meta['total'], 'total': meta['total'],
                            'bytes': meta.get('bytes', 0)})
        arr = _open_region_mosaic(rid, mode='r+')
        _cache['mosaics'].pop(rid, None)
        nx_t = meta['nx_t']
        t_start = time.monotonic()
        n_done = 0
        while (meta['next_i'] < meta['total'] and n_done < FETCH_MAX_TILES
               and time.monotonic() - t_start < FETCH_BUDGET_SEC):
            i = meta['next_i']
            ty, tx = divmod(i, nx_t)
            tile, nb = _fetch_tile_txt(meta['x0'] + tx, meta['y0'] + ty)
            arr[ty * CELL:(ty + 1) * CELL, tx * CELL:(tx + 1) * CELL] = _pool_tile(tile)
            meta['bytes'] = meta.get('bytes', 0) + nb
            meta['next_i'] = i + 1
            n_done += 1
            time.sleep(0.02)
        arr.flush()
        if meta['next_i'] >= meta['total']:
            meta['done'] = True
        _save_region_meta(rid, meta)
        lock_f.close()
        return jsonify({'success': True, 'id': rid,
                        'state': 'ready' if meta['done'] else 'building',
                        'next_i': meta['next_i'], 'total': meta['total'],
                        'bytes': meta.get('bytes', 0)})
    except Exception as e:
        logging.error('lookout region build_step error: %s', e)
        return jsonify({'success': False, 'error': str(e)}), 500


@lookout_bp.route('/api/region/fetch_peaks', methods=['POST'])
@admin_required
def api_region_fetch_peaks():
    """地域内の山名（名前つき山頂）をOpenStreetMapから取得して保存"""
    try:
        rid = (request.json or {}).get('id', '')
        meta = _load_region_meta(rid)
        if meta is None:
            return jsonify({'success': False, 'error': '地域がありません'}), 404
        peaks = _fetch_peaks_from_osm(_region_bbox(meta))
        n = _store_region_peaks(rid, meta, peaks, 'server')
        return jsonify({'success': True, 'count': n})
    except Exception as e:
        logging.error('lookout fetch_peaks error: %s', e)
        return jsonify({'success': False,
                        'error': '山名の取得に失敗しました（後でもう一度お試しください）: %s' % e}), 500


def _store_region_peaks(rid, meta, peaks, via):
    """山名リストを検証・DEM補完して地域に保存し件数を返す"""
    clean = []
    for p in peaks[:PEAKS_MAX * 5]:
        try:
            name = str(p.get('name', '')).strip()[:80]
            lat = float(p.get('lat'))
            lon = float(p.get('lon'))
        except (TypeError, ValueError):
            continue
        if not name or not (20.0 <= lat <= 46.0 and 122.0 <= lon <= 154.0):
            continue
        elev = p.get('elev')
        try:
            elev = float(elev) if elev is not None else None
        except (TypeError, ValueError):
            elev = None
        clean.append({'name': name, 'lat': lat, 'lon': lon, 'elev': elev})
    if meta.get('done'):
        mosaic = _open_region_mosaic(rid)
        for p in clean:
            if p['elev'] is None:
                xg, yg = _merc_xy(p['lon'], p['lat'])
                col = (float(xg) - meta['x0'] * 256) / POOL - 0.5
                row = (float(yg) - meta['y0'] * 256) / POOL - 0.5
                p['elev'] = round(float(_bilinear(
                    mosaic, np.array([col]), np.array([row]))[0]))
    clean = [p for p in clean if p['elev'] is not None]
    clean.sort(key=lambda p: -p['elev'])
    clean = clean[:PEAKS_MAX]
    tmp = _region_peaks_path(rid) + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({'fetched_at': fmt_datetime(get_jst_now()),
                   'source': 'OpenStreetMap (ODbL) via ' + via,
                   'peaks': clean}, f, ensure_ascii=False)
    os.replace(tmp, _region_peaks_path(rid))
    meta['peaks_count'] = len(clean)
    _save_region_meta(rid, meta)
    _cache.pop('peaks_' + rid, None)
    _log_region('fetch_peaks', rid, '%d peaks (%s)' % (len(clean), via))
    return len(clean)


@lookout_bp.route('/api/region/save_peaks', methods=['POST'])
@admin_required
def api_region_save_peaks():
    """ブラウザ側でOverpassから取得した山名リストを預かって保存"""
    try:
        data = request.json or {}
        rid = data.get('id', '')
        meta = _load_region_meta(rid)
        if meta is None:
            return jsonify({'success': False, 'error': '地域がありません'}), 404
        peaks = data.get('peaks')
        if not isinstance(peaks, list):
            return jsonify({'success': False, 'error': 'peaksがありません'}), 400
        n = _store_region_peaks(rid, meta, peaks, 'browser')
        return jsonify({'success': True, 'count': n})
    except Exception as e:
        logging.error('lookout save_peaks error: %s', e)
        return jsonify({'success': False, 'error': str(e)}), 500


@lookout_bp.route('/api/region/delete', methods=['POST'])
@admin_required
def api_region_delete():
    """地域を削除（モザイクごと）"""
    try:
        import shutil
        rid = (request.json or {}).get('id', '')
        meta = _load_region_meta(rid)
        if meta is None:
            return jsonify({'success': False, 'error': '地域がありません'}), 404
        shutil.rmtree(_region_dir(rid))
        _cache['mosaics'].pop(rid, None)
        _log_region('delete', rid, meta.get('name', ''))
        return jsonify({'success': True})
    except Exception as e:
        logging.error('lookout region delete error: %s', e)
        return jsonify({'success': False, 'error': str(e)}), 500


# ------------------------------------------------------------------
# パノラマ計算
# ------------------------------------------------------------------

@lookout_bp.route('/api/panorama', methods=['POST'])
@login_required
def api_panorama():
    """指定地点（＋視点高0〜100m）からの全方位スカイラインを計算して返す。"""
    lat = lon = height = None
    try:
        data = request.json or {}
        lat = float(data.get('lat'))
        lon = float(data.get('lon'))
        height = max(0.0, min(100.0, float(data.get('height', 0))))

        cands = [r for r in _list_regions() if r['done']
                 and r['bbox'][0] <= lat <= r['bbox'][2]
                 and r['bbox'][1] <= lon <= r['bbox'][3]]
        if not cands:
            return jsonify({'success': False,
                            'error': 'この地点は適用地域の外です．管理者に地域の追加を依頼してください'}), 400
        region = min(cands, key=lambda r: (lat - (r['center_lat'] or 0)) ** 2
                     + (lon - (r['center_lon'] or 0)) ** 2)
        meta = _load_region_meta(region['id'])

        ground, elev_src = _ground_elev(lat, lon)
        mosaic = _open_region_mosaic(region['id'])
        if ground is None:
            xg0, yg0 = _merc_xy(lon, lat)
            col0 = (float(xg0) - meta['x0'] * 256) / POOL - 0.5
            row0 = (float(yg0) - meta['y0'] * 256) / POOL - 0.5
            ground = float(_bilinear(mosaic, np.array([col0]), np.array([row0]))[0])
            elev_src = 'mosaic'
        h0 = ground + height
        ny, nx = mosaic.shape
        m_lat = 111132.0
        m_lon = 111320.0 * math.cos(math.radians(lat))
        # 距離刻み：近距離25m・遠距離50m
        S = np.concatenate([np.arange(30.0, 8000.0, 25.0),
                            np.arange(8000.0, R_MAX, 50.0)])
        curv = np.degrees(S / (2.0 * RE_EFF))
        az_list = np.arange(0.0, 360.0, AZ_STEP)

        out = {'all': [], 'near': [], 'mid': [], 'far': [],
               'dist': [], 'elev': [], 'exit': []}
        band_masks = None
        for az in az_list:
            ar = math.radians(az)
            lat_s = lat + S * math.cos(ar) / m_lat
            lon_s = lon + S * math.sin(ar) / m_lon
            xg, yg = _merc_xy(lon_s, lat_s)
            col = (xg - meta['x0'] * 256) / POOL - 0.5
            row = (yg - meta['y0'] * 256) / POOL - 0.5
            inside = (col >= 0) & (col <= nx - 1.001) & (row >= 0) & (row <= ny - 1.001)
            if inside.all():
                exit_d = R_MAX
                valid = None
            else:
                first_out = int(np.argmin(inside))  # 最初のFalse位置
                exit_d = float(S[first_out]) if not inside[first_out] else R_MAX
                valid = inside
            h = _bilinear(mosaic, col, row)
            ang = np.degrees(np.arctan2(h - h0, S)) - curv
            if valid is not None:
                ang = np.where(valid, ang, -90.0)
            if band_masks is None:
                band_masks = {
                    'near': S < BAND_NEAR,
                    'mid': (S >= BAND_NEAR) & (S < BAND_MID),
                    'far': S >= BAND_MID,
                }
            idx = int(np.argmax(ang))
            out['all'].append(round(float(ang[idx]), 2))
            out['dist'].append(int(S[idx]))
            out['elev'].append(int(h[idx]))
            out['exit'].append(int(exit_d))
            for k in ('near', 'mid', 'far'):
                out[k].append(round(float(ang[band_masks[k]].max()), 2))

        # 山名の可視判定（mountains.csv収録の山のうち稜線上に見えているもの）
        peaks = []
        A = np.array(out['all'])
        n_az = len(A)
        csv_mts = _load_mountains()
        cand_mts = list(csv_mts)
        for p in _load_region_peaks(region['id']):
            dup = any(abs(p['lat'] - m['lat']) < 0.003
                      and abs(p['lon'] - m['lon']) < 0.004 for m in csv_mts)
            if not dup:
                cand_mts.append(p)
        for m in cand_mts:
            if not (region['bbox'][0] <= m['lat'] <= region['bbox'][2]
                    and region['bbox'][1] <= m['lon'] <= region['bbox'][3]):
                continue
            dN = (m['lat'] - lat) * m_lat
            dE = (m['lon'] - lon) * m_lon
            D = math.hypot(dN, dE)
            if D < 800 or D > R_MAX * 0.98:
                continue
            az_m = math.degrees(math.atan2(dE, dN)) % 360.0
            tgt = math.degrees(math.atan2(m['elev'] - h0, D))                 - math.degrees(D / (2.0 * RE_EFF))
            i = int(round(az_m / AZ_STEP)) % n_az
            if D > out['exit'][i] + 2000:
                continue  # データ範囲の縁の向こうは判定不能
            sky = max(A[(i - 1) % n_az], A[i], A[(i + 1) % n_az])
            if tgt >= sky - 0.12 and tgt > -3.0:
                peaks.append({'name': m['name'],
                              'az': round(az_m, 1),
                              'ang': round(_finite(tgt), 2),
                              'dist_km': round(D / 1000.0, 1),
                              'elev': int(m['elev'])})
        peaks.sort(key=lambda p: p['az'])

        return jsonify({'success': True,
                        'az_step': AZ_STEP,
                        'ground_elev': round(_finite(ground), 1),
                        'elev_source': elev_src,
                        'height': height,
                        'eye_elev': round(_finite(h0), 1),
                        'r_max': int(R_MAX),
                        'region': {'id': region['id'], 'name': region['name']},
                        'peaks': peaks,
                        'skyline': out})
    except Exception as e:
        logging.error('lookout panorama error at lat=%s lon=%s h=%s: %s',
                      lat, lon, height, e)
        return jsonify({'success': False, 'error': str(e)}), 500


# ------------------------------------------------------------------
# 閲覧履歴
# ------------------------------------------------------------------

@lookout_bp.route('/api/history/add', methods=['POST'])
@login_required
def api_history_add():
    """クリック地点を履歴に記録"""
    try:
        data = request.json or {}
        user_id = session.get('user_id')
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO lookout_history (user_id, lat, lon, ground_elev, created_at)
            VALUES (%s, %s, %s, %s, %s)
        """, (user_id, float(data.get('lat')), float(data.get('lon')),
              float(data.get('ground_elev', 0)), get_jst_now()))
        conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        logging.error('lookout history_add error: %s', e)
        if 'conn' in locals():
            conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()


@lookout_bp.route('/api/history/recent', methods=['GET'])
@login_required
def api_history_recent():
    """自分の最近の閲覧地点（10件）"""
    try:
        user_id = session.get('user_id')
        conn = mysql.connector.connect(**DatabaseConfig.default())
        cursor = conn.cursor(dictionary=True)
        cursor.execute("""
            SELECT id, lat, lon, ground_elev, created_at
            FROM lookout_history
            WHERE user_id = %s
            ORDER BY created_at DESC
            LIMIT 10
        """, (user_id,))
        rows = cursor.fetchall()
        for r in rows:
            r['created_at'] = fmt_datetime(r.get('created_at'))
            r['lat'] = float(r['lat'])
            r['lon'] = float(r['lon'])
            r['ground_elev'] = float(r['ground_elev'])
        return jsonify({'success': True, 'items': rows})
    except Exception as e:
        logging.error('lookout history_recent error: %s', e)
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()