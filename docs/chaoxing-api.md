# 超星（学习通）接口清单 —— 真协议

全部经 2026-09 真环境实测（桌面版助手 v0.4.3 的注释 + 本次 cxflow 实跑）。
**请求一律带** `User-Agent: <PC Chrome UA>` 与 `Referer`（见每条），POST 用
`Content-Type: application/x-www-form-urlencoded; charset=UTF-8` + `Origin: https://<host>`。

## 0. 域名
| 域名 | 用途 |
|---|---|
| `passport2.chaoxing.com` | 登录（表单 + fanyalogin） |
| `mooc1.chaoxing.com` | 课程/作业主站（courses、getAllWork、prompt、dowork、提交、验证码） |
| `stat2-ans.chaoxing.com` | 官方「学习规划」聚合（临期任务）+ 作业直达链（getWorkStuUrl） |
| `mobilelearn.chaoxing.com` | 签到（不在本技能范围：`core/sign.py` 另存） |
| `p.ananas.chaoxing.com` | 题图 CDN（`/star3/origin/<hash>.png`） |

## 1. 登录
```
GET  https://passport2.chaoxing.com/login?fid=-1&refer=http%3A%2F%2Fi.chaoxing.com
     -> 从页面抠 hidden: fid / refer / t / forbidotherlogin / validate / doubleFactorLogin
                        / independentId / independentNameId
POST https://passport2.chaoxing.com/fanyalogin
     Referer: 上面那个 login URL；X-Requested-With: XMLHttpRequest
     fid, uname=<AES(账号)>, password=<AES(密码)>, refer, t, forbidotherlogin,
     validate, doubleFactorLogin, independentId, independentNameId
     -> {"status":true,...} / status=false 时看 msg2|msg
```
- **AES-128-CBC + PKCS7，key = iv = `u2oh6Vu^HWe4_AES`，密文再 base64**（`scripts/aes128.py`，纯标准库）。
- 登录成功把 cookie 存 `cookies.txt`（MozillaCookieJar 格式），后续所有请求都靠它。
- 判掉线：最终 URL 落在 `passport2.chaoxing.com/.../login`，或正文含「扫码登录/用户登录/手机号登录」。
  **注意**：`login()` 本身就是去 GET 登录页取 token，别把「请求登录页」判成掉线。
- 验证码登录（`validate` 非空）本链未实现：遇到就走「人工在 App/网页登录一次」。

## 2. 读课程任务
### 2a. 临期任务聚合（**首选，1 个请求**）
```
GET https://stat2-ans.chaoxing.com/stat2/learning/plan/recommended-course-list
    Referer: https://stat2-ans.chaoxing.com/stat2-vue/studyPlanAssistant
-> {"code":0,"data":{"cacheGenerateTime","allNearTasks":[{id,courseName,name,eventType,
    endTime(ms),endDate,remainTimeStr,questionNum,url}],"courseList":[{courseName,clazzId,
    className,learningProgressPer,nearTaskNum,weakKnowledgePointsNum}],...}}
```
- 官方**每 4h 刷新**，只给「临期」任务；想实时/全量要 2b。
- `code != 0` 或 `msg == "未登录"` → 掉线。响应可能带 ajax 防劫持前缀 `@`，先 `lstrip("@")`。
- **坑**：三 id 要从 `url` 里**分别**解析（`courseid|courseId`、`clazzid|classid`、`cpi`）。
  活动类任务 url 常缺 `clazzid`，用「三个参数一条正则整体匹配」会全返空 → 后续 `classId=` 空 → 服务端返非 JSON。

### 2b. 逐课全量扫（实时状态）
```
GET https://mooc1.chaoxing.com/visit/courses            Referer: https://mooc1.chaoxing.com/
    -> <a class="courseName" href='/visit/stucoursemiddle?courseid=..&clazzid=..&vc=N&cpi=..' title="课名">
GET https://mooc1.chaoxing.com/visit/stucoursemiddle?courseid=C&clazzid=K&vc=1&cpi=P
    -> 页面里抠出 (/work/getAllWork?...)   拼 &start=0&size=50
GET https://mooc1.chaoxing.com/mooc-ans/work/getAllWork?...
    -> 每条作业 <div class="titTxt"> 块：
       data=<workId> data2=<workAnswerId> data3=? title=<标题>
       （已批阅的走 selectWorkQuestionYiPiYue?workId=..&workAnswerId=..）
       <strong>待做|已完成|待批阅</strong>；开始时间/截止时间；enc=<32 位 hex>
```
- 列表**没有题数/题型字段**，题数只有领卷后才知道。
- 标题含「分组任务/PBL」→ 剔除；「随堂练习」**不要**在入口剔除（很多课它就是作业，交给审核判）。
- 每课 2 个请求；请求间 `0.6~1.4s` 随机 sleep。

## 3. 领卷（**全链只 GET，无副作用**）
```
GET https://stat2-ans.chaoxing.com/stat2/learning/plan/getWorkStuUrl
    ?courseid=C&clazzid=K&cpi=P&workId=W
    -> 契约已变（2026-09-28 实锤）：
       ① 已排期作业 = 302 **直接落到 dowork 卷子页 HTML**（不是 JSON）
       ② 仍是 JSON 时抠 data/url 里的 answerId=..&enc=<32hex>
GET https://mooc1.chaoxing.com/mooc-ans/work/isExpire
    ?courseId=C&classId=K&cpi=P&workRelationId=W&answerId=A&workId=W
    Referer: https://mooc1.chaoxing.com/     ★ 必须 mooc1：stat2 的 Referer 会被风控成非 JSON
    -> {"data":{"standardEnc":"<32hex>"}, "status": 3=可直接提交 / 2=需先过验证码}
GET https://mooc1.chaoxing.com/mooc-ans/mooc2/work/prompt
    ?courseId=C&classId=K&cpi=P&workId=W&answerId=A&enc=<32hex>
    -> 卷子页（若 getWorkStuUrl 已给页面则跳过这一跳）
```
卷子页里要抠的东西：
| 目标 | 位置 |
|---|---|
| 提交 URL | `<form id="submitForm" action="/mooc-ans/work/addStudentWorkNewWeb?…">`（`&amp;` 要还原） |
| 提交 hidden 群 | 所有 `<input type="hidden" name=.. value=..>`（jobid/enc/enc_work/knowledgeid/pyFlag/…） |
| 题目 | `<div … id="question{qid}" typeName="…">` |
| 题型 | `id="answertype{qid}" value=N`（0 单选/1 多选/2 填空/3 判断/4+ 主观） |
| 题干 | `<h3 class="mark_name">…</h3>` |
| 选项 | `<span data="A" class="choice{qid}">A</span><div class="fl answer_p">…</div>` |
| 判断题选项 | `data="true|false"`（**提交值就是 true/false，不是字母**） |
| 空数 | `<input name="tiankongsize{qid}" value=N>`（权威来源，正文下划线数常不准） |
| 题图 | 题干里的 `<img src="https://p.ananas.chaoxing.com/star3/origin/…">` |
| 次数上限 | `id="limitWorkSubmitTimes" value=N` |
- **题型四条线索兜底**（缺一就整类丢）：answertype 输入 → 容器 `typeName` → 题面 `(单选题, 2分)` 标记 → 保守当主观题。
  只靠 answertype 会漏（实测有容器既无 typeName 也无输入）；只靠 typeName 会把多选并进单选（答案必错）。
- 领卷**也会被 9010 打中**，必须走自愈壳（见 risk-control.md）。

## 4. 提交
```
GET  https://mooc1.chaoxing.com/mooc-ans/work/validate?courseId=C&classId=K&cpi=P
     -> status=3 可直提；2 = 需验证码（captcha）
POST https://mooc1.chaoxing.com/mooc-ans/work/addStudentWorkNewWeb?<form_action 的查询串>
     &ua=pc&formType=1&saveStatus=1&version=<原值+1>
     Referer: https://mooc1.chaoxing.com/mooc-ans/mooc2/work/dowork?courseId=..&classId=..&cpi=..&workId=..&answerId=..
     X-Requested-With: XMLHttpRequest
-> JSON；`status` 假 = 服务端拒绝（msg 里常是「无效的参数：code-1」）
```
字段语义（**逐条实测**）：
| 字段 | 值 |
|---|---|
| `courseId` `classId` `cpi` | 三 id |
| `workRelationId` `workAnswerId` | workId / answerId |
| `knowledgeid` `jobid` `enc` `enc_work` `standardEnc` `uploadEnc` `matchEnc` `workTimesEnc` `pyFlag` `randomOptions` | 从页面 hidden 原样带（缺就给默认） |
| `totalQuestionNum` | hidden 的题数 |
| `questionIds` | `qid1,qid2,…,`（**尾逗号**） |
| `answerwqbid` | **必须 = 同上的 qid 逗号串 + 尾逗号**；页面 hidden 是空串，照抄必被拒 |
| `answertype{qid}` | 0 单选 / 1 多选 / 2 填空 / 3 判断 / 4 主观 |
| `answer{qid}` | 单选|多选=字母串（多选无分隔按字母序，如 `ABD`）；判断=**`true`/`false`**；主观=文本 |
| `answerEditor{qid}{n}` + `tiankongsize{qid}` | 填空专用：n=1..空数，内容是 **UEditor HTML**（纯文本按行包 `<p>…</p>`） |
| `mooc2` | `1` |
- 二次提交同卷 → 「已批阅作业，不允许重复提交」= **已入库的反证**，不是本次失败。
- 已完成/待批阅的作业不要解、不要提；`limitWorkSubmitTimes` 超了会被拒。

## 5. 风控与验证码
```
GET  https://mooc1.chaoxing.com/processVerifyPng.ac?t=<随机数>   Referer: https://mooc1.chaoxing.com/
POST https://mooc1.chaoxing.com/html/processVerify.ac            app=0&ucode=<4 位>
```
详见 risk-control.md（含检测判据、自愈级联、回验判据）。

## 6. 签名/其他（不在本技能范围，供扩展）
- 签到：`mobilelearn.chaoxing.com/pptSign/*`、`newsign/signDetail`（`core/sign.py`、`core/signcode.py`；部分接口只认学习通 App UA，Windows Chrome UA 直接 500）。
- 拍照签到：先传 `pan-yz.chaoxing.com` 拿 objectId 再提交。
