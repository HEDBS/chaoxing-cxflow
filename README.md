# cxflow · 学习通（超星）作业 CLI

**中文** | [English](#english) · [下载 Release](https://github.com/HEDBS/chaoxing-cxflow/releases/latest)

把超星学习通上散落在几十门课里的作业，变成一条可脚本化、可被 AI Agent 调用的流水线：
**登录 → 读课程任务 → 领卷 → 解题 → 提交 → 风控处置**。

纯 Python 标准库，零第三方依赖；协议来自桌面版助手 v0.4.3 的真环境实测（每个字段都有实测注释）。
**默认 dry-run**：不加 `--confirm` 绝不真的交卷。

> 仅供个人学习管理与自动化技术研究。默认保留人工确认闸门，请保留它。

## 特性

- 🔑 **登录**：手机号+密码 AES-128-CBC 加密登录，cookie 落盘续用，过期自动重登
- 📋 **读任务**：官方学习规划聚合接口（1 个请求拿全部临期任务）+ 逐课 `getAllWork` 全量扫描
- 🔎 **三态审核**：只读领卷验真 —— 可作答 / 非作业 / 读不到题 / 需手写，带缓存
- 🤖 **解题**：任意 OpenAI 兼容端点；单选/多选/判断出字母，填空/简答按「像学生手写」约束生成
- 🖼️ **识图**：本地 PaddleOCR-VL（llama.cpp，免费离线）为主，云多模态兜底，全挂则转人工
- ✅ **提交**：真协议 `addStudentWorkNewWeb`，填空题按空发 UEditor 字段，**默认 dry-run**
- 🛡️ **风控**：（【9010】罚站）自动识别 + 冷却重登自愈；验证码只做「取图给人看 + 代提交」，**不自动识别**
- 🧩 **可被 Agent 调用**：stdout 只有 JSON，退出码语义化（0/2/3/4/5），附 `docs/AGENT-SKILL.md` 技能文档

## 快速开始

要求：Python 3.8+（无需 pip install 任何东西）。

```bash
git clone https://github.com/HEDBS/chaoxing-cxflow.git && cd chaoxing-cxflow

python cxflow.py selftest                 # 离线自检（14 项：AES 向量/卷子解析/表单构造）
python cxflow.py login --user 手机号 --pwd 密码
python cxflow.py tasks                    # 临期任务（JSON）
python cxflow.py tasks --refresh --limit 5   # 实时：逐课全量扫（每课 2 请求，慢）
python cxflow.py audit --refresh --limit 5   # 三态审核：哪些真能做
python cxflow.py pull --course-id C --class-id K --cpi P --work-id W --out job.json
python cxflow.py solve --in job.json --out ans.json          # 解题（图题走识图链）
python cxflow.py submit --in job.json --answers ans.json                 # dry-run：只打印将发送的表单
python cxflow.py submit --in job.json --answers ans.json --confirm       # 真提交
python cxflow.py captcha get --out captcha.png               # 风控：取验证码图（人工看）
python cxflow.py captcha post --code 1234                    # 人工回填（以回验为准）
```

一条最小流水线：

```bash
python cxflow.py tasks --refresh --limit 10 > tasks.json
python cxflow.py pull --course-id ... --class-id ... --cpi ... --work-id ... --out job.json
python cxflow.py solve --in job.json --out ans.json
python cxflow.py submit --in job.json --answers ans.json      # 先看 dry-run 结果
# 确认无误后加 --confirm
```

## 命令

| 命令 | 作用 | 网络 |
|---|---|---|
| `login` | 登录并落盘 cookie（`--force` 强制重登） | 2 请求 |
| `tasks` | 临期任务聚合；`--refresh` 加逐课全量扫 | 1 或 1+2N |
| `audit` | 三态审核（只 GET 领卷验真，带缓存） | N |
| `pull` | 领卷 → `job.json`（题目 + 提交上下文） | 2–3 |
| `solve` | 解题 → `ans.json` | 每题 1 |
| `submit` | 构造/发送提交（默认 dry-run） | 0 或 2 |
| `captcha` | 风控验证码取图/回填 | 1 |
| `selftest` | 离线自检 | 0 |

退出码：`0` 成功 · `2` 被风控 · `3` 掉线 · `4` 无凭据 · `5` 接口错。

## 数据目录与登录

- 默认 `%APPDATA%\cx-pilot`（非 Windows 退 `~/cx-pilot`；目录名沿用桌面版），可用 **`CX_DATA`** 覆盖。
- `cookies.txt` / `credentials.json` 都在这里；凭据以 0600 落盘，删除目录即彻底清除。
- 与桌面版助手共用同一份 cookie：桌面版登录过，CLI 直接可用。

## 解题后端（任意 OpenAI 兼容端点）

优先级：命令行 `--base-url/--model/--key` > 环境变量 `CX_LLM_BASE_URL/CX_LLM_MODEL/CX_LLM_KEY` > 数据目录下的 `settings.json`。
代理默认**直连**（`CX_PROXY` 显式配置才走）。

```bash
python cxflow.py solve --in job.json --out ans.json \
  --base-url https://api.example.com/v1 --model your-model --key sk-xxxx
```

**识图**：`CX_LOCAL_VLM_BASE=http://127.0.0.1:8081`（llama.cpp + PaddleOCR-VL，本地免费）
→ 云多模态 `CX_VISION_BASE_URL/CX_VISION_MODEL/CX_VISION_KEY` 兜底 → 都没有则图题标 `need_manual`。
本地 VLM 的提示词**必须就是 `OCR:`**（约束型 prompt 实测更差）。

## 文档

- [`docs/chaoxing-api.md`](docs/chaoxing-api.md) —— 全部接口清单（URL/参数/响应/坑）
- [`docs/risk-control.md`](docs/risk-control.md) —— 风控三态、自愈级联、验证码出口
- [`docs/solve-and-vision.md`](docs/solve-and-vision.md) —— 提示词原文、答案提取、识图链、手写题判定
- [`docs/AGENT-SKILL.md`](docs/AGENT-SKILL.md) —— 给 AI Agent 的技能文档（如何驱动本工具）

## 常见问题

**会不会不经过我就提交？** 不会。默认 dry-run，只有显式 `--confirm` 才真正发提交请求。

**为什么提交要 `--confirm` 两道？** 学习通作业通常只能交一次、交了不可撤；先 dry-run 看将发送的字段再决定。

**被风控了怎么办？** 报 `【9010】` 就是撞上图片验证码。`captcha get` 取图、眼睛看、`captcha post --code`；
或者到学习通 App/网页正常登录一次。风控跟 IP 热度走，等 1–2 分钟更稳。**本工具不自动识别验证码。**

**分数/格式？** 学习通多为 AI 评分：填空/简答按「简洁、平实、像学生手写」生成（纯文本、矩阵逐行、分数写 `9/2`），实测优于富文本。

**图片题读不出来？** 配本地 PaddleOCR-VL 最稳；没有就让它们落 `need_manual`，不猜。

## 免责声明

本工具用于个人学习管理与自动化技术研究，使用者需自行确保符合学校规定与平台服务条款。
作者不对账号受限、作业成绩或任何连带后果负责。默认的人工确认闸门与「不自动识别验证码」是有意保留的红线，请勿移除。

## License

MIT — 见 [LICENSE](LICENSE)。

---

<a name="english"></a>

# cxflow · Chaoxing (Xuexitong) Homework CLI

**English** | [中文](#cxflow--学习通超星作业-cli)

A dependency-free Python CLI that turns Chaoxing / Xuexitong homework scattered across dozens of
courses into a scriptable, agent-callable pipeline:
**login → list tasks → fetch a paper → solve → submit → risk-control handling**.

Pure standard library. The protocol is derived from real-environment testing of the desktop helper
v0.4.3 (every field carries a measured note). **Dry-run by default** — nothing is submitted unless
you pass `--confirm`.

> For personal study management and automation research only. The manual confirmation gate is
> intentional; please keep it.

## Features

- 🔑 **Login** — phone + password, AES-128-CBC encrypted; cookies persisted and auto-refreshed
- 📋 **Tasks** — official study-plan aggregate (1 request) plus per-course `getAllWork` full scan
- 🔎 **Audit** — read-only paper fetching to classify: solvable / not homework / unreadable / handwriting-only (cached)
- 🤖 **Solve** — any OpenAI-compatible endpoint; letters for choice/judge, student-style plain text for blanks/essays
- 🖼️ **Vision** — local PaddleOCR-VL via llama.cpp as the main engine, cloud VLM as fallback, manual if all fail
- ✅ **Submit** — real `addStudentWorkNewWeb` protocol (UEditor fields for blanks), **dry-run unless confirmed**
- 🛡️ **Risk control** — detects the `【9010】` captcha block, self-heals with cooldown + re-login; captcha is shown to a
  human and posted back, **never solved automatically**
- 🧩 **Agent-friendly** — JSON on stdout, meaningful exit codes (0/2/3/4/5), plus `docs/AGENT-SKILL.md`

## Quick start

Requires Python 3.8+. No dependencies to install.

```bash
git clone https://github.com/HEDBS/chaoxing-cxflow.git && cd chaoxing-cxflow
python cxflow.py selftest                                   # 14 offline self-checks
python cxflow.py login --user <phone> --pwd <password>
python cxflow.py tasks                                      # upcoming tasks (JSON)
python cxflow.py audit --refresh --limit 5                  # which tasks are actually doable
python cxflow.py pull --course-id C --class-id K --cpi P --work-id W --out job.json
python cxflow.py solve --in job.json --out ans.json
python cxflow.py submit --in job.json --answers ans.json            # dry-run (prints the form to be sent)
python cxflow.py submit --in job.json --answers ans.json --confirm  # actually submit
python cxflow.py captcha get --out captcha.png               # risk control: grab the captcha image
python cxflow.py captcha post --code 1234                    # post it back (verified by re-checking)
```

Exit codes: `0` ok · `2` risk-controlled · `3` session expired · `4` no credentials · `5` API error.

## Data directory

Defaults to `%APPDATA%\cx-pilot` (`~/cx-pilot` off Windows; the name is kept for compatibility),
overridable with **`CX_DATA`**. It holds `cookies.txt` and `credentials.json` (mode 0600).
Sharing the same directory lets the CLI reuse a desktop helper's session.

## Solver backend

Priority: CLI flags `--base-url/--model/--key` > env `CX_LLM_BASE_URL/CX_LLM_MODEL/CX_LLM_KEY` >
`settings.json` in the data directory. Direct connection by default (`CX_PROXY` opts into a proxy).
Image questions: `CX_LOCAL_VLM_BASE=http://127.0.0.1:8081` (llama.cpp + PaddleOCR-VL, free & offline;
the prompt must be exactly `OCR:`), then a cloud vision endpoint, else `need_manual`.

## Docs

- [`docs/chaoxing-api.md`](docs/chaoxing-api.md) — every endpoint, parameter, response and pitfall
- [`docs/risk-control.md`](docs/risk-control.md) — the three failure modes, self-heal cascade, captcha exit
- [`docs/solve-and-vision.md`](docs/solve-and-vision.md) — prompts, answer extraction, vision chain, handwriting detection
- [`docs/AGENT-SKILL.md`](docs/AGENT-SKILL.md) — skill doc for driving this tool with an AI agent

## FAQ

**Will it submit without asking me?** No. It is dry-run unless you pass `--confirm`.

**Why two gates before submitting?** Homework is usually one-shot and irreversible; inspect the
dry-run payload first.

**What if I get risk-controlled?** `【9010】` means a captcha wall: `captcha get`, read it with your
eyes, `captcha post --code`. Or just log in normally in the app/browser. The block follows IP heat —
waiting 1–2 minutes helps. This tool never solves captchas automatically.

**Answer style?** Grading is mostly AI-based: answers are generated plain, concise and student-like
(no markdown, matrices row by row, fractions as `9/2`).

## Disclaimer

For personal study management and automation research. You are responsible for complying with your
school's rules and the platform's terms of service. The manual confirmation gate and the refusal to
auto-solve captchas are deliberate red lines — please keep them.

## License

MIT — see [LICENSE](LICENSE).
