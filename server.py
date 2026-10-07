"""포트폴리오 서버: Open DART 프록시 + Gemini 기업 분석 / 맞춤 자기소개서
실행: python server.py  (또는 start.bat 더블클릭)  ->  http://localhost:8000
키는 같은 폴더의 .env 에서만 읽습니다. (DART_API_KEY, GEMINI_API_KEY, 선택: GEMINI_MODEL, PORT)
외부 패키지 없이 Python 3.8+ 로 동작합니다.
"""
import io, json, os, re, sys, threading, time, zipfile, webbrowser
import urllib.parse, urllib.request, urllib.error
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, "corp_codes.json")
CACHE_DAYS = 7
DART = "https://opendart.fss.or.kr/api/"
GEMINI = "https://generativelanguage.googleapis.com/v1beta/"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# DART가 돌려주는 상태 코드를 사람이 읽을 수 있는 원인으로 바꿉니다.
DART_STATUS = {
    "010": "등록되지 않은 인증키입니다. opendart.fss.or.kr 의 '인증키 신청/관리'에서 키를 다시 확인하세요.",
    "011": "사용할 수 없는 인증키입니다. (일시 정지되었거나 아직 승인 전일 수 있습니다)",
    "012": "이 PC의 IP에서는 접근할 수 없습니다. 인증키 신청 때 IP를 제한했는지 확인하세요.",
    "013": "DART에 해당 데이터가 없습니다.",
    "020": "오늘 요청 한도를 넘었습니다. 내일 다시 시도하세요.",
    "100": "요청 값이 잘못되었습니다.",
    "800": "DART가 점검 중입니다. 잠시 후 다시 시도하세요.",
    "900": "DART에서 알 수 없는 오류가 발생했습니다.",
}


def load_env():
    for name in (".env", ".env.txt", ".envux"):  # 메모장으로 저장하면 .env.txt 가 되는 경우가 많음
        p = os.path.join(ROOT, name)
        if os.path.isfile(p):
            for line in open(p, encoding="utf-8-sig"):
                k, _, v = line.strip().partition("=")
                if k and not k.startswith("#"):
                    os.environ[k.strip()] = re.sub(r"[\s​﻿]", "", v).strip("\"'")
            return name
    return None


ENV_FILE = load_env()
DART_KEY = os.environ.get("DART_API_KEY", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "")


def hide(s):
    """오류 메시지에 키가 섞여 나가지 않도록 가립니다."""
    s = str(s)
    for k in (DART_KEY, GEMINI_KEY):
        if k:
            s = s.replace(k, "***")
    return s


def net_error(e, who):
    if isinstance(e, urllib.error.HTTPError):
        return "%s 서버가 오류를 돌려줬습니다 (HTTP %s)." % (who, e.code)
    if isinstance(e, (urllib.error.URLError, TimeoutError, OSError)):
        return "%s 서버에 연결하지 못했습니다. 인터넷 연결, 방화벽, 백신 프로그램을 확인하세요. (%s)" % (who, hide(getattr(e, "reason", e)))
    return hide(e)


# ---------------------------------------------------------------- DART
def dart_raw(path, **params):
    q = urllib.parse.urlencode({"crtfc_key": DART_KEY, **params})
    last = None
    for _ in range(2):  # 일시적인 끊김은 한 번 더 시도
        try:
            with urllib.request.urlopen(urllib.request.Request(DART + path + "?" + q, headers=UA), timeout=60) as r:
                return r.read()
        except Exception as e:
            last = e
            time.sleep(1)
    raise RuntimeError(net_error(last, "DART"))


def dart_json(path, **params):
    j = json.loads(dart_raw(path, **params).decode("utf-8"))
    st = j.get("status", "000")
    if st != "000":
        raise RuntimeError(DART_STATUS.get(st, j.get("message", "DART 오류")) + " [코드 %s]" % st)
    return j


def _num(v):
    """'1,234' -> 1234.0, '-' 나 빈 값 -> None"""
    m = re.search(r"-?\d+(?:\.\d+)?", str(v or "").replace(",", ""))
    return float(m.group()) if m else None


def _tenure(v):
    """'12.5' 또는 '12년 6개월' -> 12.5"""
    s = str(v or "")
    m = re.search(r"(\d+)\s*년\s*(\d+)\s*개월", s)
    if m:
        return int(m.group(1)) + int(m.group(2)) / 12
    m = re.search(r"(\d+)\s*개월", s)
    return int(m.group(1)) / 12 if m else _num(s)


def _won(v):
    """회사마다 원·천원·백만원으로 적는 단위가 달라서, 연봉 범위를 기준으로 원 단위로 맞춥니다."""
    if v is None or v <= 0:
        return None
    return v * 1000000 if v < 10000 else v * 1000 if v < 10000000 else v


def employees(corp_code):
    """가장 최근 사업보고서의 직원 현황(직원 수, 평균 근속연수, 1인 평균 급여)을 요약합니다."""
    year = time.localtime().tm_year
    rows, used = None, None
    for y in range(year - 1, year - 4, -1):
        j = json.loads(dart_raw("empSttus.json", corp_code=corp_code, bsns_year=str(y), reprt_code="11011").decode("utf-8"))
        st = j.get("status")
        if st == "000" and j.get("list"):
            rows, used = j["list"], y
            break
        if st != "013":  # 013(데이터 없음)만 이전 연도로 넘어가고, 나머지는 오류로 알림
            raise RuntimeError(DART_STATUS.get(st, j.get("message", "DART 오류")) + " [코드 %s]" % st)
    if not rows:
        raise RuntimeError("최근 3년 사업보고서에 직원 현황이 없습니다. (사업보고서를 내지 않는 비상장사일 수 있습니다)")
    is_total = lambda r: any(k in (r.get("fo_bbm") or "") + (r.get("sexdstn") or "") for k in ("합계", "총계")) or (r.get("sexdstn") or "").strip() == "계"
    part = [r for r in rows if not is_total(r)] or rows  # 합계 행이 섞여 있으면 중복 계산을 막기 위해 제외
    total = regular = contract = 0
    t_sum = t_w = pay_sum = s_sum = s_w = 0.0
    for r in part:
        n = _num(r.get("sm")) or 0
        total += n
        regular += _num(r.get("rgllbr_co")) or 0
        contract += _num(r.get("cnttk_co")) or 0
        t = _tenure(r.get("avrg_cnwk_sdytrn"))
        if t is not None and n:
            t_sum += t * n; t_w += n
        s = _won(_num(r.get("jan_salary_am")))
        if s and n:
            s_sum += s * n; s_w += n
    return {
        "year": used, "stlm_dt": rows[0].get("stlm_dt", ""), "rcept_no": rows[0].get("rcept_no", ""),
        "total": int(total) or None, "regular": int(regular) or None, "contract": int(contract) or None,
        "tenure": round(t_sum / t_w, 1) if t_w else None,
        "salary": int(round(s_sum / s_w, -4)) if s_w else None,  # 인원 가중 평균, 만원 단위로 반올림
    }


_corps, _lock = None, threading.Lock()


def read_cache():
    try:
        data = json.load(open(CACHE, encoding="utf-8"))
        return data if isinstance(data, list) and data else None
    except Exception:
        return None


def corps():
    """회사명 -> 고유번호 목록. 7일마다 새로 받고, 받지 못하면 예전 캐시를 그대로 씁니다."""
    global _corps
    with _lock:
        if _corps is not None:
            return _corps
        old = read_cache()
        if old and time.time() - os.path.getmtime(CACHE) < CACHE_DAYS * 86400:
            _corps = old
            return _corps
        try:
            print(">> [DART] 기업 고유번호 목록을 내려받는 중... (처음 한 번, 최대 1분)")
            raw = dart_raw("corpCode.xml")
            try:
                z = zipfile.ZipFile(io.BytesIO(raw))
            except zipfile.BadZipFile:  # 키 문제면 ZIP 대신 XML 오류가 옴
                txt = raw.decode("utf-8", "ignore")
                m = re.search(r"<status>(\d+)</status>", txt)
                code = m.group(1) if m else ""
                raise RuntimeError(DART_STATUS.get(code, re.sub(r"<[^>]+>", " ", txt).strip()) + (" [코드 %s]" % code if code else ""))
            out = []
            for _, e in ET.iterparse(z.open(z.namelist()[0])):
                if e.tag == "list":
                    name, code = e.findtext("corp_name") or "", e.findtext("corp_code") or ""
                    if name and code:
                        out.append({"corp_code": code, "corp_name": name, "stock_code": (e.findtext("stock_code") or "").strip()})
                    e.clear()
            tmp = CACHE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False)
            os.replace(tmp, CACHE)
            print(">> [DART] %d개 기업 저장 완료" % len(out))
            _corps = out
        except Exception as e:
            if not old:
                raise
            print(">> [DART] 새 목록을 받지 못해 기존 캐시를 사용합니다:", hide(e))
            _corps = old
        return _corps


# ---------------------------------------------------------------- Gemini
def gemini_http(path, payload=None, timeout=60):
    req = urllib.request.Request(
        GEMINI + path,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_KEY, **UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        try:
            msg = json.loads(body)["error"]["message"]
        except Exception:
            msg = body[:300]
        raise RuntimeError("Gemini 오류 (HTTP %s): %s" % (e.code, hide(msg)))
    except Exception as e:
        raise RuntimeError(net_error(e, "Gemini"))


_models = None


def gemini_models():
    """이 키로 실제 쓸 수 있는 모델을 물어봐서 고릅니다. (모델 이름을 코드에 고정하지 않음)"""
    global _models
    if _models is None:
        names = []
        for m in gemini_http("models?pageSize=200", timeout=30).get("models", []):
            n = m.get("name", "").replace("models/", "")
            if "generateContent" in m.get("supportedGenerationMethods", []) and n.startswith("gemini"):
                names.append(n)
        skip = ("image", "tts", "audio", "live", "embedding", "vision", "robotics", "computer")
        names = [n for n in names if not any(s in n for s in skip)]

        def rank(n):  # 안정판 flash 우선, 그다음 버전이 높은 순
            ver = [int(x) for x in re.findall(r"\d+", n)[:2]] + [0, 0]
            return ("flash" not in n, "lite" in n, "preview" in n or "exp" in n, -ver[0], -ver[1], n)

        _models = sorted(names, key=rank)
    return _models


def gemini_text(res):
    parts = ((res.get("candidates") or [{}])[0].get("content") or {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts).strip()


def ask_gemini(prompt, search=False):
    if not GEMINI_KEY:
        raise RuntimeError(".env 파일에 GEMINI_API_KEY가 없습니다.")
    tried = ([GEMINI_MODEL] if GEMINI_MODEL else []) + [m for m in gemini_models() if m != GEMINI_MODEL]
    if not tried:
        raise RuntimeError("이 Gemini 키로 사용할 수 있는 모델이 없습니다.")
    last = ""
    for model in tried[:3]:
        for with_search in ([True, False] if search else [False]):
            body = {"contents": [{"parts": [{"text": prompt}]}]}
            if with_search:
                body["tools"] = [{"google_search": {}}]
            try:
                text = gemini_text(gemini_http("models/%s:generateContent" % model, body, timeout=90))
                if text:
                    return text, model, with_search
                last = "%s: 빈 응답" % model
            except Exception as e:
                last = "%s: %s" % (model, hide(e))
    raise RuntimeError(last)


def insight_prompt(name):
    return """기업 '%s'에 대해 신입 개발자 취업 관점에서 최신 정보를 정리해줘.
1. 주요 서비스와 핵심 사업
2. 채용공고·기술 블로그에서 확인되는 개발 기술 스택
3. 개발 문화와 인재상
4. 지원할 때 강조하면 좋은 역량
확인된 사실 위주로 간결한 마크다운 불릿으로 쓰고, 확인되지 않은 내용은 추측하지 말고 '확인 필요'라고 적어줘.""" % name


def cover_letter_prompt(p):
    u, t = p.get("user", {}), p.get("target", {})
    lines = lambda items, fmt: "\n".join(fmt(x) for x in items) or "- (없음)"
    return """너는 IT 분야 채용 서류를 돕는 자기소개서 컨설턴트야.
아래 [지원자 자료]에 적힌 사실만 근거로 '%(corp)s' %(role)s 직무 맞춤 자기소개서를 써줘.

[목표 기업]
- 회사명: %(corp)s
- 지원 직무: %(role)s
- 요구 기술: %(req)s
- 기업 정보: %(info)s

[지원자 자료]
- 이름: %(name)s
- 소개: %(intro)s
- 보유 기술: %(skills)s
- 학력·이력:
%(history)s
- 자격증:
%(certs)s
- 프로젝트:
%(projects)s
- 지원자가 직접 쓴 자기소개:
%(about)s

[작성 규칙]
1. 자료에 없는 경험, 수치, 수상, 기술은 절대 지어내지 말 것. 요구 기술 중 지원자가 갖추지 못한 것은 보유한 척하지 말고 학습 계획으로 쓸 것.
2. 지원자가 직접 쓴 자기소개의 표현과 말투를 최대한 살릴 것.
3. 아래 세 항목으로 나누고 각 항목에 '### ' 소제목을 붙일 것.
   ### 1. 지원 동기 및 입사 후 포부 (약 500자)
   ### 2. 직무 역량과 문제 해결 경험 (약 700자)
   ### 3. 협업과 성장 자세 (약 400자)
""" % dict(
        corp=t.get("c", "목표 기업"), role=t.get("r") or "개발자", req=", ".join(t.get("q", [])), info=t.get("info", ""),
        name=u.get("name", "지원자"), intro=u.get("intro", ""), skills=", ".join(u.get("skills", [])),
        history=lines(u.get("history", []), lambda h: "- %s (%s): %s" % (h.get("t"), h.get("d"), h.get("n", ""))),
        certs=lines(u.get("certs", []), lambda c: "- %s (%s, %s)" % (c.get("t"), c.get("o", ""), c.get("d", ""))),
        projects=lines(u.get("projects", []), lambda x: "- %s | 기술: %s | 내용: %s | AI 협업: %s" % (x.get("t"), ", ".join(x.get("s", [])), x.get("m", ""), x.get("a", ""))),
        about=lines(u.get("about", []), lambda a: "- %s: %s" % (a.get("q"), a.get("a"))),
    )


# ---------------------------------------------------------------- 배포(공개 서버) 보호
PUBLIC = bool(os.environ.get("RENDER") or os.environ.get("PUBLIC"))  # Render는 RENDER=true 를 자동으로 넣어줌
LIMITS = {"gemini": (8, 3600), "dart": (60, 600)}  # 방문자 1명(IP)당 (횟수, 초). 내 API 한도를 남이 다 쓰지 못하게 함
_hits, _hits_lock = {}, threading.Lock()


def allow(ip, kind):
    n, window = LIMITS[kind]
    now = time.time()
    with _hits_lock:
        q = [t for t in _hits.get((ip, kind), []) if now - t < window]
        ok = len(q) < n
        if ok:
            q.append(now)
        _hits[(ip, kind)] = q
        if len(_hits) > 5000:
            _hits.clear()
        return ok


# ---------------------------------------------------------------- 상태 점검
_status_cache = (0, None)


def status():
    """결과를 10분간 기억해서, 페이지를 열 때마다 DART·Gemini를 호출하지 않게 합니다."""
    global _status_cache
    if time.time() - _status_cache[0] < 600 and _status_cache[1]:
        return _status_cache[1]
    out = _status()
    if out["dart"].get("ok") and out["gemini"].get("ok"):
        _status_cache = (time.time(), out)
    return out


def _status():
    out = {"env_file": ENV_FILE, "dart": {}, "gemini": {}, "cache": {}}
    if not DART_KEY:
        out["dart"] = {"ok": False, "msg": ".env 파일에 DART_API_KEY가 없습니다." if ENV_FILE else "server.py 와 같은 폴더에 .env 파일이 없습니다."}
    elif not re.fullmatch(r"[0-9a-fA-F]{40}", DART_KEY):
        out["dart"] = {"ok": False, "msg": "DART 인증키는 40자리여야 하는데 지금은 %d자리입니다. .env 의 키를 다시 붙여넣으세요." % len(DART_KEY)}
    else:
        try:
            j = dart_json("company.json", corp_code="00126380")  # 삼성전자로 실제 호출
            out["dart"] = {"ok": True, "msg": "정상 (시험 조회: %s)" % j.get("corp_name", "")}
        except Exception as e:
            out["dart"] = {"ok": False, "msg": hide(e)}
    if not GEMINI_KEY:
        out["gemini"] = {"ok": False, "msg": ".env 파일에 GEMINI_API_KEY가 없습니다. (기업 분석·자기소개서 생성에만 필요)"}
    else:
        try:
            models = gemini_models()  # 글을 생성하지 않고 모델 목록만 물어봐서 한도를 쓰지 않음
            if not models:
                raise RuntimeError("이 키로 사용할 수 있는 Gemini 모델이 없습니다.")
            out["gemini"] = {"ok": True, "msg": "정상 (모델: %s)" % (GEMINI_MODEL or models[0])}
        except Exception as e:
            out["gemini"] = {"ok": False, "msg": hide(e)}
    c = read_cache() if _corps is None else _corps
    out["cache"] = {"count": len(c) if c else 0,
                    "age_days": round((time.time() - os.path.getmtime(CACHE)) / 86400, 1) if os.path.exists(CACHE) else None}
    return out


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("   %s\n" % hide(fmt % args))

    def send_body(self, body, ctype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, obj, code=200):
        self.send_body(json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", code)

    def client_ip(self):
        fwd = self.headers.get("X-Forwarded-For", "") if PUBLIC else ""  # Render는 프록시 뒤에서 동작
        return fwd.split(",")[0].strip() or self.client_address[0]

    def limited(self, kind):
        if allow(self.client_ip(), kind):
            return False
        self.send_json({"error": "요청이 너무 많습니다. 잠시 후 다시 시도하세요."}, 429)
        return True

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            if u.path == "/healthz":
                return self.send_body(b"ok", "text/plain")
            if u.path == "/portfolio.json":  # 'JSON 백업'으로 받은 파일을 같은 폴더에 두면 방문자에게 그 내용이 보임
                p = os.path.join(ROOT, "portfolio.json")
                if not os.path.isfile(p):
                    return self.send_json({"error": "portfolio.json 없음"}, 404)
                with open(p, "rb") as f:
                    return self.send_body(f.read(), "application/json; charset=utf-8")
            if u.path.startswith("/api/dart/") and self.limited("dart"):
                return
            if u.path.startswith("/api/gemini/") and self.limited("gemini"):
                return
            if u.path in ("/", "/index.html"):  # index.html 하나만 제공 (.env 등 다른 파일은 절대 내보내지 않음)
                with open(os.path.join(ROOT, "index.html"), "rb") as f:
                    return self.send_body(f.read(), "text/html; charset=utf-8")
            if u.path == "/api/status":
                return self.send_json(status())
            if u.path == "/api/dart/search":
                if not DART_KEY:
                    return self.send_json({"error": ".env 파일에 DART_API_KEY가 없습니다."}, 500)
                s = q.get("q", "").strip().lower()
                hit = [c for c in corps() if s and s in c["corp_name"].lower()]
                hit.sort(key=lambda c: (not c["stock_code"], not c["corp_name"].lower().startswith(s), len(c["corp_name"])))
                return self.send_json(hit[:12])
            if u.path == "/api/dart/company":
                code = q.get("corp_code", "")
                if not re.fullmatch(r"\d{8}", code):
                    return self.send_json({"error": "고유번호(8자리)가 올바르지 않습니다."}, 400)
                return self.send_json(dart_json("company.json", corp_code=code))
            if u.path == "/api/dart/employees":
                code = q.get("corp_code", "")
                if not code and q.get("name"):  # 예전에 저장한 공고는 회사명만 있으므로 이름으로 찾음
                    name = q["name"].strip()
                    hit = [c for c in corps() if c["corp_name"] == name]
                    hit.sort(key=lambda c: not c["stock_code"])
                    if not hit:
                        return self.send_json({"error": "DART에서 '%s'와 이름이 정확히 같은 회사를 찾지 못했습니다. 위 검색으로 회사를 고른 뒤 다시 저장하세요." % name}, 404)
                    code = hit[0]["corp_code"]
                if not re.fullmatch(r"\d{8}", code):
                    return self.send_json({"error": "고유번호(8자리)가 올바르지 않습니다."}, 400)
                return self.send_json(employees(code))
            if u.path == "/api/gemini/insights":
                name = q.get("corp_name", "").strip()
                if not name:
                    return self.send_json({"error": "회사명이 없습니다."}, 400)
                text, model, searched = ask_gemini(insight_prompt(name), search=True)
                return self.send_json({"corp_name": name, "report": text, "model": model, "searched": searched})
            self.send_json({"error": "없는 주소입니다."}, 404)
        except FileNotFoundError:
            self.send_body("index.html 파일이 server.py 와 같은 폴더에 없습니다.".encode("utf-8"), "text/plain; charset=utf-8", 404)
        except Exception as e:
            self.send_json({"error": hide(e)}, 502)

    def do_POST(self):
        try:
            if urllib.parse.urlparse(self.path).path != "/api/gemini/cover-letter":
                return self.send_json({"error": "없는 주소입니다."}, 404)
            if self.limited("gemini"):
                return
            n = int(self.headers.get("Content-Length") or 0)
            if n > 200000:
                return self.send_json({"error": "요청이 너무 큽니다."}, 413)
            body = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
            text, model, _ = ask_gemini(cover_letter_prompt(body))
            self.send_json({"cover_letter": text, "model": model})
        except Exception as e:
            self.send_json({"error": hide(e)}, 502)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    url = "http://localhost:%d" % port
    print("=" * 60)
    print(" 포트폴리오 서버  %s" % url)
    print(" 설정 파일 : %s" % (ENV_FILE or "없음 (.env 파일을 이 폴더에 만드세요)"))
    print(" DART 키   : %s" % ("있음 (%d자리)" % len(DART_KEY) if DART_KEY else "없음"))
    print(" Gemini 키 : %s" % ("있음" if GEMINI_KEY else "없음"))
    print(" 끝내려면 이 창에서 Ctrl+C")
    print("=" * 60)
    try:
        srv = ThreadingHTTPServer(("0.0.0.0" if PUBLIC else "127.0.0.1", port), Handler)
    except OSError:
        print("\n[오류] %d번 포트를 이미 다른 프로그램이 쓰고 있습니다." % port)
        print("이전에 켜 둔 server.py 창이 있으면 닫고 다시 실행하세요.")
        sys.exit(1)
    if not PUBLIC:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n서버를 종료했습니다.")
