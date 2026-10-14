"""
과거 데이터로 '기본 확률표(base rates)'를 만드는 스크립트.

  python baserates.py            # 최근 약 3년치

한국투자증권 API에서 코스피·코스닥·보유종목 일봉, 미국 지수(필라델피아반도체·S&P500·나스닥)·환율 일봉,
코스피 시장 투자자별(외국인) 순매수를 받아서
  data/history/*.json   : 원본 일별 종가
  data/base_rates.json  : "이런 조건이면 다음날 이렇게 됐다" 확률표 (아침 브리핑이 읽음)
을 만든다. 조회 전용(주문 API 없음). 키·토큰 처리와 오류 기록은 collect.py 것을 그대로 쓴다.
"""
import datetime as dt
import json
import math
import sys
from pathlib import Path

sys.argv = [sys.argv[0], "baserates"]  # collect.py의 SESSION 자동판단을 막기 위한 값
import collect as C  # noqa: E402

YEARS = 3
START = (C.NOW - dt.timedelta(days=365 * YEARS + 10)).strftime("%Y%m%d")
TODAY = C.NOW.strftime("%Y%m%d")

DOMESTIC_INDEX = {"0001": "코스피", "1001": "코스닥"}
DOMESTIC_STOCK = {"005930": "삼성전자", "000660": "SK하이닉스", "005380": "현대차"}
OVERSEAS = [("N", "SOX", "필라델피아반도체"), ("N", "SPX", "S&P500"), ("N", "COMP", "나스닥종합"),
            ("X", "FX@KRW", "원/달러 환율")]


def _ymd_minus1(d):
    return (dt.datetime.strptime(d, "%Y%m%d") - dt.timedelta(days=1)).strftime("%Y%m%d")


def paged(fetch, label):
    """최근→과거 방향으로 끝날짜를 당겨가며 START까지 반복 조회. fetch(end) -> [(date, row)]"""
    out, end, guard = {}, TODAY, 0
    while end >= START and guard < 60:
        guard += 1
        try:
            rows = fetch(end)
        except Exception as e:
            C.log_err(f"history {label} ~{end}", e)
            break
        rows = [(d, r) for d, r in rows if d and START <= d <= end]
        if not rows:
            break
        for d, r in rows:
            out[d] = r
        oldest = min(d for d, _ in rows)
        if oldest >= end:
            break
        end = _ymd_minus1(oldest)
    return dict(sorted(out.items()))


def f(x):
    try:
        v = float(str(x).replace(",", ""))
        return v if math.isfinite(v) else None
    except Exception:
        return None


def get_index(code, name):
    def fetch(end):
        j = C.kis_get("/uapi/domestic-stock/v1/quotations/inquire-daily-indexchartprice", "FHKUP03500100",
                      {"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": code, "FID_INPUT_DATE_1": START,
                       "FID_INPUT_DATE_2": end, "FID_PERIOD_DIV_CODE": "D"})
        return [(r.get("stck_bsop_date"), {"open": f(r.get("bstp_nmix_oprc")), "close": f(r.get("bstp_nmix_prpr"))})
                for r in (j.get("output2") or [])]
    return paged(fetch, name)


def get_stock(code, name):
    def fetch(end):
        j = C.kis_get("/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice", "FHKST03010100",
                      {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code, "FID_INPUT_DATE_1": START,
                       "FID_INPUT_DATE_2": end, "FID_PERIOD_DIV_CODE": "D", "FID_ORG_ADJ_PRC": "0"})
        return [(r.get("stck_bsop_date"), {"open": f(r.get("stck_oprc")), "close": f(r.get("stck_clpr"))})
                for r in (j.get("output2") or [])]
    return paged(fetch, name)


def get_overseas(mkt, code, name):
    def fetch(end):
        j = C.kis_get("/uapi/overseas-price/v1/quotations/inquire-daily-chartprice", "FHKST03030100",
                      {"FID_COND_MRKT_DIV_CODE": mkt, "FID_INPUT_ISCD": code, "FID_INPUT_DATE_1": START,
                       "FID_INPUT_DATE_2": end, "FID_PERIOD_DIV_CODE": "D"})
        return [(r.get("stck_bsop_date"), {"close": f(r.get("ovrs_nmix_prpr"))}) for r in (j.get("output2") or [])]
    return paged(fetch, name)


INVESTOR_KEYS_SEEN = []


def get_kospi_investor():
    """코스피 시장 외국인 순매수(일별). 응답 필드명을 확실히 모르므로 후보 이름을 차례로 찾는다."""
    def fetch(end):
        j = C.kis_get("/uapi/domestic-stock/v1/quotations/inquire-investor-daily-by-market", "FHPTJ04040000",
                      {"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": "0001", "FID_INPUT_DATE_1": end,
                       "FID_INPUT_ISCD_1": "KSP", "FID_INPUT_DATE_2": end, "FID_INPUT_ISCD_2": "0001"})
        rows = j.get("output") or j.get("output1") or []
        if isinstance(rows, dict):
            rows = [rows]
        if rows and not INVESTOR_KEYS_SEEN:
            INVESTOR_KEYS_SEEN.extend(sorted(rows[0].keys()))
        out = []
        for r in rows:
            v = None
            for k in ("frgn_ntby_tr_pbmn", "frgn_ntby_qty", "frgn_reg_ntby_tr_pbmn", "frgn_nreg_ntby_tr_pbmn"):
                if k in r and f(r[k]) is not None:
                    v = f(r[k])
                    break
            out.append((r.get("stck_bsop_date") or r.get("bsop_date"), {"frgn_net": v}))
        return out
    return paged(fetch, "코스피 투자자")


# ---------------- 통계 ----------------
def returns(series):
    """{date: {close}} → [(date, ret%, prev_date)]"""
    ds = [d for d in series if series[d].get("close")]
    out = []
    for a, b in zip(ds, ds[1:]):
        p, c = series[a]["close"], series[b]["close"]
        if p:
            out.append((b, (c / p - 1) * 100, a))
    return out


def overnight(us_ret, kr_date):
    """한국 거래일 D 아침에 알 수 있는 가장 최근 미국 종가 등락(미국 날짜 < D)."""
    cands = [d for d in us_ret if d < kr_date]
    return us_ret[max(cands)] if cands else None


def stat(rows):
    """rows: [kospi_ret...] → 상승확률 등"""
    n = len(rows)
    if n == 0:
        return {"n": 0}
    up = sum(1 for r in rows if r > 0)
    return {"n": n, "p_up": round(up / n * 100, 1), "mean": round(sum(rows) / n, 2),
            "p_abs_ge1": round(sum(1 for r in rows if abs(r) >= 1) / n * 100, 1)}


def bucket(x, edges):
    for lo, hi, lab in edges:
        if lo <= x < hi:
            return lab
    return None


SOX_EDGES = [(-99, -3, "≤-3%"), (-3, -1.5, "-3~-1.5%"), (-1.5, -0.5, "-1.5~-0.5%"), (-0.5, 0.5, "-0.5~+0.5%"),
             (0.5, 1.5, "+0.5~+1.5%"), (1.5, 3, "+1.5~+3%"), (3, 99, "≥+3%")]
KR_EDGES = [(-99, -3, "≤-3%"), (-3, -1, "-3~-1%"), (-1, 0, "-1~0%"), (0, 1, "0~+1%"), (1, 3, "+1~+3%"), (3, 99, "≥+3%")]


def build(h):
    kospi = h["index"]["코스피"]
    k_ret = {d: r for d, r, _ in returns(kospi)}
    k_dates = sorted(k_ret)
    us = {name: {d: r for d, r, _ in returns(h["overseas"][name])} for name in ("필라델피아반도체", "S&P500")
          if h["overseas"].get(name)}
    stk = {name: {d: r for d, r, _ in returns(h["stock"][name])} for name in h["stock"]}
    res = {"period": f"{k_dates[0] if k_dates else '?'}~{k_dates[-1] if k_dates else '?'}",
           "kospi_days": len(k_dates), "tables": {}, "headline": []}
    T = res["tables"]

    def tbl(name, groups, note):
        T[name] = {"note": note, "rows": {g: stat(v) for g, v in groups.items()}}

    # 1) 밤사이 반도체지수 구간 → 코스피 당일
    for usname in us:
        g, g_hynix, g_sam, gap, intraday = {}, {}, {}, {}, {}
        hit = tot = 0
        for d in k_dates:
            o = overnight(us[usname], d)
            if o is None:
                continue
            b = bucket(o, SOX_EDGES)
            g.setdefault(b, []).append(k_ret[d])
            if "SK하이닉스" in stk and d in stk["SK하이닉스"]:
                g_hynix.setdefault(b, []).append(stk["SK하이닉스"][d])
            if "삼성전자" in stk and d in stk["삼성전자"]:
                g_sam.setdefault(b, []).append(stk["삼성전자"][d])
            op, cl = kospi[d].get("open"), kospi[d].get("close")
            if op and cl:
                intraday.setdefault(b, []).append((cl / op - 1) * 100)
            if o != 0 and k_ret[d] != 0:
                tot += 1
                hit += (o > 0) == (k_ret[d] > 0)
        key = "SOX" if usname == "필라델피아반도체" else "SPX"
        tbl(f"{key}밤사이→코스피", g, f"밤사이 {usname} 등락 구간별 그날 코스피 종가 등락")
        tbl(f"{key}밤사이→SK하이닉스", g_hynix, f"밤사이 {usname} 구간별 SK하이닉스")
        tbl(f"{key}밤사이→삼성전자", g_sam, f"밤사이 {usname} 구간별 삼성전자")
        tbl(f"{key}밤사이→코스피 장중(종가/시가)", intraday,
            "p_up = 시가보다 종가가 높게 끝난 비율. 갭으로 출발한 뒤 장중에 되돌리는지 확인용")
        if tot:
            res[f"rule_{key}_direction_hit"] = {"n": tot, "hit_pct": round(hit / tot * 100, 1),
                                                "note": f"'코스피 방향 = 밤사이 {usname} 방향' 단순 규칙의 과거 적중률"}

    # 2) 코스피 전일 등락 구간 → 다음날
    g, prev = {}, None
    for d in k_dates:
        if prev is not None:
            g.setdefault(bucket(k_ret[prev], KR_EDGES), []).append(k_ret[d])
        prev = d
    tbl("코스피전일→다음날", g, "코스피 전일 등락 구간별 다음날 코스피")

    # 3) 연속 하락/상승 일수 → 다음날
    gd, gu, streak = {}, {}, 0
    for i, d in enumerate(k_dates):
        if i > 0 and streak != 0:
            key = f"{min(abs(streak), 5)}{'+' if abs(streak) >= 5 else ''}일"
            (gd if streak < 0 else gu).setdefault(key, []).append(k_ret[d])
        r = k_ret[d]
        streak = (streak - 1 if streak < 0 else -1) if r < 0 else ((streak + 1 if streak > 0 else 1) if r > 0 else 0)
    tbl("코스피연속하락→다음날", gd, "코스피 n일 연속 하락 다음날 (5+는 5일 이상)")
    tbl("코스피연속상승→다음날", gu, "코스피 n일 연속 상승 다음날")

    # 4) 외국인 연속 순매도 일수 → 다음날 코스피
    inv = h.get("investor") or {}
    inv_dates = [d for d in sorted(inv) if inv[d].get("frgn_net") is not None]
    if len(inv_dates) > 30:
        g, streak = {}, 0
        for i, d in enumerate(inv_dates):
            nxt = next((x for x in k_dates if x > d), None)
            v = inv[d]["frgn_net"]
            streak = (streak - 1 if streak < 0 else -1) if v < 0 else ((streak + 1 if streak > 0 else 1) if v > 0 else 0)
            if nxt and nxt in k_ret:
                key = (f"순매도 {min(-streak, 5)}{'+' if -streak >= 5 else ''}일째" if streak < 0
                       else f"순매수 {min(streak, 5)}{'+' if streak >= 5 else ''}일째" if streak > 0 else "0")
                g.setdefault(key, []).append(k_ret[nxt])
        tbl("외국인연속순매도→다음날코스피", g, "코스피 시장 외국인 순매수 부호 연속일수별 다음날 코스피")
    else:
        res["investor_note"] = f"외국인 일별 데이터 부족({len(inv_dates)}일) — 응답 필드: {INVESTOR_KEYS_SEEN[:40]}"

    # 5) 환율: 전일 방향 지속 비율, 하루 변동 0.3% 미만 비율
    fx = h["overseas"].get("원/달러 환율") or {}
    fxr = returns(fx)
    if len(fxr) > 30:
        same = sum(1 for (a, b) in zip(fxr, fxr[1:]) if a[1] != 0 and b[1] != 0 and (a[1] > 0) == (b[1] > 0))
        tot = sum(1 for (a, b) in zip(fxr, fxr[1:]) if a[1] != 0 and b[1] != 0)
        small = sum(1 for _, r, _ in fxr if abs(r) < 0.3)
        res["원달러"] = {"n": len(fxr), "전일방향지속_pct": round(same / tot * 100, 1) if tot else None,
                      "하루변동0.3%미만_pct": round(small / len(fxr) * 100, 1)}

    # 6) 최근 60거래일만 따로 (국면 변화 확인용)
    recent = k_dates[-60:]
    if us.get("필라델피아반도체"):
        g = {}
        for d in recent:
            o = overnight(us["필라델피아반도체"], d)
            if o is not None:
                g.setdefault(bucket(o, SOX_EDGES), []).append(k_ret[d])
        tbl("최근60일 SOX밤사이→코스피", g, "최근 60거래일만 — 전체 기간과 다르면 국면이 바뀐 것")
    res["코스피_전체"] = stat([k_ret[d] for d in k_dates])
    res["코스피_최근60일"] = stat([k_ret[d] for d in recent])

    # 사람이 바로 읽는 요약
    hl = res["headline"]
    hl.append(f"기간 {res['period']} · 코스피 {len(k_dates)}거래일 · 전체 상승확률 {res['코스피_전체'].get('p_up')}%"
              f" · 최근60일 {res['코스피_최근60일'].get('p_up')}%")
    if "rule_SOX_direction_hit" in res:
        hl.append(f"'코스피 방향 = 밤사이 SOX 방향' 과거 적중률 {res['rule_SOX_direction_hit']['hit_pct']}%"
                  f" (n={res['rule_SOX_direction_hit']['n']})")
    for k in ("≤-3%", "≥+3%"):
        r = T.get("SOX밤사이→코스피", {}).get("rows", {}).get(k)
        if r and r.get("n"):
            hl.append(f"SOX {k} 다음날 코스피 상승확률 {r['p_up']}% (평균 {r['mean']}%, n={r['n']})")
    for k in ("3일", "4일", "5+일"):
        r = T.get("코스피연속하락→다음날", {}).get("rows", {}).get(k)
        if r and r.get("n"):
            hl.append(f"코스피 {k} 연속하락 다음날 상승확률 {r['p_up']}% (n={r['n']})")
    if "원달러" in res:
        hl.append(f"원/달러 전일방향 지속 {res['원달러']['전일방향지속_pct']}% · 하루 0.3% 미만 변동 {res['원달러']['하루변동0.3%미만_pct']}%")
    return res


def main():
    if not (C.KIS_KEY and C.KIS_SECRET):
        print("KIS 키 없음")
        return
    C._token = C.kis_token()
    h = {"index": {}, "stock": {}, "overseas": {}, "investor": {}}
    for code, name in DOMESTIC_INDEX.items():
        h["index"][name] = get_index(code, name)
    for code, name in DOMESTIC_STOCK.items():
        h["stock"][name] = get_stock(code, name)
    for mkt, code, name in OVERSEAS:
        h["overseas"][name] = get_overseas(mkt, code, name)
    try:
        h["investor"] = get_kospi_investor()
    except Exception as e:
        C.log_err("history investor", e)

    hist = Path("data") / "history"
    hist.mkdir(parents=True, exist_ok=True)
    (hist / "daily.json").write_text(json.dumps(h, ensure_ascii=False), encoding="utf-8")

    res = {"generated_at": C.NOW.isoformat(timespec="minutes"),
           "counts": {k: {n: len(v) for n, v in h[k].items()} if isinstance(h[k], dict) and k != "investor"
                      else len(h[k]) for k in h}}
    try:
        res.update(build(h))
    except Exception as e:
        C.log_err("base rates build", e)
    res["errors"] = C.errors
    res["how_to_read"] = ("p_up=상승 마감 비율(%), mean=평균 등락률(%), p_abs_ge1=|등락률|≥1% 비율(%), n=표본 수. "
                          "n<20이면 참고만. '밤사이'=한국 거래일 아침 전에 끝난 가장 최근 미국장.")
    text = json.dumps(res, ensure_ascii=False, indent=1)
    for s in (C.KIS_KEY, C.KIS_SECRET, C.DART_KEY, C._token):
        if s:
            text = text.replace(s, "***")
    (Path("data") / "base_rates.json").write_text(text, encoding="utf-8")
    print("saved base_rates:", res.get("counts"), "errors=", len(C.errors))
    for line in res.get("headline", []):
        print(" ", line)
    for e in C.errors:
        print(" -", e["where"], ":", e["error"])


if __name__ == "__main__":
    main()
