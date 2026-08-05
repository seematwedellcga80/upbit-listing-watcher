# Upbit 上架交易对监控 → 邮件通知

自动监控 **Upbit 官方公告**，发现**新交易对上架公告**（韩文 `거래지원` / `상장`，英文 listing 等）时，第一时间通过 **QQ 邮箱 / 163 邮箱 SMTP** 发送通知邮件到你的邮箱。

> 为什么用 GitHub Actions：你的网络无法直连 Upbit，GitHub 的服务器在海外，可免费、免代理地定时访问 Upbit 官方公告。

## 工作原理

```
GitHub Actions 定时任务（每 15 分钟）
        │
        ▼
轮询 Upbit 官方公告 API（api-manager.upbit.com）
        │
        ▼
过滤「上架交易对」公告（排除下架/暂停/维护类公告）
        │
        ▼
有新的？──是──► SMTP 发送邮件到你的邮箱（QQ/163）
        │
        否
        ▼
记录已处理公告 ID 到 state.json（提交回仓库，避免重复通知）
```

- 首次运行只初始化状态、**不发送历史公告**，从部署时刻起开始监控新公告。
- 公告发布时间到你收到邮件，通常有 **5~15 分钟**延迟（GitHub Actions 调度延迟所致），已经是免费方案里最快的。

## 部署步骤（约 10 分钟）

### 1. 获取邮箱 SMTP 授权码

**QQ 邮箱**
1. 登录 mail.qq.com → 设置 → 账号
2. 找到「POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV 服务」
3. 开启「IMAP/SMTP 服务」→ 按提示用手机发短信验证
4. 得到一串授权码（类似 `xxxxxxxxxxxxxxxx`），记下来

**163 邮箱**
1. 登录 mail.163.com → 设置 → POP3/SMTP/IMAP
2. 开启「SMTP 服务」→ 按提示验证
3. 得到授权码

> ⚠️ 授权码不是邮箱登录密码！只在下面第 3 步配置到 GitHub Secrets 里，不要泄露。

### 2. 创建 GitHub 仓库并推送

在本地（`策略/upbit-listing-watcher` 目录）：

```bash
cd upbit-listing-watcher

# 初始化 git
git init
git add .
git commit -m "init upbit listing watcher"

# 关联你的 GitHub 仓库（把 USER/REPO 换成你自己的）
git remote add origin https://github.com/USER/REPO.git
git push -u origin main
```

> 仓库设为 **public** 时，GitHub Actions 定时任务**不限次数**（每 15 分钟一次没问题）；
> 设为 **private** 时每月有 2000 分钟免费额度，每 15 分钟一次约消耗 2880 分钟/月，会超额 —— 见下方「调整频率」。

### 3. 配置 Secrets（邮箱信息）

GitHub 仓库页面 → **Settings → Secrets and variables → Actions → New repository secret**，依次添加：

| Secret 名 | 值 | 示例 |
|---|---|---|
| `SMTP_HOST` | 邮箱 SMTP 服务器 | `smtp.qq.com` 或 `smtp.163.com` |
| `SMTP_PORT` | SMTP 端口（SSL） | `465` |
| `SMTP_USER` | 发件邮箱完整地址 | `yourname@qq.com` |
| `SMTP_PASS` | 邮箱**授权码**（不是登录密码） | `xxxxxxxxxxxxxxxx` |
| `MAIL_TO` | 收件邮箱（可与发件相同） | `yourname@qq.com` |

### 4. 手动测试

1. 打开 GitHub 仓库 → **Actions** → 左侧 **upbit-listing-watcher**
2. 点 **Run workflow** → 确认运行
3. 等 1~2 分钟运行完成，到邮箱查收测试邮件（没有新公告时不会发邮件，属正常）

### 5. 验证自动运行

部署后约 15 分钟内，Actions 页面会出现第一次定时运行记录（图标为钟表）。之后有新币/新交易对上架公告时，你会收到邮件。

## 调整运行频率

编辑 `.github/workflows/upbit-watch.yml` 中的 cron 表达式，例如：

| 频率 | cron | 每月消耗（约） | 适用 |
|---|---|---|---|
| 每 15 分钟 | `*/15 * * * *` | 2880 分钟 | public 仓库（免费不限量） |
| 每 30 分钟 | `*/30 * * * *` | 1440 分钟 | private 仓库（额度内） |
| 每 1 小时 | `0 * * * *` | 720 分钟 | 最省额度 |

改完后 commit 并 push，自动生效。

## 常见问题

**Q：手动运行失败，日志显示 `缺少环境变量`**
A：Secrets 没配全，检查第 3 步的 5 个变量是否都添加、名字拼写是否正确。

**Q：报错 `SMTPAuthenticationError`**
A：`SMTP_PASS` 填错或授权码过期，重新生成授权码并更新 Secret。

**Q：一直没有收到邮件**
A：可能是期间确实没有新上架公告（正常，公告频率不高）。手动触发一次 `Run workflow`，在日志里看 `新上架公告：N 条` 与 `邮件已发送`。

**Q：公告 API 结构变了 / 抓取报错**
A：脚本已内置多个端点和容错解析。若 `所有公告端点均请求失败`，说明官方接口变动，可到 Actions 日志查看具体报错后调整 `main.py` 中的 `NOTICE_ENDPOINTS`。

## 说明与免责

- 本项目**非 Upbit 官方出品**，仅轮询公开公告页面，用于个人研究学习。
- 上架公告的发布时间与内容以 Upbit 官网为准；本通知可能存在延迟或解析偏差。
- 交易有风险，新币上线初期波动极大，请注意风险控制。

## 目录结构

```
upbit-listing-watcher/
├── main.py                        # 核心脚本（纯标准库，零依赖）
├── .github/workflows/upbit-watch.yml  # GitHub Actions 定时任务
├── state.json                     # 已处理公告 ID（自动更新，勿手改）
├── .gitignore
└── README.md
```
