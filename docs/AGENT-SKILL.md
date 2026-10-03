---
name: chaoxing-homework-agent
description: Use when 让 agent 登录超星学习通读课程任务/领卷解题/一键提交/处理风控。含自包含 CLI（scripts/cxflow.py），协议来自真环境实测。
platforms: [windows, linux, macos]
metadata:
  hermes:
    tags: [chaoxing, xuexitong, mooc, homework, submit, risk-control, chaoxing-api]
    editorial_name: 超星学习通作业代理
    editorial_description: 登录超星账号 → 读课程任务 → 领卷 → 解题 → 提交 → 风控处置，附真协议接口清单。
---

# 超星（学习通）作业代理

给别的 agent 用的完整能力：**登录 → 读课程任务 → 领卷 → 解题 → 一键提交 → 风控处置**。
协议知识来自桌面版助手 v0.4.3 的实测实现（每个字段都带真环境实测注释）。

## 红线（先读，别越）
1. **提交默认 dry-run**：只有显式 `--confirm` 才真发 POST。没有人工确认闸门就别批量提交。
2. **不自动识别验证码**——那属于绕过人机验证。风控只做「取图给人看 + 代提交」，判据是**回验**（课程列表不再被拦），不信响应文案。
3. **限速**：扫课每课 0.6~1.4s，解题每题 1.0~2.5s，**不要并发**。风控跟 IP 热度走，请求越密越容易被【9010】罚站。
4. 账号是用户本人的：凭据落 `%APPDATA%\cx-pilot\credentials.json`（0600），别外传、别写进日志。

## 快速开始（scripts/cxflow.py，纯标准库，无需安装）
```bash
S="<skill>/scripts/cxflow.py"          # 本技能目录下的 scripts/
python "$S" selftest                    # 离线自检（AES 向量/卷子解析/表单构造，14 项）
python "$S" login --user 手机号 --pwd 密码      # 也可只写 credentials.json
python "$S" tasks                       # 临期任务（stat2 聚合，1 个请求）；stdout 是 JSON
python "$S" tasks --refresh --limit 5   # 实时：再逐课全量扫 getAllWork（每课 2 请求，慢）
python "$S" audit --refresh --limit 5   # 三态审核：可作答 / 非作业 / 读不到题 / 需手写（只 GET，带缓存）
python "$S" pull --course-id C --class-id K --cpi P --work-id W --out job.json
python "$S" solve --in job.json --out ans.json     # 后端见下；图题走识图链
python "$S" submit --in job.json --answers ans.json                 # dry-run：只打印将发送的表单
python "$S" submit --in job.json --answers ans.json --confirm       # 真提交
python "$S" captcha get --out captcha.png          # 风控：取验证码图
python "$S" captcha post --code 1234               # 人工看图后回填（回验通过才算过）
```
- **数据目录**：默认 `%APPDATA%\cx-pilot`（非 Windows 退 `~/cx-pilot`），`CX_DATA` 可覆盖；与桌面版助手共用 cookies.txt / credentials.json，互不干扰。
- **输出约定**：stdout 只有 JSON（agent 直接 parse），进度与告警走 stderr。退出码：`0` 成功 / `2` 风控 / `3` 掉线 / `4` 无凭据 / `5` 接口错。
- **解题后端**（优先级：命令行 flag > 环境变量 > 桌面版助手的 settings.json）：
  `--base-url/--model/--key` 或 `CX_LLM_BASE_URL/CX_LLM_MODEL/CX_LLM_KEY`。任意 OpenAI 兼容端点。
  代理默认**直连**，只有 `CX_PROXY`/settings.proxy 显式配置且端口探活通过才走。
- **识图后端**：`CX_LOCAL_VLM_BASE=http://127.0.0.1:8081`（llama.cpp + PaddleOCR-VL，本地免费，主引擎）
  → `CX_VISION_BASE_URL/CX_VISION_MODEL/CX_VISION_KEY`（云多模态兜底）→ 都没有则图题落 `need_manual`。

## 五步链路（每步真正的坑）

| 步 | 接口 | 要害 |
|---|---|---|
| 1 登录 | `GET passport2/login` → `POST passport2/fanyalogin` | 账号密码都要 **AES-128-CBC/PKCS7，key=iv=`u2oh6Vu^HWe4_AES`** 后 base64（`scripts/aes128.py`）；表单还有 fid/refer/t/forbidotherlogin 等 hidden 要照抄；成功判据 `status` |
| 2 读任务 | `GET stat2-ans.../stat2/learning/plan/recommended-course-list` | 1 个请求拿全部**临期**任务 + 课程进度；官方每 4h 刷新。实时/全量要逐课 `/work/getAllWork`。任务 url 里的 courseid/clazzid/cpi 要**分别**解析（活动类任务常缺 clazzid，整体返空会炸后续） |
| 3 领卷 | `GET stat2 .../getWorkStuUrl` → `GET mooc1 /mooc-ans/mooc2/work/prompt` | 契约已变：getWorkStuUrl 对已排期作业 **302 直落 dowork 页**（不是 JSON），两种都要吃。领卷这一跳也会被 9010 打中，必须走自愈壳。`isExpire` 的 **referer 必须是 mooc1**（stat2 referer 会被风控成非 JSON）。这个接口还带 `standardEnc`，提交要用 |
| 4 解题 | 题图 `p.ananas.chaoxing.com` + 任选 LLM | 题型四条线索兜底：`answertype{qid}` 输入 > 容器 `typeName` > 题面 `(单选题, 2分)` 标记 > 保守主观题。题干实义字符 < 8 说明题在图里 → 识图。详见 references/solve-and-vision.md |
| 5 提交 | `POST mooc1 /mooc-ans/work/addStudentWorkNewWeb?…&ua=pc&formType=1&saveStatus=1` | 见下方「提交字段铁律」 |
| 6 风控 | `GET processVerifyPng.ac` → `POST /html/processVerify.ac` | 见 references/risk-control.md |

## 提交字段铁律（2026-09-28~29 真提交实测定案）
- `answerwqbid` **必须 = qid 逗号串 + 尾逗号**（如 `5001,5003,5004,`）。卷子页 hidden 里它是**空串**，照抄会被拒「无效的参数：code-1」。
- 填空（`answertype{qid}=2`）：每空一个 **`answerEditor{qid}{n}`**（n 从 1 开始），内容是 **UEditor HTML**（`<p>文本</p>`，纯文本按行包 p）；同时发 `tiankongsize{qid}`；**不要**发 `answer{qid}` 纯文本，服务端拒收。
- 判断（`answertype{qid}=3`）：`answer{qid}` 必须是页面 `data` 原值 **`true`/`false`**（不是字母 A/B、不是「对/错」）。
- 单选 `answertype=0` / 多选 `1`，`answer{qid}` 是选项字母串（多选按字母序、无分隔，如 `ABD`）；主观题 `4`。
- URL 上 `version` 要 +1；`form_action` 从卷子页 `<form id="submitForm" action=…>` 抠（自带 token/totalQuestionNum），别自己拼。
- 提交前可先 `GET work/validate?courseId&classId&cpi`：`status=3` 可直提，`2` 需过验证码。
- 二次提交同卷报「已批阅作业，不允许重复提交」→ 这是**已入库的反证**，不是失败。已完成/待批阅的作业别再去解（会重复打开已交卷）。
- `limitWorkSubmitTimes` 是提交次数上限（卷子页 hidden），超了会被拒。

## 答案风格（学习通多为 AI 评分）
简洁、平实、**像学生手写**的纯文本：禁「首先/其次/综上所述」，禁 markdown 加粗与分点，矩阵按行空格分列、换行分排，分数写 `9/2`。提示词原文见 references/solve-and-vision.md（实测比富文本得分高）。

手写题（题干含「手写/拍照上传/纸上作答」…）**不能自动作答** → 标 `need_manual`，别浪费模型调用。

## 实战坑（都已踩过，别再踩）
- **共享缓存会缺字段**：`audit_cache.json` 与桌面版助手共用，老条目没有新字段 → 读缓存必须 `setdefault`，否则 KeyError 打断整轮审核。
- **stat2 也会被【9010】罚站**：只 catch `SessionExpired/ApiError` 会让 `RiskControl` 直接冒到调用方，下游精心写的自愈根本没机会跑。所有请求都走自愈壳。
- **裸 `raw_get` 是历史 bug 源头**：领卷第一跳必须用带自愈的版本，否则异常秒抛、冷却都没跑到。
- **mooc1 侧会话过期会招来风控，而 stat2 仍报 `code==0`** → `ensure_login` 会误判「已登录」永不重登。所以要靠级联里的 `recover_session`（丢 cookie 全新登录）兜底。
- **已完成/待批阅的作业不要重开**：会二次打开已交卷（`getAllWork` 里 `status` 已给出）。


## 参考
- `references/chaoxing-api.md` —— 全部接口清单（URL/参数/响应/坑）
- `references/risk-control.md` —— 风控三态、自愈级联、验证码出口
- `references/solve-and-vision.md` —— 提示词、选型、识图链、手写题判定
- `scripts/cxflow.py` / `scripts/aes128.py` —— 可直接跑的 CLI（`selftest` 自查）
