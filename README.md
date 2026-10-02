# market-data

매일 장전(06:40)·장후(16:40)에 한국투자증권 Open API와 DART에서 시장 숫자를 받아 `data/` 폴더에 저장합니다.
Claude 데일리 브리핑이 이 파일을 읽어서 예측·채점에 씁니다.

- 시세 **조회**만 합니다. 주문·계좌 API는 코드에 없습니다.
- API 키는 GitHub Secrets에만 저장되고, 결과 파일과 로그에서는 자동으로 가려집니다.
- 저장되는 것: 지수, 추적 종목 시세·일별 종가·투자자별 순매수, 등락률 순위, 해외 지수·환율, 주요 공시 목록.
- 추적 종목을 바꾸려면 `collect.py`의 `WATCH`만 수정하면 됩니다.

## 설정 (처음 한 번)

1. 이 저장소를 Public으로 만들고 파일을 모두 올립니다.
2. Settings → Secrets and variables → Actions → New repository secret 에서 3개 등록
   - `KIS_APP_KEY` — 한국투자증권 App Key
   - `KIS_APP_SECRET` — 한국투자증권 App Secret
   - `DART_API_KEY` — DART 인증키
3. Actions 탭 → market-data → Run workflow → session에 `morning` 입력 → 실행
4. 1~2분 뒤 `data/latest_morning.json`이 생기면 성공. `errors` 항목에 실패한 부분이 기록됩니다.
