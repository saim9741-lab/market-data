#!/usr/bin/env python3
"""
장전/장후 시장 데이터 수집기 (한국투자증권 Open API + DART Open API)

- 시세 '조회' API만 사용합니다. 주문/계좌 관련 API는 이 코드에 없습니다.
- API 키는 GitHub Secrets(환경변수)로만 받고, 결과 파일과 에러 메시지에서 자동으로 가립니다.
- 결과:
    data/latest_morning.json  /  data/latest_evening.json   (가장 최근 1회분)
    data/archive/YYYY-MM-DD_morning.json ...                 (날짜별 보관 — 나중에 과거 학습용)

사용: python collect.py morning   |   python collect.py evening
"""
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

import requests

KST = dt.timezone(dt.timedelta(hours=9))
NOW = dt.datetime.now(KST)
SESSION = sys.argv[1] if len(sys.argv) > 1 else ("morning" if NOW.hour < 12 else "evening")

KIS_BASE = os.environ.get("KIS_BASE_URL", "").strip() or "https://openapi.koreainvestment.com:9443"  # 실전투자
KIS_KEY = os.environ.get("KIS_APP_KEY", "").strip()
KIS_SECRET = os.environ.get("KIS_APP_SECRET", "").strip()
DART_KEY = os.environ.get("DART_API_KEY", "").strip()

# 항상 추적할 종목 (보유종목). 바꾸려면 여기만 수정.
WATCH = {
    "005930": "삼성전자",
    "000660": "SK하이닉스",
    "005380": "현대차",
    "360750": "TIGER 미국S&P500",
}
INDICES = {"0001": "코스피", "1001": "코스닥", "2001": "코스피200"}

# 해외 지수/환율 (한투 해외 기간별시세). 코드가 틀리면 errors에 기록되고 나머지는 계속 진행.
OVERSEAS = [
    ("N", "SPX", "S&P500"),
    ("N", "COMP", "나스닥종합"),
    ("N", ".DJI", "다우"),
    ("N", "SOX", "필라델피아반도체"),
    ("X", "FX@KRW", "원/달러 환율"),
]

# DART 공시 중 주가에 영향 큰 것만 골라내는 키워드
DART_KEYWORDS = [
    "단일판매", "공급계약", "유상증자", "무상증자", "감자", "자기주식", "최대주주", "잠정", "영업실적",
    "합병", "분할", "소송", "거래정지", "상장폐지", "관리종목", "전환사채", "신주인수권", "교환사채",
    "투자판단", "조회공시", "품목허가", "임상", "기술이전", "특허", "투자주의", "투자경고", "투자위험",
]

errors = []
http = requests.Session()
_token = None


def scrub(text: str) -> str:
    """에러 메시지 등에 키가 섞여 나가지 않도록 가림 (공개 저장소라 필수)."""
    s = str(text)
    for secret in (KIS_KEY, KIS_SECRET, DART_KEY, _token):
        if secret:
            s = s.replace(secret, "***")
    return s[:400]


def log_err(where, e):
    errors.append({"where": where, "error": scrub(f"{type(e).__name__}: {e}")})


def pick(d: dict, keys):
    """필요한 필드만 추리고, 없으면 원본 일부를 남겨서 나중에 키 이름을 확인할 수 있게 함."""
    if not isinstance(d, dict):
        return d
    out = {k: d.get(k) for k in keys if k in d}
    return out if out else {k: d[k] for k in list(d)[:25]}


# ---------------- 한국투자증권 ----------------
# 접근 토큰은 1일 1회 발급이 원칙이라(자주 발급하면 이용 제한 + 매번 카톡 알림),
# 발급한 토큰을 암호화해서 GitHub Actions 캐시에 보관하고 20시간 동안 재사용함.
# 캐시 파일은 저장소에 커밋되지 않고(data/ 밖에 있음), App Secret으로 암호화됨.
TOKEN_CACHE = Path(".kis_token.enc")
TOKEN_REUSE_HOURS = 20


def _fernet():
    import base64
    import hashlib

    from cryptography.fernet import Fernet

    key = base64.urlsafe_b64encode(hashlib.sha256(("kis-token-cache:" + KIS_SECRET).encode()).digest())
    return Fernet(key)


def _load_cached_token():
    try:
        if not TOKEN_CACHE.exists():
            return None
        data = json.loads(_fernet().decrypt(TOKEN_CACHE.read_bytes()))
        issued = dt.datetime.fromisoformat(data["issued_at"])
        if NOW - issued < dt.timedelta(hours=TOKEN_REUSE_HOURS):
            return data["token"]
    except Exception as e:
        log_err("kis token cache read", e)
    return None


def _save_cached_token(token):
    try:
        payload = json.dumps({"token": token, "issued_at": NOW.isoformat()}).encode()
        TOKEN_CACHE.write_bytes(_fernet().encrypt(payload))
    except Exception as e:
        log_err("kis token cache write", e)


def kis_token(force_new=False):
    if not force_new:
        cached = _load_cached_token()
        if cached:
            print("kis token: reused cached token")
            return cached
    r = http.post(
        f"{KIS_BASE}/oauth2/tokenP",
        json={"grant_type": "client_credentials", "appkey": KIS_KEY, "appsecret": KIS_SECRET},
        timeout=20,
    )
    r.raise_for_status()
    token = r.json()["access_token"]
    _save_cached_token(token)
    print("kis token: issued new token")
    return token


_token_refreshed = False


def kis_get(path, tr_id, params):
    global _token, _token_refreshed
    for attempt in range(2):
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {_token}",
            "appkey": KIS_KEY,
            "appsecret": KIS_SECRET,
            "tr_id": tr_id,
            "custtype": "P",
        }
        r = http.get(f"{KIS_BASE}{path}", headers=headers, params=params, timeout=20)
        time.sleep(0.15)  # 초당 호출 제한 대비
        try:
            j = r.json()
        except ValueError:
            r.raise_for_status()
            raise
        # 캐시 토큰이 만료·무효면(EGW00121/EGW00123) 딱 한 번만 새로 발급하고 재시도
        if j.get("msg_cd") in ("EGW00121", "EGW00123") and not _token_refreshed and attempt == 0:
            _token_refreshed = True
            _token = kis_token(force_new=True)
            continue
        r.raise_for_status()
        if j.get("rt_cd") != "0":
            raise RuntimeError(f"rt_cd={j.get('rt_cd')} msg_cd={j.get('msg_cd')} msg={j.get('msg1')}")
        return j


def get_indices():
    out = {}
    for code, name in INDICES.items():
        try:
            j = kis_get(
                "/uapi/domestic-stock/v1/quotations/inquire-index-price",
                "FHPUP02100000",
                {"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": code},
            )
            out[name] = pick(j.get("output", {}), [
                "bstp_nmix_prpr", "bstp_nmix_prdy_vrss", "bstp_nmix_prdy_ctrt",
                "acml_vol", "acml_tr_pbmn", "ascn_issu_cnt", "down_issu_cnt", "stnr_issu_cnt",
            ])
        except Exception as e:
            log_err(f"index {name}", e)
    return out


def get_watch():
    out = {}
    for code, name in WATCH.items():
        item = {"name": name}
        try:
            j = kis_get(
                "/uapi/domestic-stock/v1/quotations/inquire-price",
                "FHKST01010100",
                {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code},
            )
            item["quote"] = pick(j.get("output", {}), [
                "stck_prpr", "prdy_vrss", "prdy_ctrt", "acml_vol", "acml_tr_pbmn",
                "hts_avls", "per", "pbr", "w52_hgpr", "w52_lwpr", "frgn_hldn_qty", "hts_frgn_ehrt",
            ])
        except Exception as e:
            log_err(f"quote {name}", e)
        try:
            j = kis_get(
                "/uapi/domestic-stock/v1/quotations/inquire-daily-price",
                "FHKST01010400",
                {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code,
                 "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0"},
            )
            item["daily"] = [pick(r, ["stck_bsop_date", "stck_clpr", "prdy_ctrt", "acml_vol"])
                             for r in (j.get("output") or [])[:20]]
        except Exception as e:
            log_err(f"daily {name}", e)
        try:
            j = kis_get(
                "/uapi/domestic-stock/v1/quotations/inquire-investor",
                "FHKST01010900",
                {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code},
            )
            item["investor"] = [pick(r, ["stck_bsop_date", "prsn_ntby_qty", "frgn_ntby_qty", "orgn_ntby_qty"])
                                for r in (j.get("output") or [])[:5]]
        except Exception as e:
            log_err(f"investor {name}", e)
        out[code] = item
    return out


def get_ranking():
    """등락률 순위 (전체시장). sort 0=상승률, 1=하락률."""
    out = {}
    for label, sort in (("up", "0"), ("down", "1")):
        try:
            j = kis_get(
                "/uapi/domestic-stock/v1/ranking/fluctuation",
                "FHPST01700000",
                {
                    "fid_cond_mrkt_div_code": "J",
                    "fid_cond_scr_div_code": "20170",
                    "fid_input_iscd": "0000",
                    "fid_rank_sort_cls_code": sort,
                    "fid_input_cnt_1": "0",
                    "fid_prc_cls_code": "1",  # 1=전일 종가 대비 (0은 장중 저가/고가 대비라 순위가 이상해짐)
                    "fid_input_price_1": "",
                    "fid_input_price_2": "",
                    "fid_vol_cnt": "",
                    "fid_trgt_cls_code": "0",
                    "fid_trgt_exls_cls_code": "0",
                    "fid_div_cls_code": "0",
                    "fid_rsfl_rate1": "",
                    "fid_rsfl_rate2": "",
                },
            )
            out[label] = [pick(r, ["stck_shrn_iscd", "hts_kor_isnm", "stck_prpr", "prdy_ctrt", "acml_vol"])
                          for r in (j.get("output") or [])[:30]]
        except Exception as e:
            log_err(f"ranking {label}", e)
    return out


def get_overseas():
    out = {}
    end = NOW.strftime("%Y%m%d")
    start = (NOW - dt.timedelta(days=14)).strftime("%Y%m%d")
    for mkt, code, name in OVERSEAS:
        try:
            j = kis_get(
                "/uapi/overseas-price/v1/quotations/inquire-daily-chartprice",
                "FHKST03030100",
                {"FID_COND_MRKT_DIV_CODE": mkt, "FID_INPUT_ISCD": code,
                 "FID_INPUT_DATE_1": start, "FID_INPUT_DATE_2": end, "FID_PERIOD_DIV_CODE": "D"},
            )
            rows = j.get("output2") or []
            out[name] = {
                "summary": pick(j.get("output1", {}), ["ovrs_nmix_prpr", "ovrs_nmix_prdy_vrss", "prdy_ctrt", "hts_kor_isnm"]),
                "daily": [pick(r, ["stck_bsop_date", "ovrs_nmix_prpr", "ovrs_nmix_oprc", "ovrs_nmix_hgpr", "ovrs_nmix_lwpr"])
                          for r in rows[:5]],
            }
        except Exception as e:
            log_err(f"overseas {name}", e)
    return out


# ---------------- DART ----------------
def get_dart():
    # 직전 3일치(주말·휴일 고려)를 받아서 키워드/보유종목으로 거름
    bgn = (NOW - dt.timedelta(days=3)).strftime("%Y%m%d")
    end = NOW.strftime("%Y%m%d")
    items, total = [], 0
    try:
        for page in range(1, 11):
            r = http.get(
                "https://opendart.fss.or.kr/api/list.json",
                params={"crtfc_key": DART_KEY, "bgn_de": bgn, "end_de": end,
                        "page_no": page, "page_count": 100},
                timeout=20,
            )
            r.raise_for_status()
            j = r.json()
            status = j.get("status")
            if status == "013":  # 조회된 데이터 없음
                break
            if status != "000":
                raise RuntimeError(f"DART status={status} msg={j.get('message')}")
            total = j.get("total_count", total)
            items.extend(j.get("list", []))
            if page >= int(j.get("total_page", 1)):
                break
            time.sleep(0.2)
    except Exception as e:
        log_err("dart list", e)

    watch_names = set(WATCH.values())
    picked = []
    for it in items:
        name, report = it.get("corp_name", ""), it.get("report_nm", "")
        if name in watch_names or any(k in report for k in DART_KEYWORDS):
            picked.append({
                "date": it.get("rcept_dt"), "corp": name, "market": it.get("corp_cls"),
                "report": report.strip(), "watch": name in watch_names,
                "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={it.get('rcept_no')}",
            })
    # 시장 구분: Y=유가, K=코스닥, N=코넥스, E=기타 → 상장사(Y/K) 우선
    picked.sort(key=lambda x: (not x["watch"], x["market"] not in ("Y", "K"), x["date"] or ""), reverse=False)
    return {"range": f"{bgn}~{end}", "total_count": total, "filtered_count": len(picked), "filtered": picked[:150]}


def main():
    global _token
    result = {
        "generated_at": NOW.isoformat(timespec="minutes"),
        "session": SESSION,
        "note": ("morning=직전 거래일 장마감 기준 + 밤사이 해외/공시, evening=당일 장마감 기준. "
                 "등락률 단위는 %. KIS 필드: prpr=현재가/종가, prdy_ctrt=전일대비율, ntby_qty=순매수수량."),
    }
    if KIS_KEY and KIS_SECRET:
        try:
            _token = kis_token()
        except Exception as e:
            log_err("kis token", e)
    else:
        errors.append({"where": "kis", "error": "KIS_APP_KEY / KIS_APP_SECRET 시크릿이 비어 있음"})

    if _token:
        result["indices"] = get_indices()
        result["watch"] = get_watch()
        result["ranking"] = get_ranking()
        result["overseas"] = get_overseas()

    if DART_KEY:
        result["dart"] = get_dart()
    else:
        errors.append({"where": "dart", "error": "DART_API_KEY 시크릿이 비어 있음"})

    result["errors"] = errors
    text = json.dumps(result, ensure_ascii=False, indent=1)
    # 공개 저장소이므로 결과 전체에서도 키·토큰 문자열을 한 번 더 제거
    for secret in (KIS_KEY, KIS_SECRET, DART_KEY, _token):
        if secret:
            text = text.replace(secret, "***")

    data = Path("data")
    (data / "archive").mkdir(parents=True, exist_ok=True)
    (data / f"latest_{SESSION}.json").write_text(text, encoding="utf-8")
    (data / "archive" / f"{NOW:%Y-%m-%d}_{SESSION}.json").write_text(text, encoding="utf-8")
    print(f"saved {SESSION}: errors={len(errors)}")
    for e in errors:
        print(" -", e["where"], ":", e["error"])


if __name__ == "__main__":
    main()
