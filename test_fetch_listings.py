"""fetch_listings.py 단위·통합 테스트. 네트워크는 전부 가짜로 막음.
실행: uv run pytest --cov --cov-report=term-missing
"""
import gzip, io, json, os, urllib.error
from datetime import datetime

import pytest

import fetch_listings as fl


def row(**kw):
    """대시보드·diff 가 쓰는 매물 한 건(필수 키만 채움)."""
    r = {'id': 1, 'sales': '전세', 'deposit': 20000, 'rent': 0, 'm2': 33.0, 'floor': '3', 'floors': '5',
         'manage': 5, 'svc': '빌라', 'room': '분리형원룸', 'addr': '강남구 역삼동', 'approve': '20100101',
         'movein': None, 'title': '', 'img': None, 'pnu': None, 'lat': 37.4949, 'lng': 127.0300}
    r.update(kw)
    return r


@pytest.fixture(autouse=True)
def no_network(monkeypatch, tmp_path):
    """실수로 진짜 API 를 치지 않게 막고, 캐시 경로를 임시 폴더로 돌림."""
    def boom(*a, **k): raise AssertionError('network call in test')
    monkeypatch.setattr(fl.urllib.request, 'urlopen', boom)
    monkeypatch.setattr(fl, 'ODSAY_CACHE', str(tmp_path / 'odsay'))
    monkeypatch.setattr(fl, 'RT_CACHE', str(tmp_path / 'rt'))
    monkeypatch.setattr(fl, 'ODSAY_KEY', '')
    monkeypatch.setattr(fl.time, 'sleep', lambda s: None)


# ---------- 지오해시·거리 ----------

def test_geohash_roundtrip_and_neighbors():
    lat, lon = fl.STATIONS['강남']
    gh = fl.gh_encode(lat, lon, 6)
    assert len(gh) == 6
    (la0, la1), (lo0, lo1) = fl.gh_bbox(gh)
    assert la0 <= lat <= la1 and lo0 <= lon <= lo1
    nb = fl.neighbors(gh)
    assert len(nb) == 9 and len(set(nb)) == 9 and gh in nb


def test_hav_one_degree_latitude():
    assert fl.hav(37, 127, 38, 127) == pytest.approx(111195, rel=1e-3)
    assert fl.hav(37.5, 127, 37.5, 127) == 0


def test_commute_picks_nearest_of_office_and_shuttles():
    d, name = fl.commute(*fl.SHUTTLE['양재셔틀'])
    assert (d, name) == (0, '양재셔틀')
    d, name = fl.commute(*fl.OFFICE)
    assert (d, name) == (0, '회사도보')


def test_nearest_stn():
    assert fl.nearest_stn(*fl.STATIONS['도곡']) == '도곡'


# ---------- ODsay 통근 ----------

class FakeResp(io.BytesIO):
    pass


def fake_urlopen(payload):
    def _open(req, timeout=None):
        if isinstance(payload, Exception): raise payload
        return FakeResp(json.dumps(payload).encode())
    return _open


def test_odsay_raw_without_key_returns_none_and_no_cache(tmp_path):
    assert fl.odsay_raw(37.4, 127.1, 37.47, 127.03) == {'min': None}
    assert os.listdir(fl.ODSAY_CACHE) == []


def test_odsay_raw_success_is_cached(monkeypatch):
    monkeypatch.setattr(fl, 'ODSAY_KEY', 'k')
    monkeypatch.setattr(fl.urllib.request, 'urlopen',
                        fake_urlopen({'result': {'path': [{'info': {'totalTime': 23.4, 'totalWalk': 310.6}}]}}))
    assert fl.odsay_raw(37.4, 127.1, 37.47, 127.03) == {'min': 23, 'walk': 311}
    # 두 번째는 캐시에서 — 네트워크가 다시 터지면 실패해야 함
    monkeypatch.setattr(fl.urllib.request, 'urlopen', fake_urlopen(RuntimeError('should not call')))
    assert fl.odsay_raw(37.4, 127.1, 37.47, 127.03) == {'min': 23, 'walk': 311}


def test_odsay_raw_near_code_cached_and_failure_not_cached(monkeypatch):
    monkeypatch.setattr(fl, 'ODSAY_KEY', 'k')
    monkeypatch.setattr(fl.urllib.request, 'urlopen', fake_urlopen({'error': {'code': '-98'}}))
    assert fl.odsay_raw(1, 2, 3, 4) == {'min': None, 'near': 1}
    assert len(os.listdir(fl.ODSAY_CACHE)) == 1
    monkeypatch.setattr(fl.urllib.request, 'urlopen', fake_urlopen(RuntimeError('429')))
    assert fl.odsay_raw(5, 6, 7, 8) == {'min': None}
    assert len(os.listdir(fl.ODSAY_CACHE)) == 1      # 일시 실패는 캐시 안 함 → 다음 실행 재시도


def test_odsay_raw_unexpected_response_not_cached(monkeypatch):
    monkeypatch.setattr(fl, 'ODSAY_KEY', 'k')
    monkeypatch.setattr(fl.urllib.request, 'urlopen', fake_urlopen({'error': {'code': '-8'}}))
    assert fl.odsay_raw(1, 2, 3, 4) == {'min': None}
    assert os.listdir(fl.ODSAY_CACHE) == []


def test_odsay_raw_corrupt_cache_falls_through(monkeypatch):
    os.makedirs(fl.ODSAY_CACHE)
    p = f'{fl.ODSAY_CACHE}/1_2__3_4.json'
    open(p, 'w').write('{broken')
    assert fl.odsay_raw(1, 2, 3, 4) == {'min': None}


def test_transit_walk_to_shuttle_beats_office_walk():
    t = fl.transit(*fl.SHUTTLE['양재숲셔틀'])
    assert t == {'tmin': 0, 'tmode': '양재숲셔틀앞 도보', 'ttype': 'walk', 'tdist': 0}


def test_transit_uses_odsay_when_far(monkeypatch):
    monkeypatch.setattr(fl, 'odsay_raw', lambda *a: {'min': 17})
    t = fl.transit(*fl.STATIONS['판교'])
    assert t['tmin'] == 17 and t['ttype'] == 'transit' and t['tmode'] == '양재숲셔틀'


def test_transit_unresolved_when_odsay_fails(monkeypatch):
    monkeypatch.setattr(fl, 'odsay_raw', lambda *a: {'min': None})
    assert fl.transit(*fl.STATIONS['판교']) == {'tmin': None, 'tmode': None, 'ttype': None, 'tdist': None}


# ---------- 직방 수집 ----------

def test_collect_ids_dedupes_and_skips_failures(monkeypatch):
    calls = []
    def fake_get(url):
        calls.append(url)
        if 'villas' in url: raise RuntimeError('down')
        return {'items': [{'id': 10, 'lat': 1, 'lng': 2}, {'itemId': 11, 'lat': 3, 'lng': 4}, {'lat': 0}]}
    monkeypatch.setattr(fl, 'get', fake_get)
    seen = fl.collect_ids()
    assert seen == {10: {'lat': 1, 'lng': 2}, 11: {'lat': 3, 'lng': 4}}
    assert all('salesTypes' not in u for u in calls)   # NOTES 2: 무필터 수집


def test_fetch_detail_maps_fields(monkeypatch):
    item = {'salesType': '월세', 'price': {'deposit': 1000, 'rent': 70}, 'area': {'전용면적M2': 33.1},
            'floor': {'floor': '3', 'allFloors': '5'}, 'manageCost': {'amount': 7}, 'serviceType': '빌라',
            'roomType': '투룸', 'jibunAddress': '서초구 양재동', 'approveDate': '2001', 'title': 't',
            'imageThumbnail': 'img', 'pnu': 'p', 'randomLocation': {'lat': 37.4, 'lng': 127.0}}
    monkeypatch.setattr(fl, 'get', lambda url: {'item': item})
    r = fl.fetch_detail(5)
    assert r['id'] == 5 and r['sales'] == '월세' and r['rent'] == 70 and r['m2'] == 33.1
    assert r['addr'] == '서초구 양재동' and r['floors'] == '5' and r['manage'] == 7 and r['lat'] == 37.4


def test_fetch_detail_retries_then_gives_up(monkeypatch):
    n = []
    def flaky(url):
        n.append(1)
        raise RuntimeError('x')
    monkeypatch.setattr(fl, 'get', flaky)
    assert fl.fetch_detail(5, retries=3) is None and len(n) == 3


def test_fetch_detail_zero_retries():
    assert fl.fetch_detail(5, retries=0) is None


def test_fetch_detail_recovers_on_retry(monkeypatch):
    n = []
    def flaky(url):
        n.append(1)
        if len(n) == 1: raise RuntimeError('x')
        return {'salesType': '전세'}       # item 래핑 없는 응답도 받음
    monkeypatch.setattr(fl, 'get', flaky)
    assert fl.fetch_detail(5)['sales'] == '전세'


def test_link_falls_back_to_oneroom():
    assert fl.link({'svc': '오피스텔', 'id': 3}).endswith('/officetel/items/3')
    assert fl.link({'svc': '알수없음', 'id': 3}).endswith('/oneroom/items/3')


def test_probe_status(monkeypatch):
    monkeypatch.setattr(fl, 'get', lambda url: {'item': {'status': 'close'}})
    assert fl.probe_status(1) == 'close'
    monkeypatch.setattr(fl, 'get', lambda url: {'item': {}})
    assert fl.probe_status(1) == 'unknown'
    def http(code):
        def _g(url): raise urllib.error.HTTPError(url, code, 'x', {}, None)
        return _g
    monkeypatch.setattr(fl, 'get', http(404)); assert fl.probe_status(1) == 'deleted'
    monkeypatch.setattr(fl, 'get', http(503)); assert fl.probe_status(1) == 'http503'
    monkeypatch.setattr(fl, 'get', lambda url: 1 / 0); assert fl.probe_status(1) == 'unknown'


def test_get_parses_json(monkeypatch):
    monkeypatch.setattr(fl.urllib.request, 'urlopen', fake_urlopen({'a': 1}))
    assert fl.get('http://x') == {'a': 1}


# ---------- 실거래 ----------

class FixedDT(datetime):
    @classmethod
    def now(cls, tz=None): return datetime(2026, 2, 10, tzinfo=tz)


def test_recent_months_wraps_year(monkeypatch):
    monkeypatch.setattr(fl, 'datetime', FixedDT)
    assert fl.recent_months(3) == ['202602', '202601', '202512']


def test_num():
    assert fl._num('1,200') == 1200 and fl._num(' ') == 0 and fl._num(None) == 0 and fl._num(7) == 7


def test_rt_load_cache_and_fresh(monkeypatch):
    payload = {'response': {'body': {'items': {'item': {'deposit': '1'}}}}}   # 단건은 dict 로 옴
    n = []
    def fake_get(url): n.append(url); return payload
    monkeypatch.setattr(fl, 'get', fake_get)
    assert fl.rt_load('A', 'a', '11680', '202601', fresh=set()) == [{'deposit': '1'}]
    assert fl.rt_load('A', 'a', '11680', '202601', fresh=set()) == [{'deposit': '1'}]   # 과거월 = 캐시
    assert len(n) == 1
    fl.rt_load('A', 'a', '11680', '202601', fresh={'202601'})                           # 최근월 = 재조회
    assert len(n) == 2


def test_rt_load_empty_and_errors(monkeypatch):
    monkeypatch.setattr(fl, 'get', lambda url: {'response': {'body': {'items': ''}}})
    assert fl.rt_load('A', 'a', 'g', '1', fresh=set()) == []
    monkeypatch.setattr(fl, 'get', lambda url: {'response': {'body': {'items': {'item': [{'x': 1}, {'x': 2}]}}}})
    assert len(fl.rt_load('A', 'a', 'g', '2', fresh=set())) == 2
    monkeypatch.setattr(fl, 'get', lambda url: 1 / 0)
    assert fl.rt_load('A', 'a', 'g', '3', fresh=set()) == []
    open(f'{fl.RT_CACHE}/a_g_4.json', 'w').write('{broken')
    assert fl.rt_load('A', 'a', 'g', '4', fresh=set()) == []      # 깨진 캐시는 재조회로 덮음


def test_rt_bls(monkeypatch):
    monkeypatch.setattr(fl, 'get', lambda url: {'rts': [1]})
    assert fl.rt_bls('p') == [1]
    monkeypatch.setattr(fl, 'get', lambda url: 1 / 0)
    assert fl.rt_bls('p') == []


PNU = '1168010100107410033'   # 역삼동 741-33


def deal(dep, rent=0, area=33.0, jibun='741-33', **kw):
    return {'umdNm': '역삼동', 'jibun': jibun, 'deposit': f'{dep:,}', 'monthlyRent': str(rent),
            'excluUseAr': str(area), 'dealYear': 2026, 'dealMonth': 9, **kw}


@pytest.fixture
def realprice(monkeypatch):
    """rt_load 를 api 별 가짜 데이터로. 월이 12개라 첫 달에만 데이터를 줌."""
    data = {'offi': [], 'rh': [], 'sh': []}
    bls = {}
    first_month = {}
    def fake_rt_load(api, short, gu, ym, fresh):
        first_month.setdefault('ym', ym)
        return data[short] if (gu == '11680' and ym == first_month['ym']) else []
    monkeypatch.setattr(fl, 'rt_load', fake_rt_load)
    monkeypatch.setattr(fl, 'rt_bls', lambda pnu: bls.get(pnu, []))
    return data, bls


def test_realprice_officetel_same_building_same_area(realprice):
    data, _ = realprice
    data['offi'] = [deal(30000, area=33.5, offiNm='역삼오피'), deal(28000, area=32.0, offiNm='역삼오피'),
                    deal(5000, rent=100, area=33.0)]           # 월세 거래는 전세 비교에서 빠짐
    r = row(svc='오피스텔', pnu=PNU, deposit=31000)
    fl.attach_realprice([r])
    rt = r['rt']
    assert rt['tier'] == 'building' and rt['bldg'] == '역삼오피' and rt['n'] == 2
    assert rt['med'] == 29000 and rt['diff'] == 7 and rt['pct'] == 100
    assert rt['hist'][0]['ym'] == '26.09'


def test_realprice_villa_bls_other_area(realprice):
    _, bls = realprice
    bls[PNU] = [{'sales_type': '월세', '보증금액': '1,000', '월세금액': '50', '전용면적': '45', '거래일': '2026-08-01'}]
    r = row(sales='월세', deposit=1000, rent=60, pnu=PNU)
    fl.attach_realprice([r])
    assert r['rt']['tier'] == 'building-any' and r['rt']['bldg'] is None
    assert r['rt']['med'] == round(1000 + 50 * 12 / fl.RT_CONV)
    assert r['rt']['hist'][0]['ym'] == '26.08' and r['rt']['hist'][0]['k'] == 202608


def test_realprice_villa_rh_fallback_when_no_bls(realprice):
    data, _ = realprice
    data['rh'] = [deal(20000, area=33.0, mhouseNm='역삼빌')]
    r = row(pnu=PNU)
    fl.attach_realprice([r])
    assert r['rt']['tier'] == 'building' and r['rt']['bldg'] == '역삼빌'


@pytest.mark.parametrize('areas,tier', [([33, 34, 36], 'area'), ([33, 44, 46], 'area-wide'), ([33, 60], 'dong')])
def test_realprice_dong_fallback_tiers(realprice, areas, tier):
    data, _ = realprice
    data['sh'] = [deal(20000 + i, area=a, jibun='1') for i, a in enumerate(areas)]
    r = row()                                     # pnu 없음 → 건물 비교 불가
    fl.attach_realprice([r])
    assert r['rt']['tier'] == tier


def test_realprice_trims_outliers_and_skips_unknown(realprice):
    data, _ = realprice
    data['sh'] = [deal(v, area=33) for v in [1, 20000, 20000, 20000, 20000, 20000, 20000, 20000, 20000, 999999]]
    data['sh'].append({'umdNm': '역삼동', 'deposit': '0', 'monthlyRent': '0', 'excluUseAr': '33', '거래일': 'x'})
    r = row()
    no_gu = row(id=2, addr='성남시 분당구 정자동')
    no_addr = row(id=3, addr=None)
    no_pool = row(id=4, sales='월세', rent=50)    # 월세 거래가 하나도 없음
    fl.attach_realprice([r, no_gu, no_addr, no_pool])
    assert r['rt']['n'] == 10 and r['rt']['lo'] == 20000 and r['rt']['hi'] == 20000   # 상하위 10% 트림
    assert r['rt']['hist'][-1]['ym'] == '?'
    assert no_gu['rt'] is None and no_addr['rt'] is None and no_pool['rt'] is None


def test_realprice_all_zero_values_gives_no_stat(realprice):
    data, _ = realprice
    data['sh'] = [{'umdNm': '역삼동', 'deposit': '0', 'monthlyRent': '0', 'excluUseAr': 'bad'}]
    r = row()
    fl.attach_realprice([r])
    assert r['rt'] is None


# ---------- 이력(체류·재등록) ----------

def write_snap(d, name, items, gz=False):
    path = d / name
    body = json.dumps({'snapshot': name, 'items': items}, ensure_ascii=False)
    if gz:
        with gzip.open(str(path) + '.gz', 'wt', encoding='utf-8') as f: f.write(body)
    else:
        path.write_text(body, encoding='utf-8')


def filler(n, start=100000):
    return [row(id=start + k, m2=50 + k / 10, addr='채움동') for k in range(n)]


def test_attach_history_reregistration_chain(tmp_path):
    a = row(id=1, deposit=30000)
    b = row(id=2, deposit=29000)                  # 같은 지문, 새 id = 재등록 인하
    write_snap(tmp_path, '2026-09-01-0700.json', [a] + filler(600), gz=True)
    write_snap(tmp_path, '2026-09-02-0700.json', [a] + filler(600))
    c = row(id=3, deposit=28000)
    fl.attach_history([b, c], '2026-09-10', str(tmp_path))
    assert b['rereg'] == 1 and b['first'] == '2026-09-01' and b['lc'] is True and b['days'] == 0
    assert c['rereg'] == 0 and c['first'] == '2026-09-10'    # 선행 하나는 후속 하나에만 배정


def test_attach_history_skips_partial_and_corrupt_snapshots(tmp_path):
    write_snap(tmp_path, '2026-09-01-0700.json', [row(id=1)])          # 600 미만 = 부분 수집
    (tmp_path / '2026-09-02-0700.json').write_text('{broken')
    r = row(id=1)
    fl.attach_history([r], '2026-09-05', str(tmp_path))
    assert r['first'] == '2026-09-05' and r['days'] == 0


def test_attach_history_counts_post_shrink_snapshots(tmp_path):
    """546a6c5 회귀: 10/5 역 축소 후 ~430건 스냅샷이 600 임계로 통째 빠지던 버그."""
    write_snap(tmp_path, '2026-10-06-1100.json', [row(id=1)] + filler(430))
    write_snap(tmp_path, '2026-09-30-0700.json', [row(id=9)] + filler(430, 200000))   # 축소 전엔 600 필요
    r, s = row(id=1), row(id=9, m2=99)
    fl.attach_history([r, s], '2026-10-08', str(tmp_path))
    assert r['first'] == '2026-10-06' and r['days'] == 2
    assert s['first'] == '2026-10-08'


def test_attach_history_does_not_link_concurrent_listings(tmp_path):
    """NOTES 10: 이틀 이상 동시에 살아 있던 같은 지문은 다른 호실 — 잇지 않음."""
    write_snap(tmp_path, '2026-09-01-0700.json', [row(id=1)] + filler(600))
    write_snap(tmp_path, '2026-09-02-0700.json', [row(id=1), row(id=2)] + filler(600))
    write_snap(tmp_path, '2026-09-03-0700.json', [row(id=1), row(id=2)] + filler(600))
    r = row(id=2)
    fl.attach_history([r], '2026-09-04', str(tmp_path))
    assert r['rereg'] == 0 and r['first'] == '2026-09-02' and r['lc'] is False


def test_load_snap_accepts_bare_list(tmp_path):
    p = tmp_path / 'x.json'; p.write_text('[{"id": 1}]')
    assert fl._load_snap(str(p)) == [{'id': 1}]


# ---------- 작은 헬퍼 ----------

def test_fingerprint_ignores_price_and_id():
    assert fl.fingerprint(row(id=1, deposit=1)) == fl.fingerprint(row(id=2, deposit=2))
    assert fl.fingerprint(row(approve=None))[-1] == ''


def test_brief_commute_text():
    assert fl.brief(row(tmin=12, tmode='양재셔틀'))['commute'] == '12분 양재셔틀'
    assert fl.brief(row(cd=300, cv='강남셔틀'))['commute'] == '300m 강남셔틀'
    assert fl.brief(row())['commute'] is None
    assert fl.brief(row(floor='2', floors='4'))['floor'] == '2/4'


@pytest.mark.parametrize('kw,expect', [
    ({'room': '복층형원룸'}, True), ({'title': '복층 오피스텔'}, True), ({'title': '복층X 일반형'}, False),
    ({'title': '복층 아님'}, False), ({'title': None}, False)])
def test_is_duplex(kw, expect):
    assert fl.is_duplex(row(**kw)) is expect


def test_won_short():
    assert fl.won_short({'deposit': 25000, 'rent': 0}, '전세') == '2.50억'
    assert fl.won_short({'deposit': 30000, 'rent': 0}, '전세') == '3억'
    assert fl.won_short({'deposit': 2000, 'rent': 70}, '월세') == '2000만/70'


# ---------- 대시보드 HTML ----------

def report(**kw):
    r = {'baseline': False, 'new': [1], 'removed': [], 'price_changed': [], 'prev_snapshot': 'P',
         'total_by_sales': {'전세': 1, '월세': 1}}
    r.update(kw)
    return r


def card(html, iid):
    i = html.index(f'data-id="{iid}"')
    return html[html.rindex('<a class="card', 0, i):html.index('</a>', i)]


def test_build_html_cards_and_flags():
    rows = [
        row(id=1, _status='new', tmin=7, tmode='양재셔틀', ttype='walk', tdist=480, manage=10, img='http://i',
            title='풀옵션 리모델링', days=3, rereg=2, first='2026-09-01', lc=True,
            lat=fl.STATIONS['양재'][0], lng=fl.STATIONS['양재'][1],
            rt={'tier': 'building', 'diff': 12, 'n': 3, 'med': 18000, 'bldg': '<b>빌', 'pct': 10,
                'hist': [{'ym': '26.09', 'k': 1, 'v': 18000, 'd': 18000, 'r': 0}]}),
        row(id=2, sales='월세', deposit=1000, rent=60, manage=5, _status='changed', _before='1000만/70',
            rt={'tier': 'area', 'diff': 0, 'n': 5, 'med': 1, 'pct': 80}, lat=fl.STATIONS['판교'][0],
            lng=fl.STATIONS['판교'][1], approve=None),
        row(id=3, room='오픈형원룸', floor='반지하', days=0, lat=None,
            rt={'tier': 'building-any', 'diff': -20, 'n': 1, 'med': 25000}),
        row(id=4, title='복층', rt={'tier': 'dong', 'diff': 0, 'n': 9, 'med': 1, 'pct': 30}),
        row(id=5, svc='오피스텔', rt={'tier': 'building', 'diff': 0, 'n': 5, 'med': 20000}),
    ]
    html = fl.build_html(rows, report(), '2026-10-08-0700')
    c1, c2, c3, c4 = (card(html, i) for i in (1, 2, 3, 4))
    assert '🆕 신규' in c1 and 'class="card new"' in c1 and '양재역' in c1
    assert 'data-burden="60"' in c1                 # 월세 0 + 관리비 10 + 2억×3%÷12 50
    assert '&lt;b&gt;빌' in c1 and '+12%' in c1 and 'rt hi' in c1 and 'class="spark"' in c1
    assert '♻️ 재등록 2회 (≥09/01~)' in c1 and '⏱ 체류 3일' in c1 and 'data-kw="리모델링,풀옵션"' in c1
    assert '(480m)' in c1 and "?w=280" in c1
    assert '💰 1000만/70 → 변동' in c2 and 'hidden' in c2 and '월 60만' in c2 and '상위 20%' in c2
    assert 'data-stn="판교"' in c2 and 'data-yr="0"' in c2
    assert 'hidden' in c3 and 'data-bsmt="1"' in c3 and '타평형' in c3 and 'rt lo' in c3 and '통근 미확정' in c3
    assert 'data-stn="기타"' in c3 and '⏱ 체류 0일' in c3
    assert 'data-dup="1"' in c4 and 'hidden' in c4 and '하위 30%' in c4
    # 탭 숫자 = 기본 필터(오픈형·반지하·복층 숨김) 반영: 전세 1·5만 남음, 월세 1
    assert '전세 2</button>' in html and '월세 1</button>' in html
    assert '🏢 오피스텔 1' in html and '🏠 빌라·원룸 1' in html
    # 신분당 남쪽은 기본 off, 서울 역은 on
    assert 'class="stnchip" data-stn="판교"' in html and 'class="stnchip on" data-stn="양재"' in html
    assert '"1": {"h": [["26.09", 18000, 18000, 0]], "m": 20000, "diff": 12}' in html
    assert '신규 1 · ❌ 빠짐 0' in html


def test_build_html_burden_formula():
    html = fl.build_html([row(id=7, sales='월세', deposit=2000, rent=70, manage=3)], report(), 't')
    assert f'data-burden="{round(70 + 3 + 2000 * fl.BURDEN_RATE / 12)}"' in html   # 78만


def test_build_html_baseline_and_no_regions():
    html = fl.build_html([row(id=1, lat=None, deposit=0)], report(baseline=True), 't')
    assert '기준점 설정' in html and 'class="regions"' not in html


# ---------- main (수집 → diff → 스냅샷 → 대시보드) ----------

@pytest.fixture
def pipeline(monkeypatch, tmp_path):
    snap = tmp_path / 'snaps'; snap.mkdir()
    dash = tmp_path / 'out' / 'index.html'
    monkeypatch.setattr(fl, 'SNAP_DIR', str(snap))
    monkeypatch.setattr(fl, 'DASHBOARD', str(dash))
    monkeypatch.setattr(fl, 'attach_realprice', lambda rows: [r.setdefault('rt', None) for r in rows])
    monkeypatch.setattr(fl, 'transit', lambda lat, lng: {'tmin': 10, 'tmode': 'x', 'ttype': 'transit', 'tdist': 1})
    monkeypatch.setattr(fl, 'probe_status', lambda iid: 'close')
    lat, lng = fl.STATIONS['양재']
    seen = {1: {'lat': lat, 'lng': lng}, 2: {'lat': lat, 'lng': lng}, 3: {'lat': lat, 'lng': lng},
            4: {'lat': 37.6, 'lng': 127.0},           # 37.5 이북 → 제외
            5: {'lat': None, 'lng': None}, 6: {'lat': lat, 'lng': lng}, 7: {'lat': lat, 'lng': lng},
            8: {'lat': lat, 'lng': lng}}
    details = {1: row(id=1, lat=lat, lng=lng), 2: row(id=2, m2=40, lat=lat, lng=lng, deposit=25000),
               3: row(id=3, sales='매매', lat=lat, lng=lng), 6: row(id=6, m2=20, lat=lat, lng=lng),
               7: None, 8: row(id=8, m2=41, lat=37.55, lng=lng)}
    monkeypatch.setattr(fl, 'collect_ids', lambda: seen)
    monkeypatch.setattr(fl, 'fetch_detail', lambda iid: details.get(iid))
    monkeypatch.setattr(fl, 'MAX_DIST', 1e9)
    return snap, dash


def run_main(capsys):
    fl.main()
    return json.loads(capsys.readouterr().out)


def test_main_baseline(pipeline, capsys):
    snap, dash = pipeline
    rep = run_main(capsys)
    assert rep['baseline'] is True and rep['total'] == 2 and rep['total_by_sales'] == {'전세': 2, '월세': 0}
    assert dash.exists() and len(list(snap.glob('*.json'))) == 1


def test_main_diff_new_removed_changed(pipeline, capsys):
    snap, dash = pipeline
    lat, lng = fl.STATIONS['양재']
    prev = [row(id=1, lat=lat, lng=lng),                              # 그대로
            row(id=20, m2=40, lat=lat, lng=lng, deposit=27000),        # 지문 같고 가격 다름 = 2번으로 변동
            row(id=21, m2=77, lat=lat, lng=lng)]                       # 사라짐
    write_snap(snap, '2000-01-01-0000.json', prev)
    rep = run_main(capsys)
    assert rep['baseline'] is False and rep['prev_snapshot'] == '2000-01-01-0000.json'
    assert [c['id'] for c in rep['price_changed']] == [2]
    assert rep['price_changed'][0]['before'] == {'deposit': 27000, 'rent': 0}
    assert rep['new'] == [] and [x['id'] for x in rep['removed']] == [21]
    assert rep['removed'][0]['status'] == 'close' and rep['unchanged'] == 1
    assert '💰 2.70억 → 변동' in dash.read_text(encoding='utf-8')


def test_main_marks_new(pipeline, capsys):
    snap, _ = pipeline
    write_snap(snap, '2000-01-01-0000.json', [row(id=1, lat=fl.STATIONS['양재'][0], lng=fl.STATIONS['양재'][1])])
    rep = run_main(capsys)
    assert [c['id'] for c in rep['new']] == [2] and rep['removed'] == []


def test_main_aborts_on_empty_collection(pipeline, monkeypatch):
    monkeypatch.setattr(fl, 'collect_ids', lambda: {})
    with pytest.raises(SystemExit, match='0 items'):
        fl.main()
    assert list(pipeline[0].iterdir()) == []                 # NOTES 3: 빈 스냅샷을 쓰지 않음


def test_main_survives_history_and_dashboard_errors(pipeline, monkeypatch, capsys):
    monkeypatch.setattr(fl, 'attach_history', lambda rows, today: 1 / 0)
    monkeypatch.setattr(fl, 'build_html', lambda *a: 1 / 0)
    rep = run_main(capsys)
    assert 'ZeroDivisionError' in rep['history_error'] and 'ZeroDivisionError' in rep['dashboard_error']
    assert len(list(pipeline[0].glob('*.json'))) == 1        # 부가정보 실패에도 스냅샷은 남음


def test_main_logs_odsay_progress(pipeline, monkeypatch, capsys):
    monkeypatch.setattr(fl, 'ODSAY_KEY', 'k')
    lat, lng = fl.STATIONS['양재']
    many = {i: {'lat': lat, 'lng': lng} for i in range(30)}
    monkeypatch.setattr(fl, 'collect_ids', lambda: many)
    monkeypatch.setattr(fl, 'fetch_detail', lambda iid: row(id=iid, m2=30 + iid, lat=lat, lng=lng))
    fl.main()
    assert '통근시간 25/30' in capsys.readouterr().err


def test_key_loaders(monkeypatch, tmp_path):
    monkeypatch.setenv('MOLIT_KEY', ' m ')
    monkeypatch.setenv('ODSAY_KEY', ' o ')
    assert fl._molit_key() == 'm' and fl._odsay_key() == 'o'
    monkeypatch.delenv('MOLIT_KEY'); monkeypatch.delenv('ODSAY_KEY')
    monkeypatch.setattr(fl, '_HERE', str(tmp_path))
    assert fl._molit_key() == '' and fl._odsay_key() == ''
    (tmp_path / '.molit_key').write_text('mk\n'); (tmp_path / '.odsay_key').write_text('ok\n')
    assert fl._molit_key() == 'mk' and fl._odsay_key() == 'ok'
