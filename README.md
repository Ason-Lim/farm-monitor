# 농축수산 가격 모니터 (비공개)

- robots.txt·봇 차단·로그인 벽을 우회하지 않고, 막힌 사이트는 상태만 기록합니다.
- 접속은 HTTP Basic 인증(APP_USER / APP_PASS)이며 APP_PASS가 비어 있으면 모두 거부합니다.
- 로컬 실행: `APP_PASS=비밀번호 python app.py` → http://localhost:5000
- Render 배포: GitHub에 올린 뒤 Blueprint로 render.yaml을 연결하고 APP_PASS를 입력합니다.
- 수집 대상은 sites.json에서 수정합니다(홈페이지 주소가 있는 26곳).
