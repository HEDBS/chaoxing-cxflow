#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""cxflow —— 学习通(超星)作业全链路 CLI：登录 / 读课程任务 / 领卷 / 解题 / 提交 / 风控。

协议来自桌面版助手 v0.4.3 的实测 core/*（登录 AES、stat2 聚合、getAllWork 全扫、领卷链、
addStudentWorkNewWeb 真协议、9010 风控自愈）。纯标准库，无需安装任何东西。

数据目录：默认 %APPDATA%\\cx-pilot（非 Windows 退 ~/cx-pilot；目录名沿用桌面版，CX_DATA 可覆盖）。
与桌面版助手共用同一份 cookies.txt / credentials.json，互不干扰。

命令（全部把结果 JSON 打到 stdout，进度/告警打到 stderr）:
  login   [--user U --pwd P] [--force]        登录并把 cookie 落盘
  tasks   [--refresh] [--limit N]             读临期任务；--refresh 再逐课全扫一次
  pull    --course-id C --class-id K --cpi P --work-id W [--answer-id A] [--out f.json]
                                              领卷 -> 题目 + 提交上下文（只 GET）
  solve   --in f.json [--out a.json] [--base-url ... --model ... --key ...]
                                              解题（默认沿用桌面版助手的后端链配置）
  submit  --in f.json --answers a.json [--confirm]
                                              默认 dry-run 只打印将发送的表单；--confirm 才真提交
  captcha get [--out captcha.png] | captcha post --code 1234
                                              风控【9010】验证码人工出口（取图 / 代提交）
  selftest                                    离线自检（AES 向量 + 卷子解析 + 表单构造）

红线：
  · submit 默认不发送；真提交必须显式 --confirm。
  · 验证码**不自动识别**（那属于绕过人机验证），只做「取图给人看 + 代提交」。
  · 限速：每课扫描间隔 0.6~1.4s，解题每题 1.0~2.5s；风控跟 IP 热度走，别并发放大。
"""
import argparse
import base64
import datetime
import http.cookiejar
import json
import os
import random
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
from aes128 import aes128_cbc_encrypt  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
TRANSFER_KEY = b"u2oh6Vu^HWe4_AES"
MOOC = "https://mooc1.chaoxing.com"
STAT2 = "https://stat2-ans.chaoxing.com"
PASSPORT2 = "https://passport2.chaoxing.com"
LOGIN_URL = PASSPORT2 + "/login?fid=-1&refer=http%3A%2F%2Fi.chaoxing.com"
RISK_COOLDOWN_S = 8


def _data_dir():
    d = os.environ.get("CX_DATA")
    if not d:
        d = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "cx-pilot")
    os.makedirs(d, exist_ok=True)
    return d


DATA = _data_dir()
COOKIE_FILE = os.path.join(DATA, "cookies.txt")
CRED_FILE = os.path.join(DATA, "credentials.json")


def log(msg):
    sys.stderr.write(str(msg) + "\n")
    sys.stderr.flush()


def emit(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=1) + "\n")
    sys.stdout.flush()


class SessionExpired(Exception):
    """会话过期：被弹到登录页 / 返回登录页正文。"""


class RiskControl(Exception):
    """超星风控罚站（【9010】操作异常，请输入图片中的验证码）。

    处置：弃 cookie 全新登录能洗白，但风控跟 **IP 热度**走——刚跑完几十个请求就立刻重登
    会被立刻再拦。所以顺序是：磁盘重载 cookie（零网络）-> 冷却数秒 -> 重登 -> 重试；
    仍失败就取出验证码图交人工。
    """


class ApiError(Exception):
    pass


class NoCredentials(Exception):
    pass


def save_credentials(user, pwd):
    with open(CRED_FILE, "w", encoding="utf-8") as f:
        json.dump({"user": user, "pwd": pwd}, f, ensure_ascii=False)
    try:
        os.chmod(CRED_FILE, 0o600)
    except OSError:
        pass


def load_credentials():
    try:
        with open(CRED_FILE, encoding="utf-8") as f:
            c = json.load(f)
        return c.get("user"), c.get("pwd")
    except Exception:
        return None, None


def aes_encrypt(s):
    ct = aes128_cbc_encrypt(str(s).encode("utf-8"), TRANSFER_KEY, TRANSFER_KEY)
    return base64.b64encode(ct).decode()


def strip_html(s):
    s = re.sub(r"<br\s*/?>", "\n", s or "")
    s = re.sub(r"<[^>]+>", "", s)
    for a, b in (("&nbsp;", " "), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'), ("&amp;", "&")):
        s = s.replace(a, b)
    return re.sub(r"[ \t]+", " ", s).strip()


# ==================== 客户端：登录 + 请求 + 风控 ====================
class Client:
    CAPTCHA_IMG = "https://mooc1.chaoxing.com/processVerifyPng.ac?t=%d"
    CAPTCHA_POST = "https://mooc1.chaoxing.com/html/processVerify.ac"

    def __init__(self):
        os.makedirs(DATA, exist_ok=True)
        self.jar = http.cookiejar.MozillaCookieJar(COOKIE_FILE)
        if os.path.exists(COOKIE_FILE):
            try:
                self.jar.load(ignore_discard=True, ignore_expires=True)
            except Exception:
                pass
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.UA = UA

    # ---------- 底层 ----------
    def raw_get(self, url, referer="https://mooc1.chaoxing.com/", timeout=30, headers=None):
        h = {"User-Agent": UA, "Referer": referer,
             "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8"}
        if headers:
            h.update(headers)
        resp = self.op.open(urllib.request.Request(url, headers=h), timeout=timeout)
        body = resp.read().decode("utf-8", "replace")
        final = ""
        try:
            final = resp.geturl() or ""
        except Exception:
            pass
        # 要害：login() 本身就是去 GET passport2 登录页取表单 token，请求本身就是登录页时不算掉线
        if "passport2.chaoxing.com" not in url:
            if "passport2.chaoxing.com" in final and "/login" in final:
                raise SessionExpired("会话过期：被重定向到登录页 %s" % final[:80])
            if ("passport2.chaoxing.com/login" in body
                    and ("扫码登录" in body or "用户登录" in body or "手机号登录" in body)):
                raise SessionExpired("会话过期：返回登录页正文")
        # 空壳错误页（~820-910B 的「温馨提示/提交失败」）
        if len(body) < 1200 and ("温馨提示" in body or "提交失败" in body or "没有此页面访问权限" in body):
            raise SessionExpired("error page: " + re.sub(r"\s+", " ", strip_html(body))[:80])
        # 风控罚站页（~4527B，正文【9010】+ processVerifyPng 验证码图）
        if "【9010】" in body or ("提示页面" in body and "processVerifyPng" in body):
            m = re.search(r"【(\d+)】([^<\n]{0,80})", body)
            raise RiskControl(m.group(0).strip() if m else "超星风控拦截（需人工过验证码）")
        return body

    def raw_post(self, url, form, referer="https://mooc1.chaoxing.com/", headers=None, timeout=30):
        data = urllib.parse.urlencode(form).encode()
        h = {"User-Agent": UA, "Referer": referer,
             "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
             "Origin": re.match(r"https://[^/]+", url).group(0)}
        if headers:
            h.update(headers)
        return self.op.open(urllib.request.Request(url, data=data, headers=h),
                            timeout=timeout).read().decode("utf-8", "replace")

    def get_json(self, url, referer=None, retries=2):
        last = None
        for i in range(retries + 1):
            try:
                t = self.raw_get(url, referer=referer or STAT2 + "/")
                if t[:1] in "@{":
                    t = t.lstrip("@")          # 超星 ajax 防劫持前缀
                j = json.loads(t)
                if isinstance(j, dict) and j.get("msg") == "未登录":
                    raise SessionExpired("stat2 says 未登录")
                return j
            except (json.JSONDecodeError, urllib.error.URLError) as e:
                last = e
                if i >= retries:
                    break
                time.sleep(1.5 + random.random())
        raise ApiError("%s @ %s" % (last, url[:100]))

    # ---------- 登录 ----------
    def login(self, user=None, pwd=None):
        if user is None and pwd is None:
            user, pwd = load_credentials()
        elif user is not None and pwd is not None:
            save_credentials(user, pwd)
        if not user or not pwd:
            raise NoCredentials("无凭据：先 `login --user 手机号 --pwd 密码`，或写 %s" % CRED_FILE)
        html = self.raw_get(LOGIN_URL)

        def hidden(name, default=""):
            m = (re.search(r'id="%s"[^>]*value="([^"]*)"' % name, html)
                 or re.search(r'value="([^"]*)"[^>]*id="%s"' % name, html))
            return m.group(1) if m else default

        form = {
            "fid": hidden("fid", "-1"),
            "uname": aes_encrypt(user),
            "password": aes_encrypt(pwd),
            "refer": hidden("refer", "http%3A%2F%2Fi.chaoxing.com"),
            "t": hidden("t", "true"),
            "forbidotherlogin": hidden("forbidotherlogin", "0"),
            "validate": hidden("validate", ""),
            "doubleFactorLogin": hidden("doubleFactorLogin", "0"),
            "independentId": hidden("independentId", ""),
            "independentNameId": hidden("independentNameId", ""),
        }
        t = self.raw_post(PASSPORT2 + "/fanyalogin", form, referer=LOGIN_URL,
                          headers={"X-Requested-With": "XMLHttpRequest"})
        j = json.loads(t)
        if not j.get("status"):
            raise ApiError("登录失败: " + str(j.get("msg2") or j.get("msg") or j)[:150])
        self.jar.save(ignore_discard=True, ignore_expires=True)
        return True

    def ensure_login(self):
        """stat2 探活：code==0 即登录态可用；否则重登。RiskControl 也在自愈壳内（否则会漏出去）。"""
        try:
            j = self.get_json_recovering(
                STAT2 + "/stat2/learning/plan/recommended-course-list",
                referer=STAT2 + "/stat2-vue/studyPlanAssistant")
            if j.get("code") == 0:
                return True
        except (SessionExpired, ApiError, RiskControl):
            pass
        return self.login()

    def reload_cookies(self):
        """从磁盘重载 cookie（零网络）；长连接实例被轮换后磁盘态通常仍干净。"""
        self.jar = http.cookiejar.MozillaCookieJar(COOKIE_FILE)
        if os.path.exists(COOKIE_FILE):
            try:
                self.jar.load(ignore_discard=True, ignore_expires=True)
            except Exception:
                pass
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        return True

    def recover_session(self):
        """丢弃 cookie 全新登录（旧 cookie 备份为 cookies.txt.stale）。"""
        try:
            if os.path.exists(COOKIE_FILE):
                try:
                    os.replace(COOKIE_FILE, COOKIE_FILE + ".stale")
                except OSError:
                    os.remove(COOKIE_FILE)
        except OSError:
            pass
        self.jar = http.cookiejar.MozillaCookieJar(COOKIE_FILE)
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        return self.login()

    def _cascade(self, op):
        """自愈级联：①磁盘重载 -> ②冷却 + 重登 -> ③原样抛出。每级只试一次，绝不静默吞错。"""
        last = None
        try:
            return op()
        except (SessionExpired, RiskControl) as e:
            last = e
        self.reload_cookies()
        try:
            return op()
        except (SessionExpired, RiskControl) as e:
            last = e
        time.sleep(RISK_COOLDOWN_S)
        if self.recover_session():
            return op()
        raise last

    def get_json_recovering(self, url, referer=None):
        return self._cascade(lambda: self.get_json(url, referer=referer))

    def raw_get_recovering(self, url, referer="https://mooc1.chaoxing.com/", timeout=30, headers=None):
        return self._cascade(lambda: self.raw_get(url, referer=referer, timeout=timeout, headers=headers))

    # ---------- 风控验证码（人工出口） ----------
    def fetch_captcha(self, timeout=20):
        url = self.CAPTCHA_IMG % random.randint(1, 2147483647)
        h = {"User-Agent": UA, "Referer": "https://mooc1.chaoxing.com/",
             "Accept": "image/*,*/*;q=0.8"}
        resp = self.op.open(urllib.request.Request(url, headers=h), timeout=timeout)
        return resp.read(), (resp.headers.get("Content-Type") or "")

    def submit_captcha(self, code, timeout=25):
        """提交验证码并**回验**（不看响应文案，看课程列表还拦不拦）。"""
        code = re.sub(r"\s+", "", str(code or ""))[:8]
        if not code:
            return False
        data = urllib.parse.urlencode({"app": "0", "ucode": code}).encode()
        h = {"User-Agent": UA, "Referer": "https://mooc1.chaoxing.com/",
             "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
             "Origin": "https://mooc1.chaoxing.com"}
        try:
            self.op.open(urllib.request.Request(self.CAPTCHA_POST, data=data, headers=h),
                         timeout=timeout).read()
        except Exception:
            return False
        try:
            t = self.raw_get(MOOC + "/visit/courses", referer=MOOC + "/")
            return "【9010】" not in t
        except Exception:
            return False


# ==================== 任务：stat2 聚合 + 逐课全扫 ====================
def _ids_from_url(url):
    """courseid/clazzid/cpi 三 id 独立解析（活动类任务 url 常缺 clazzid，不能整体返空）。"""
    u = url or ""

    def one(*keys):
        for k in keys:
            m = re.search(k + r"=(\d+)", u, re.I)
            if m:
                return m.group(1)
        return ""
    return one("courseid", "courseId"), one("clazzid", "clazzid", "classid"), one("cpi")


def stat2_tasks(client):
    """1 个请求拿全部临期任务 + 课程进度（官方数据每 4h 刷新；要实时用 refresh_all）。"""
    j = client.get_json(STAT2 + "/stat2/learning/plan/recommended-course-list",
                        referer=STAT2 + "/stat2-vue/studyPlanAssistant")
    if j.get("code") != 0:
        raise ApiError("stat2 code=%s msg=%s" % (j.get("code"), j.get("msg")))
    d = j["data"]
    tasks = []
    for t in d.get("allNearTasks") or []:
        cid, cls, cpi = _ids_from_url(t.get("url", ""))
        tasks.append({"courseId": cid, "classId": cls, "cpi": cpi,
                      "workId": str(t.get("id", "")), "course": t.get("courseName", ""),
                      "title": t.get("name", ""), "etype": t.get("eventType", ""),
                      "endTime": int(t.get("endTime", 0) or 0), "endDate": t.get("endDate", ""),
                      "remain": t.get("remainTimeStr", ""),
                      "questionNum": int(t.get("questionNum", 0) or 0), "url": t.get("url", "")})
    return {"generated": d.get("cacheGenerateTime", ""), "tasks": tasks}


COURSE_PAT = re.compile(r"<a class=\"courseName\"\s+href='/visit/stucoursemiddle\?courseid=(\d+)"
                        r"&clazzid=(\d+)&vc=\d+&cpi=(\d+)'[^>]*title=\"([^\"]*)\"")
TITLE_EXCLUDES = ("分组任务", "PBL")     # 随堂练习不剔除：很多课的随堂练习本质就是作业


def course_triplets(client):
    t = client.raw_get(MOOC + "/visit/courses", referer=MOOC + "/")
    seen, out = set(), []
    for cid, cls, cpi, name in COURSE_PAT.findall(t):
        if cid in seen:
            continue
        seen.add(cid)
        out.append((cid, cls, cpi, name.replace("&nbsp;", " ")))
    return out


def _parse_dt(s):
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(datetime.datetime.strptime(s, fmt).timestamp() * 1000)
        except ValueError:
            continue
    return 0


def works_of_course(client, cid, cls, cpi, name):
    portal = client.raw_get("%s/visit/stucoursemiddle?courseid=%s&clazzid=%s&vc=1&cpi=%s"
                            % (MOOC, cid, cls, cpi), referer=MOOC + "/visit/courses")
    m = re.search(r"(/work/getAllWork\?[^'\"]+)", portal)
    if not m:
        return []
    gpath = m.group(1).replace("&amp;", "&") + "&start=0&size=50"
    wl = client.raw_get(MOOC + "/mooc-ans" + gpath)
    out = []
    for blk in re.split(r'<div class="titTxt"', wl)[1:]:
        a = re.search(r'class="inspectTask"[^>]*?data="(\d+)"[^>]*?data2="(\d+)"[^>]*?data3="(\d+)"'
                      r'[^>]*?title="([^"]+)"', blk, re.S)
        if a:
            wid, waid, title = a.group(1), a.group(2), a.group(4)
        else:
            b = re.search(r'selectWorkQuestionYiPiYue\?[^"]*workId=(\d+)&workAnswerId=(\d+)[^"]*"'
                          r'[^>]*title="([^"]+)"', blk)
            if not b:
                continue
            wid, waid, title = b.group(1), b.group(2), b.group(3)
        if any(k in title for k in TITLE_EXCLUDES):
            continue
        st = re.search(r"<strong>\s*([^<]+?)\s*</strong>", blk)
        end = re.search(r"截止时间：</span>([^<]+)", blk)
        start = re.search(r"开始时间：</span>([^<]+)", blk)
        we = re.search(r"enc=([0-9a-f]{32})", blk)
        dl = end.group(1).strip() if end else ""
        out.append({"courseId": cid, "classId": cls, "cpi": cpi, "course": name,
                    "workId": wid, "answerId": waid, "title": title,
                    "status": st.group(1).strip() if st else "",
                    "start": start.group(1).strip() if start else "", "deadline": dl,
                    "deadlineTs": _parse_dt(dl), "enc": we.group(1) if we else "",
                    "etype": "work"})
    return out


def refresh_all(client, limit=None, delay=(0.6, 1.4)):
    """逐课全扫（每课 2 个请求）。风控处置：首课先免费重载；单课被拦只跳过该课并汇总，绝不静默 0 条。"""
    try:
        trips = course_triplets(client)
    except RiskControl:
        client.reload_cookies()
        try:
            trips = course_triplets(client)
        except RiskControl:
            time.sleep(RISK_COOLDOWN_S)
            if not client.recover_session():
                raise
            trips = course_triplets(client)
    if limit:
        trips = trips[:limit]
    all_w, skipped = [], []
    for i, (cid, cls, cpi, name) in enumerate(trips):
        try:
            ws = works_of_course(client, cid, cls, cpi, name)
        except RiskControl:
            client.reload_cookies()
            try:
                ws = works_of_course(client, cid, cls, cpi, name)
            except RiskControl as e2:
                skipped.append(name)
                log("[%d/%d] %s 风控跳过：%s" % (i + 1, len(trips), name[:18], str(e2)[:50]))
                continue
        except Exception as e:
            log("[%d/%d] %s ERR %s" % (i + 1, len(trips), name[:18], str(e)[:60]))
            continue
        all_w.extend(ws)
        log("[%d/%d] %s -> %d 条" % (i + 1, len(trips), name[:18], len(ws)))
        time.sleep(delay[0] + random.random() * (delay[1] - delay[0]))
    if skipped:
        log("!! 有 %d 门课被风控跳过（%s）——等 1-2 分钟冷后再扫可补齐，不是没作业"
            % (len(skipped), "、".join(skipped[:4])))
    return all_w


# ==================== 领卷 + 题目解析 ====================
QTYPE = {"0": "single", "1": "multi", "2": "blank", "3": "judge",
         "4": "subjective", "5": "subjective", "6": "subjective", "7": "subjective",
         "8": "subjective", "9": "blank", "10": "blank", "11": "blank", "13": "blank",
         "14": "blank", "18": "subjective", "26": "subjective"}
_TM_MARK = re.compile(r"[（(]\s*(单选题|多选题|判断题|填空题|主观题)\s*"
                      r"(?:[,，]\s*\d+(?:\.\d+)?\s*分)?\s*[)）]")
_TM_TYPE = {"单选题": "single", "多选题": "multi", "判断题": "judge",
            "填空题": "blank", "主观题": "subjective"}


# 手写题关键词（桌面版助手 core/audit.py）：题目明确要求手写/拍照上传的**不能**自动作答，
# 题读得到但必须人工——solver 据此直接落 need_manual，不浪费模型调用。
HW_HINTS = ("手写", "写在纸上", "纸上作答", "纸质", "拍照上传", "上传照片",
            "上传图片", "拍照提交", "拍照作答", "附纸")


def parse_work_page(html):
    """dowork 卷子页 -> 题目池。题型四条线索兜底：answertype 输入 > typeName > 题面标记 > 保守主观题。"""
    out = []
    for m in re.finditer(r'<div class="[^"]*"[^>]*id="question(\d+)"[^>]*>', html):
        qid = m.group(1)
        tn = re.search(r'typeName="([^"]*)"', m.group(0))
        tname = tn.group(1) if tn else ""
        i = m.end()
        nxt = re.search(r'<div class="[^"]*"[^>]*id="question\d+"', html[i:])
        ch = html[i:i + (nxt.start() if nxt else 30000)]
        tc = re.search(r'id="answertype%s"[^>]*value="(\d+)"' % qid, ch)
        qtype = QTYPE.get(tc.group(1)) if tc else None
        if qtype is None:
            qtype = ("multi" if "多选" in tname else "single" if "单选" in tname else
                     "judge" if "判断" in tname else "blank" if "填空" in tname else None)
        stem_m = re.search(r'<h3[^>]*class="mark_name[^"]*"[^>]*>(.*?)</h3>', ch, re.S)
        stem_raw = stem_m.group(1) if stem_m else ""
        stem = ""
        if stem_m:
            stem = re.sub(r"^\d+\.\s*", "", stem_raw)
            stem = re.sub(r"<span[^>]*>\((?:单选题|多选题|填空题|判断题|主观题)\)</span>", "", stem)
        has_img = "<img" in stem_raw
        img_urls = [u for u in re.findall(r'<img[^>]*src="([^"]+)"', stem_raw)
                    if "ananas" in u or u.startswith("//")]
        stem_txt = strip_html(stem)[:2000]
        if qtype is None or qtype == "subjective":
            # 必须搜**未被 span 正则清过的原文**（`(判断题)` 会被它整段删掉）
            mm = _TM_MARK.search(strip_html(stem_raw)) or _TM_MARK.search(stem_txt)
            if mm:
                qtype = _TM_TYPE[mm.group(1)]
        if qtype is None:
            qtype = "subjective"
        stem_txt = _TM_MARK.sub("", stem_txt).strip() or stem_txt
        q = {"qid": qid, "type": qtype, "type_name": tname, "stem": stem_txt,
             "image_flag": has_img, "img_urls": img_urls, "options": {}}
        if qtype in ("single", "multi"):
            for om in re.finditer(
                    r'<span[^>]*data="([A-G])"[^>]*class="choice%s[^"]*"[^>]*>[A-G]</span>\s*'
                    r'<div[^>]*class="fl answer_p"[^>]*>(.*?)</div>' % qid, ch, re.S):
                q["options"][om.group(1)] = strip_html(om.group(2))[:500]
            if not has_img and not q["options"]:
                q["image_flag"] = bool(re.search(r"choice%s" % qid, ch))
        elif qtype == "judge":
            # 判断题选项 value 是 data 原值 true/false（**不是**字母 A/B）
            jmap = {}
            for om in re.finditer(
                    r'<span[^>]*data="(true|false)"[^>]*class="choice%s[^"]*"[^>]*>[A-G]</span>\s*'
                    r'<div[^>]*class="fl answer_p"[^>]*>(.*?)</div>' % qid, ch, re.S):
                letter = "A" if om.group(1) == "true" else "B"
                q["options"][letter] = strip_html(om.group(2))[:20] or ("对" if letter == "A" else "错")
                jmap[letter] = om.group(1)
            if jmap:
                q["judge_map"] = jmap
        if qtype == "blank":
            # 空数权威来源是 hidden tiankongsize{qid}（下划线在题图里，正文数不到）
            ts = re.search(r'name="tiankongsize%s"[^>]*value="(\d+)"' % qid, ch)
            q["blank_count"] = int(ts.group(1)) if ts else max(1, len(re.findall(r"_{3,}", stem_txt)))
        q["handwrite"] = any(h in q["stem"] for h in HW_HINTS)
        out.append(q)
    return out


def fetch_work(client, courseid, clazzid, cpi, workid, answerid="0", enc=""):
    """领卷 -> (questions, ctx)。全链只 GET。

    ctx 里带提交要用的 form_action/hidden/standardEnc。
    注意 getWorkStuUrl 契约已变：对已排期作业 302 直落 dowork 页（不是 JSON）——两种都要吃下。
    """
    page = None
    if not enc:
        gurl = (STAT2 + "/stat2/learning/plan/getWorkStuUrl?courseid=%s&clazzid=%s&cpi=%s&workId=%s"
                % (courseid, clazzid, cpi, workid))
        body = client.raw_get_recovering(gurl)      # 这一跳也会被【9010】打中，必须走自愈壳
        if body[:1] in "@{":
            try:
                j = json.loads(body.lstrip("@"))
            except Exception:
                j = {}
            url = j.get("data") or j.get("url") or ""
            mm = re.search(r"answerId=(\d+).*?enc=([0-9a-f]{32})", url)
            if mm:
                answerid, enc = mm.group(1), mm.group(2)
        elif "addStudentWorkNewWeb" in body or 'id="question' in body:
            page = body
            ma = re.search(r'name="workAnswerId"[^>]*value="(\d+)"', body)
            if ma:
                answerid = ma.group(1)
    referer = MOOC + "/"
    # isExpire 拿 standardEnc（**referer 必须是 mooc1**：stat2 referer 会被风控成非 JSON）
    ie = client.get_json_recovering(
        MOCO_ISEXPIRE % (courseid, clazzid, cpi, workid, answerid, workid), referer=referer)
    d = ie.get("data") if isinstance(ie.get("data"), dict) else {}
    standard_enc = (d or {}).get("standardEnc", "") or ie.get("standardEnc", "")
    if page is None:
        prompt_url = (MOOC + "/mooc-ans/mooc2/work/prompt?courseId=%s&classId=%s&cpi=%s"
                      "&workId=%s&answerId=%s&enc=%s" % (courseid, clazzid, cpi, workid, answerid, enc))
        page = client.raw_get_recovering(prompt_url, referer=referer)
    qs = parse_work_page(page)
    fa = re.search(r'action="(/mooc-ans/work/addStudentWorkNewWeb\?[^"]+)"', page)
    action = fa.group(1).replace("&amp;", "&") if fa else ""
    hidden = {}
    for hm in re.finditer(r'<input[^>]*type="hidden"[^>]*name="(\w+)"[^>]*value="([^"]*)"', page):
        hidden[hm.group(1)] = hm.group(2)
    limit = re.search(r'id="limitWorkSubmitTimes"[^>]*value="(\d+)"', page)
    ctx = {"form_action": action, "hidden": hidden, "standardEnc": standard_enc,
           "limit_submit": int(limit.group(1)) if limit else 100, "answerId": answerid}
    ref = {"courseId": courseid, "classId": clazzid, "cpi": cpi,
           "workId": workid, "answerId": answerid}
    return qs, ctx, ref


MOCO_ISEXPIRE = (MOOC + "/mooc-ans/work/isExpire?courseId=%s&classId=%s&cpi=%s"
                 "&workRelationId=%s&answerId=%s&workId=%s")


# ==================== 解题（可插拔 OpenAI 兼容后端） ====================
TEXT_RULES = (
    "作答规则（逐条遵守）：\n"
    "① 只输出答案本身。不要开场白、不要收尾总结、不要解释你在做什么，"
    "不要任何 markdown 标记（**、##、- 列表）、不要 emoji。\n"
    "② 像学生自己手写的答案：句子短，直说结论和理由，可以用「因为…所以…」「也就是说」这类直白说法。\n"
    "③ 禁用这类套话：首先/其次/再次/最后、综上所述/总而言之、值得注意的是、"
    "在一定程度上、具有重要意义、有效地、从而、进而、随着……的发展。\n"
    "④ 简洁优先：一句话能答清就一句话，别为显得全面而分点罗列或排比铺陈。\n"
    "⑤ 要能手写成纯文本：矩阵按行空格分列、换行分排；分数写成 9/2 这种。\n"
)
CHOICE_PROMPT = """你是答题引擎。从选项中选出正确答案。
先在内部思考，最后一行只输出一个字母（不要任何其他字符、不要括号）。

题目：{stem}

选项：
{options}

最后一行："""
MULTI_PROMPT = """你是答题引擎。这是一道**多选题**，正确选项可能有多个（至少两个，也可能全选）。
先在内部思考，最后一行只输出正确选项的字母，按字母顺序连写（如 ABD）。
不要空格、不要逗号、不要其他任何字符。

题目：{stem}

选项：
{options}

最后一行："""
_REFUSAL = re.compile(r"(看不到|没看到|无法确定|无法回答|请提供|请把|请您提供|需要看到|"
                      r"没有.{0,6}(题目|内容|题干)|抱歉|sorry|i cannot|i need)", re.I)


def _proxy_alive(proxy, timeout=0.6):
    try:
        u = urllib.parse.urlparse(proxy if "//" in proxy else "//" + proxy)
        with socket.create_connection((u.hostname, int(u.port or 80)), timeout=timeout):
            return True
    except Exception:
        return False


def load_backend(args):
    """后端来源优先级：命令行 > 环境变量 > 桌面版助手的 settings.json/api_keys.json。

    代理默认**直连**；只在显式配置（CX_PROXY 或 settings.proxy）且端口探活通过时才走代理
    ——陌生机器上没有任何理由假设本地代理在跑（桌面版助手同款纪律）。
    """
    base = args.base_url or os.environ.get("CX_LLM_BASE_URL")
    model = args.model or os.environ.get("CX_LLM_MODEL")
    key = args.key or os.environ.get("CX_LLM_KEY")
    settings = {}
    try:
        with open(os.path.join(DATA, "settings.json"), encoding="utf-8") as f:
            settings = json.load(f)
    except Exception:
        pass
    if not (base and model):
        ref = {}
        try:
            with open(os.path.join(DATA, "api_keys.json"), encoding="utf-8") as f:
                ref = json.load(f)
        except Exception:
            pass
        for cfg in settings.get("providers", []):
            if not cfg.get("enabled") or cfg.get("kind") == "pollinations":
                continue
            if cfg.get("base_url") and cfg.get("model") and (base or cfg.get("key_ref") or cfg.get("api_key")):
                base = base or cfg["base_url"]
                model = model or cfg["model"]
                key = key or ref.get(cfg.get("key_ref", "")) or cfg.get("api_key") or ""
                break
    proxy = os.environ.get("CX_PROXY") or settings.get("proxy") or ""
    if proxy and not _proxy_alive(proxy):
        log("!! 代理 %s 探不通，临时直连" % proxy)
        proxy = ""
    return {"base_url": (base or "").rstrip("/"), "model": model or "", "key": key or "", "proxy": proxy}


def llm_chat(be, messages, max_tokens=800, timeout=180):
    if not be["base_url"]:
        raise ApiError("没有可用的解题后端：--base-url/--model/--key 或环境变量 CX_LLM_BASE_URL/CX_LLM_MODEL/CX_LLM_KEY")
    headers = {"Content-Type": "application/json", "User-Agent": UA}
    if be["key"]:
        headers["Authorization"] = "Bearer " + be["key"]
    body = {"model": be["model"], "messages": messages, "max_tokens": max_tokens,
            "temperature": 0}
    op = (urllib.request.build_opener(urllib.request.ProxyHandler({"http": be["proxy"], "https": be["proxy"]}))
          if be["proxy"] else urllib.request.build_opener(urllib.request.ProxyHandler({})))
    last = None
    for attempt in range(3):
        try:
            r = op.open(urllib.request.Request(be["base_url"] + "/chat/completions",
                                               data=json.dumps(body).encode(), headers=headers),
                        timeout=timeout)
            return json.loads(r.read().decode("utf-8", "replace"))["choices"][0]["message"]["content"]
        except Exception as e:
            last = e
            if isinstance(e, urllib.error.HTTPError) and e.code in (401, 403, 404):
                raise ApiError("HTTP %s from %s" % (e.code, be["base_url"]))
            time.sleep(2 + attempt * 3)
    raise ApiError(str(last)[:150])


def _extract_letter(raw, valid):
    """优先显式标签（答案：X），再最后一行，再全文最后一个独立字母。"""
    raw = raw or ""
    v = set(valid)
    for p in (r"正确答案[是为:：]\s*\(?([A-G])\)?", r"答案[是为:：]\s*\(?([A-G])\)?",
              r"[Aa]nswer\s*[:：]\s*\(?([A-G])\)?"):
        for c in reversed(re.findall(p, raw)):
            if c in v:
                return c
    for line in reversed([l.strip() for l in raw.splitlines() if l.strip()]):
        m = re.fullmatch(r"[（(\[]?([A-G])[)）\]]?[.。、:：]?", line)
        if m and m.group(1) in v:
            return m.group(1)
    for c in reversed(re.findall(r"(?<![A-Za-z])([A-G])(?![A-Za-z])", raw)):
        if c in v:
            return c
    return ""


def _extract_letters(raw, valid):
    raw = raw or ""
    first = r"[A-G][A-G\s,，、.。]*[A-G]|[A-G]"
    for p in (r"(?:正确答案|答案|最终答案|最后一行|最终答案)\s*[是为:：]?\s*(%s)" % first,
              r"[Aa]nswer\s*[:：]\s*(%s)" % first):
        ms = re.findall(p, raw)
        if ms:
            s = "".join(sorted({c for c in re.findall(r"[A-G]", ms[-1]) if c in set(valid)}))
            if s:
                return s
    for line in reversed([l.strip() for l in raw.splitlines() if l.strip()]):
        if re.fullmatch(r"[（(\[]?\s*[A-G][A-G\s,，、.。]*[)）\]]?\.?。?", line):
            s = "".join(sorted({c for c in re.findall(r"[A-G]", line) if c in set(valid)}))
            if s:
                return s
    runs = [r for r in re.findall(r"[A-G]{2,}", raw) if set(r) <= set(valid)]
    if runs:
        return "".join(sorted(set(runs[-1])))
    got = [c for c in re.findall(r"\b([A-G])\b", raw) if c in set(valid)]
    return "".join(sorted(set(got)))


def _stem_needs_image(stem):
    """剥掉题型模板字后实义字符 < 8 → 题干实质内容在图里（阈值 4 会把图片题漏进文本模型）。"""
    t = re.sub(r"\(\s*(填空题|单选题|多选题|判断题|主观题)\s*,?\s*\d+(\.\d+)?分\s*\)", "", stem or "")
    core = re.sub(r"[^0-9A-Za-z一-鿿]", "", t)
    return len(core) < 8


def download_image(client, url):
    if url.startswith("//"):
        url = "https:" + url
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": MOOC + "/"})
        return client.op.open(req, timeout=30).read()
    except Exception:
        return None


def image_to_text(client, q, hint=""):
    """识图链：本地 VLM（llama.cpp，主引擎）-> 云 VL 兜底 -> none（落人工）。

    本地 VLM 的 prompt 必须是 `OCR:`（约束型 prompt 实测更差）。
    """
    urls = q.get("img_urls") or []
    if not urls:
        return None, "none"
    data = download_image(client, urls[0])
    if not data:
        return None, "none"
    b64 = base64.b64encode(data).decode()
    lv = os.environ.get("CX_LOCAL_VLM_BASE") or ""
    if lv:
        try:
            body = {"model": "paddleocr", "temperature": 0, "max_tokens": 1500,
                    "messages": [{"role": "user", "content": [
                        {"type": "text", "text": "OCR:"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}}]}]}
            r = urllib.request.urlopen(urllib.request.Request(
                lv.rstrip("/") + "/v1/chat/completions", data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"}), timeout=180)
            txt = (json.loads(r.read().decode("utf-8", "replace"))["choices"][0]["message"]["content"] or "").strip()
            if txt:
                return txt, "local_vlm"
        except Exception as e:
            log("!! 本地 VLM 调用失败：%s" % str(e)[:80])
    vbase = os.environ.get("CX_VISION_BASE_URL")
    vmodel = os.environ.get("CX_VISION_MODEL")
    if vbase and vmodel:
        try:
            txt = llm_chat({"base_url": vbase.rstrip("/"), "model": vmodel,
                            "key": os.environ.get("CX_VISION_KEY", ""),
                            "proxy": os.environ.get("CX_PROXY", "")},
                           [{"role": "user", "content": [
                               {"type": "text", "text": "转写图片中的题目文字（含公式用线性记法，矩阵按行 空格分列 "
                                                        "换行分排）。只输出转写内容本身，不要分析过程。" + (hint or "")},
                               {"type": "image_url", "image_url": {"url": "data:image/png;base64," + b64}}]}],
                           max_tokens=800, timeout=280)
            if txt and txt.strip():
                return txt.strip(), "vision_api"
        except Exception as e:
            log("!! 云 VL 调用失败：%s" % str(e)[:80])
    return None, "none"


def solve_questions(client, qs, be, rate=(1.0, 2.5)):
    """逐题解。返回 {qid: {answer, confidence, source, status, note}}。status: ok/need_manual/failed"""
    results = {}
    for q in qs:
        res = {"type": q["type"], "answer": "", "confidence": 0.0, "source": "", "status": "failed"}
        stem = q.get("stem", "")
        img_cap = None
        if q.get("handwrite"):
            res.update(status="need_manual", note="题目要求手写/拍照上传，不能自动作答")
            results[q["qid"]] = res
            continue
        if q.get("image_flag") and _stem_needs_image(stem):
            if client is None:
                res.update(status="need_manual", note="图题：本次跳过识图链（--no-image）")
                results[q["qid"]] = res
                continue
            text, source = image_to_text(client, q)
            if source == "none":
                res.update(status="need_manual", note="题图未转出文字（识图链全挂）")
                results[q["qid"]] = res
                continue
            stem = ((stem + "\n") if stem.strip() else "") + text
            img_cap = {"local_vlm": 0.8, "vision_api": 0.75}.get(source, 0.6)
            res["img_source"] = source
        try:
            if q["type"] in ("single", "multi"):
                if not q.get("options"):
                    res.update(status="need_manual", note="未解析出选项（页面结构可能变了）")
                    results[q["qid"]] = res
                    continue
                opt_txt = "\n".join("%s、%s" % (k, v) for k, v in sorted(q["options"].items()))
                if q["type"] == "multi":
                    raw = llm_chat(be, [{"role": "user", "content": MULTI_PROMPT.format(stem=stem, options=opt_txt)}])
                    ans = _extract_letters(raw, q["options"].keys())
                else:
                    raw = llm_chat(be, [{"role": "user", "content": CHOICE_PROMPT.format(stem=stem, options=opt_txt)}])
                    ans = _extract_letter(raw, q["options"].keys())
                if not ans and _REFUSAL.search(raw or ""):
                    res.update(status="failed", note="模型称缺题面")
                else:
                    res.update(answer=ans, confidence=min(0.8, img_cap) if img_cap else 0.8,
                               source=be["model"], status="ok" if ans else "failed")
            elif q["type"] == "judge":
                if not q.get("options"):
                    res.update(status="need_manual", note="未解析出判断选项")
                    results[q["qid"]] = res
                    continue
                raw = llm_chat(be, [{"role": "user", "content": CHOICE_PROMPT.format(stem=stem, options="A、对\nB、错")}])
                ans = _extract_letter(raw, q["options"].keys())
                res.update(answer=ans, confidence=0.8 if ans else 0.0,
                           source=be["model"], status="ok" if ans else "failed")
            else:   # blank / subjective
                n = int(q.get("blank_count") or 1)
                if q["type"] == "blank":
                    task = ("这是填空题，共 %d 空。每空只写答案本身（一个词/一个数/一个式子），"
                            "不要写成句子、不要解释；每行一个答案，按顺序对应每个空。" % n)
                else:
                    task = "这是简答题。用平实的话直接回答，写成一小段就行，不要小标题、不要分点罗列、不要「首先其次」。"
                txt = llm_chat(be, [{"role": "user", "content": "%s%s\n\n题目：\n%s" % (TEXT_RULES, task, stem)}])
                if _REFUSAL.search(txt or ""):
                    res.update(status="failed", note="模型称缺题面")
                else:
                    txt = (txt or "").strip()
                    res.update(answer=([l.strip() for l in txt.splitlines() if l.strip()]
                                       if q["type"] == "blank" else txt),
                               confidence=min(0.6, img_cap) if img_cap else 0.6,
                               source=be["model"], status="ok" if txt else "failed")
        except ApiError as e:
            res.update(status="failed", note=str(e)[:120])
        results[q["qid"]] = res
        log("  %s %s -> %s (%s)" % (q["type"], q["qid"],
                                    (str(res["answer"])[:24] or res["status"]), res["status"]))
        time.sleep(rate[0] + random.random() * (rate[1] - rate[0]))
    return results


# ==================== 提交（真协议） ====================
def _as_editor_html(s):
    s = str(s) if s is not None else ""
    return "".join("<p>%s</p>" % ln for ln in s.split("\n") if ln.strip()) or ""


def build_form(qs, results, ctx, ref):
    """按卷子页 hidden + 真协议字段拼表单。

    真协议要点（2026-09-28 实测定案）：
      · 填空：answertype{qid}=2，每空一个 answerEditor{qid}{n}（UEditor HTML），不是 answer{qid} 纯文本
      · 判断：answertype{qid}=3，answer{qid}=true/false 原值（不是 A/B）
      · answerwqbid 必须 = qid 逗号串 + 尾逗号（页面 hidden 是空串，照抄会被拒「无效的参数：code-1」）
    """
    hidden = dict(ctx.get("hidden") or {})
    form = {
        "courseId": ref["courseId"], "classId": ref["classId"],
        "knowledgeid": hidden.get("knowledgeid", "0"), "cpi": ref["cpi"],
        "workRelationId": ref["workId"], "workAnswerId": ref["answerId"],
        "jobid": hidden.get("jobid", ""), "standardEnc": ctx.get("standardEnc", ""),
        "enc_work": hidden.get("enc_work", ""),
        "totalQuestionNum": hidden.get("totalQuestionNum", ""),
        "pyFlag": hidden.get("pyFlag", "3"),
        "answerwqbid": hidden.get("answerwqbid") or (",".join(q["qid"] for q in qs) + ","),
        "mooc2": "1",
        "uploadEnc": hidden.get("uploadEnc", hidden.get("enc", "")),
        "enc": hidden.get("enc", ""), "matchEnc": hidden.get("matchEnc", ""),
        "workTimesEnc": hidden.get("workTimesEnc", ""),
        "randomOptions": hidden.get("randomOptions", "false"),
        "questionIds": ",".join(q["qid"] for q in qs) + ",",
    }
    for q in qs:
        r = results.get(q["qid"]) or {}
        ans = r.get("answer", "")
        if q["type"] in ("single", "multi"):
            form["answertype%s" % q["qid"]] = "0" if q["type"] == "single" else "1"
            form["answer%s" % q["qid"]] = ans
        elif q["type"] == "blank":
            form["answertype%s" % q["qid"]] = "2"
            blanks = list(ans) if isinstance(ans, (list, tuple)) else [ans]
            form["tiankongsize%s" % q["qid"]] = str(len(blanks))
            for n, one in enumerate(blanks, 1):
                form["answerEditor%s%d" % (q["qid"], n)] = _as_editor_html(one)
        elif q["type"] == "judge":
            form["answertype%s" % q["qid"]] = "3"
            form["answer%s" % q["qid"]] = (q.get("judge_map") or {}).get(ans, ans)
        else:
            form["answertype%s" % q["qid"]] = "4"
            form["answer%s" % q["qid"]] = ans or ""
    return form


def submit_work(client, qs, results, ctx, ref, confirm=False):
    action = ctx.get("form_action")
    if not action:
        raise ApiError("form_action 缺失：dowork 页结构变了（拿不到提交地址）")
    m = re.search(r"&version=(\d+)", action)
    if m:
        url = re.sub(r"&version=\d+", "&version=%d" % (int(m.group(1)) + 1), action)
    else:
        url = action + "&version=1"
    url += ("&" if "?" in url else "?") + urllib.parse.urlencode(
        {"ua": "pc", "formType": "1", "saveStatus": "1"})
    form = build_form(qs, results, ctx, ref)
    if not confirm:
        return {"status": "dry_run", "note": "未发送（需 --confirm）", "url": url,
                "field_count": len(form),
                "answers": {k: v for k, v in form.items()
                            if k.startswith("answer") and k != "answerwqbid"}}
    referer = (MOOC + "/mooc-ans/mooc2/work/dowork?courseId=%s&classId=%s&cpi=%s&workId=%s&answerId=%s"
               % (ref["courseId"], ref["classId"], ref["cpi"], ref["workId"], ref["answerId"]))
    t = client.raw_post(MOOC + url, form, referer=referer,
                        headers={"X-Requested-With": "XMLHttpRequest"})
    try:
        j = json.loads(t)
    except Exception:
        raise ApiError("非 JSON 响应(len=%d): %s" % (len(t), strip_html(t)[:120]))
    if not j.get("status"):
        raise ApiError("服务端拒绝: " + str(j.get("msg"))[:200])
    return {"status": "ok", "raw": j}


# ==================== selftest ====================
FIXTURE = """
<form id="submitForm" action="/mooc-ans/work/addStudentWorkNewWeb?workId=101&amp;courseId=202&amp;token=abc">
<input type="hidden" name="jobid" value="j1">
<input type="hidden" name="totalQuestionNum" value="3">
<input type="hidden" name="answerwqbid" value="">
<input type="hidden" name="enc_work" value="EW">
<input type="hidden" name="enc" value="EN">
</form>
<div class="TiMu" id="question5001" typeName="单选题">
<h3 class="mark_name"><span>(单选题)</span>1. 下列哪一个是素数？</h3>
<input type="hidden" id="answertype5001" value="0">
<span data="A" class="choice5001">A</span><div class="fl answer_p">4</div>
<span data="B" class="choice5001">B</span><div class="fl answer_p">7</div>
</div>
<div class="TiMu" id="question5003" typeName="填空题">
<h3 class="mark_name">2. 1+1=<img src="https://p.ananas.chaoxing.com/star3/origin/aa.png"></h3>
<input type="hidden" name="tiankongsize5003" value="2">
</div>
<div class="TiMu" id="question5004" typeName="判断题">
<h3 class="mark_name"><span>(判断题)</span>3. 地球是圆的。</h3>
<input type="hidden" id="answertype5004" value="3">
<span data="true" class="choice5004">A</span><div class="fl answer_p">对</div>
<span data="false" class="choice5004">B</span><div class="fl answer_p">错</div>
</div>
"""


def selftest():
    ok = []
    from aes128 import _encrypt_block, _expand_key
    ct = _encrypt_block(bytes.fromhex("00112233445566778899aabbccddeeff"),
                        _expand_key(bytes.fromhex("000102030405060708090a0b0c0d0e0f")))
    ok.append(("aes_fips197", ct.hex() == "69c4e0d86a7b0430d8cdb78070b4c55a", ct.hex()))
    enc = aes_encrypt("13800000000")
    ok.append(("aes_login_b64", len(base64.b64decode(enc)) % 16 == 0 and len(base64.b64decode(enc)) > 0, enc[:12]))
    qs = parse_work_page(FIXTURE)
    ok.append(("q_count", len(qs) == 3, [q["qid"] for q in qs]))
    types = [q["type"] for q in qs]
    ok.append(("q_types", types == ["single", "blank", "judge"], types))
    ok.append(("q_options", qs[0]["options"] == {"A": "4", "B": "7"}, qs[0]["options"]))
    ok.append(("q_image", qs[1]["image_flag"] is True and qs[1]["img_urls"], qs[1]["img_urls"]))
    ok.append(("q_blank_count", qs[1]["blank_count"] == 2, qs[1]["blank_count"]))
    ok.append(("q_judge_map", qs[2].get("judge_map") == {"A": "true", "B": "false"}, qs[2].get("judge_map")))
    ctx = {"form_action": "/mooc-ans/work/addStudentWorkNewWeb?workId=101&courseId=202&version=0",
           "hidden": {"jobid": "j1", "totalQuestionNum": "3", "answerwqbid": "", "enc_work": "EW", "enc": "EN"},
           "standardEnc": "SE", "answerId": "9"}
    ref = {"courseId": "202", "classId": "303", "cpi": "404", "workId": "101", "answerId": "9"}
    results = {"5001": {"answer": "B"}, "5003": {"answer": ["9/2", "x=2"]}, "5004": {"answer": "A"}}
    f = build_form(qs, results, ctx, ref)
    ok.append(("form_answerwqbid", f["answerwqbid"] == "5001,5003,5004,", f["answerwqbid"]))
    ok.append(("form_questionIds", f["questionIds"] == "5001,5003,5004,", f["questionIds"]))
    ok.append(("form_single", f["answer5001"] == "B" and f["answertype5001"] == "0", f["answer5001"]))
    ok.append(("form_blank", f["answertype5003"] == "2" and f["tiankongsize5003"] == "2"
               and f["answerEditor50031"] == "<p>9/2</p>", f["answerEditor50031"]))
    ok.append(("form_judge", f["answer5004"] == "true" and f["answertype5004"] == "3", f["answer5004"]))
    ok.append(("form_ids", f["workRelationId"] == "101" and f["workAnswerId"] == "9"
               and f["standardEnc"] == "SE" and f["cpi"] == "404", f["standardEnc"]))
    bad = [(n, d) for n, p, d in ok if not p]
    for n, p, d in ok:
        print("%-18s %s  %s" % (n, "PASS" if p else "FAIL", d))
    print("\nSELFTEST: %d/%d PASS" % (len(ok) - len(bad), len(ok)))
    return 0 if not bad else 1


AUDIT_CACHE = os.path.join(DATA, "audit_cache.json")


def audit_tasks(client, tasks, limit=None, use_cache=True):
    """三态审核（**只 GET**）：可作答 / 非作业 / 读不到题 / 需手写。

    为什么要这一步：getAllWork 列表里混着签到、讨论、分组任务，题数只有领卷后才知道。
    先零成本判「非作业」，再领卷验真，agent 才不会把时间花在必错的题上。
    缓存 audit_cache.json：命中零请求；生命周期 = 提交成功 or 作业过期（不做时间 TTL）。
    """
    cache = {}
    if use_cache and os.path.exists(AUDIT_CACHE):
        try:
            cache = json.load(open(AUDIT_CACHE, encoding="utf-8"))
        except Exception:
            cache = {}
    out = []
    for t in (tasks[:limit] if limit else tasks):
        key = "%s:%s" % (t.get("courseId", ""), t.get("workId", ""))
        et = t.get("eventType") or t.get("event_type") or "work"
        if et not in ("", "work"):
            out.append(dict(t, solvable=False, qreal=0, handwrite=False, preview=[],
                            reason="非作业(%s)" % et))
            continue
        if not (t.get("classId") and t.get("cpi")):
            out.append(dict(t, solvable=False, qreal=0, handwrite=False, preview=[],
                            reason="任务 url 缺 classId/cpi，需先 --refresh 扫一遍"))
            continue
        ent = cache.get(key) if use_cache else None
        if isinstance(ent, dict) and isinstance(ent.get("result"), dict):
            r = dict(ent["result"])
            # 缓存是与桌面版助手共用的 audit_cache.json：老条目没有 handwrite 等
            # 新字段，必须补默认值，否则 KeyError 会把审核线程整条打断（实测踩到）。
            r.setdefault("handwrite", False)
            r.setdefault("qreal", 0)
            r.setdefault("preview", [])
            r.setdefault("reason", "")
            r.setdefault("solvable", bool(r["qreal"]))
            r["cached"] = True
        else:
            try:
                qs, _ctx, _ref = fetch_work(client, t["courseId"], t["classId"], t["cpi"], t["workId"])
                r = {"qreal": len(qs), "solvable": len(qs) > 0,
                     "handwrite": any(q.get("handwrite") for q in qs),
                     "preview": [[q["type"], q["stem"][:24]] for q in qs[:2]],
                     "reason": "" if qs else "领卷成功但无题"}
            except RiskControl:
                raise
            except Exception as e:
                r = {"qreal": 0, "solvable": False, "handwrite": False, "preview": [],
                     "reason": "读不到题(%s)" % str(e)[:60]}
            cache[key] = {"result": r, "ts": time.time()}
            time.sleep(0.6 + random.random() * 0.8)   # 限速：审核也是领卷，别把 IP 打热
        out.append(dict(t, **r))
        log("%-14s %-22s -> %s | %d题%s" % ((t.get("course") or "")[:14], (t.get("title") or "")[:22],
                                            "可作答" if r["solvable"] else r["reason"],
                                            r["qreal"], " | 需手写" if r["handwrite"] else ""))
    if use_cache:
        try:
            tmp = AUDIT_CACHE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False)
            os.replace(tmp, AUDIT_CACHE)
        except Exception:
            pass
    return out


# ==================== CLI ====================
def _fmt_task(t):
    return "%-16s | %-26s | %s | %s题 | %s" % ((t.get("course") or "")[:16],
                                               (t.get("title") or "")[:26],
                                               t.get("endDate") or "", t.get("questionNum", ""),
                                               t.get("remain") or t.get("deadline") or "")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="cxflow", description="学习通(超星)作业全链路 CLI：登录/读任务/领卷/解题/提交/风控")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("login", help="登录并落盘 cookie")
    p.add_argument("--user"), p.add_argument("--pwd")
    p.add_argument("--force", action="store_true", help="无视现有 cookie 强制重登")

    p = sub.add_parser("tasks", help="读课程任务")
    p.add_argument("--refresh", action="store_true", help="再逐课全扫一次（实时状态，慢）")
    p.add_argument("--limit", type=int)

    p = sub.add_parser("pull", help="领卷（只 GET）")
    for a in ("--course-id", "--class-id", "--cpi", "--work-id"):
        p.add_argument(a, required=True)
    p.add_argument("--answer-id", default="0")
    p.add_argument("--enc", default="")
    p.add_argument("--out")

    p = sub.add_parser("solve", help="解题")
    p.add_argument("--in", dest="inp", required=True)
    p.add_argument("--out")
    p.add_argument("--base-url"), p.add_argument("--model"), p.add_argument("--key")
    p.add_argument("--no-image", action="store_true", help="跳过识图链（图题一律落人工）")

    p = sub.add_parser("submit", help="提交（默认 dry-run）")
    p.add_argument("--in", dest="inp", required=True)
    p.add_argument("--answers", required=True)
    p.add_argument("--confirm", action="store_true")

    p = sub.add_parser("audit", help="三态审核（只 GET 领卷验真，带缓存）")
    p.add_argument("--limit", type=int)
    p.add_argument("--refresh", action="store_true", help="先逐课扫一遍再审核（classId/cpi 更全）")
    p.add_argument("--no-cache", action="store_true")

    p = sub.add_parser("captcha", help="风控验证码人工出口")
    p.add_argument("action", choices=["get", "post"])
    p.add_argument("--out", default="captcha.png")
    p.add_argument("--code")

    sub.add_parser("selftest", help="离线自检")
    args = ap.parse_args(argv)

    if args.cmd == "selftest":
        return selftest()

    c = Client()
    if args.cmd == "login":
        if args.force:
            result = c.login(args.user, args.pwd)
        else:
            result = c.ensure_login() if not (args.user and args.pwd) else c.login(args.user, args.pwd)
        emit({"ok": bool(result), "cookie_file": COOKIE_FILE})
        return 0

    if args.cmd == "captcha":
        if args.action == "get":
            data, ct = c.fetch_captcha()
            with open(args.out, "wb") as f:
                f.write(data)
            emit({"ok": True, "image": os.path.abspath(args.out), "content_type": ct,
                  "hint": "打开这张图看 4 位验证码，然后 `captcha post --code XXXX`（不自动识别）"})
            return 0
        ok = c.submit_captcha(args.code)
        emit({"ok": bool(ok),
              "hint": "" if ok else "回验仍被拦：等 1-2 分钟后重试，或到学习通 App/网页手动过验证码"})
        return 0 if ok else 1

    c.ensure_login()

    if args.cmd == "tasks":
        snap = stat2_tasks(c)
        out = {"generated": snap["generated"], "tasks": snap["tasks"]}
        for t in snap["tasks"]:
            log(_fmt_task(t))
        if args.refresh:
            works = refresh_all(c, limit=args.limit)
            have = {(t["courseId"], t["workId"]) for t in out["tasks"]}
            for w in works:
                if (w["courseId"], w["workId"]) not in have and w.get("status") != "已完成":
                    out["tasks"].append(w)
            out["scanned"] = len(works)
        emit(out)
        return 0

    if args.cmd == "audit":
        snap = stat2_tasks(c)
        tasks = list(snap["tasks"])
        if args.refresh:
            works = refresh_all(c, limit=args.limit)
            have = {(t["courseId"], t["workId"]) for t in tasks}
            for w in works:
                if (w["courseId"], w["workId"]) not in have and w.get("status") != "已完成":
                    tasks.append(w)
        audited = audit_tasks(c, tasks, limit=args.limit, use_cache=not args.no_cache)
        emit({"audited": len(audited), "solvable": sum(1 for a in audited if a["solvable"]),
              "items": audited})
        return 0

    if args.cmd == "pull":
        qs, ctx, ref = fetch_work(c, args.course_id, args.class_id, args.cpi,
                                  args.work_id, args.answer_id, args.enc)
        out = {"work_ref": ref, "ctx": ctx, "questions": qs}
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=1)
            log("领卷 %d 题 -> %s" % (len(qs), args.out))
        emit(out)
        return 0

    if args.cmd == "solve":
        with open(args.inp, encoding="utf-8") as f:
            job = json.load(f)
        be = load_backend(args)
        log("后端: %s @ %s" % (be["model"] or "?", be["base_url"] or "?"))
        results = solve_questions(None if args.no_image else c, job["questions"], be)
        out = {"work_ref": job.get("work_ref"), "backend": be["model"], "results": results,
               "ok": sum(1 for r in results.values() if r["status"] == "ok"),
               "total": len(results)}
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=1)
            log("%d/%d 解出 -> %s" % (out["ok"], out["total"], args.out))
        emit(out)
        return 0

    if args.cmd == "submit":
        with open(args.inp, encoding="utf-8") as f:
            job = json.load(f)
        with open(args.answers, encoding="utf-8") as f:
            ans = json.load(f)
        results = ans.get("results", ans)
        r = submit_work(c, job["questions"], results, job["ctx"], job["work_ref"], confirm=args.confirm)
        emit(r)
        return 0
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RiskControl as e:
        log("!! 被超星风控拦截：%s" % e)
        log("   处置：`captcha get` 取图 -> 人工看图 -> `captcha post --code XXXX` 过验证码；")
        log("   或到学习通 App/网页正常登录一次后重试（风控跟 IP 热度走，等 1-2 分钟更稳）。")
        sys.exit(2)
    except SessionExpired as e:
        log("!! %s" % e)
        log("   处置：`login --force` 重登一次。")
        sys.exit(3)
    except NoCredentials as e:
        log("!! %s" % e)
        sys.exit(4)
    except ApiError as e:
        log("!! %s" % e)
        sys.exit(5)
    except KeyboardInterrupt:
        sys.exit(130)
