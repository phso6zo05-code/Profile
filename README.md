# CareerMatch 포트폴리오

진로 탐색, 포트폴리오, 기업 매칭을 한 페이지에서 다루는 개인 포트폴리오 사이트입니다.
Open DART에서 기업 개황과 직원 현황을 가져오고, Gemini로 기업 분석과 맞춤 자기소개서 초안을 만듭니다.

## 구성
- `index.html` : 화면 전체 (스킬 매칭, 직무 적합도, 공고 관리)
- `server.py` : 페이지 제공 + DART/Gemini 프록시 (Python 표준 라이브러리만 사용)
- `render.yaml` : Render 배포 설정
- `portfolio.json` : (선택) 방문자에게 보여줄 내용. 페이지 하단 "JSON 백업"으로 받은 파일

## 내 컴퓨터에서 실행
1. 같은 폴더에 `.env` 파일을 만들고 키를 넣습니다.
   ```
   DART_API_KEY=발급받은_키
   GEMINI_API_KEY=발급받은_키
   ```
2. `start.bat` 더블클릭 (또는 `python server.py`) 후 http://localhost:8000 접속

## 배포 (Render)
저장소를 Render에 Blueprint로 연결하고 `DART_API_KEY`, `GEMINI_API_KEY`를 환경 변수로 입력합니다.
키는 저장소에 올리지 않습니다 (`.env`는 `.gitignore`에 포함).
