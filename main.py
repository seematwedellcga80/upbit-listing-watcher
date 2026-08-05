#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Upbit 上架交易对监控 + 邮件通知

- 轮询 Upbit 官方公告接口，过滤「新交易对上架」类公告
- 发现新公告 → 通过 SMTP 发送邮件到指定邮箱
- 已处理公告 ID 持久化到 state.json（由 GitHub Actions 提交回仓库）
- 纯标准库实现，零第三方依赖

运行前需要环境变量（GitHub Actions secrets）：
    SMTP_HOST   如 smtp.qq.com / smtp.163.com
    SMTP_PORT   默认 465（SSL）
    SMTP_USER   发件邮箱完整地址
    SMTP_PASS   邮箱 SMTP 授权码（不是登录密码！）
    MAIL_TO     收件邮箱

可选：
    UPBIT_NOTICE_PAGES   拉取公告页数（默认 3，每页 20 条）
"""

import json
import os
import re
import smtplib
import sys
import time
import urllib.request
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

STATE_FILE = "state.json"
STATE_MAX_IDS = 200  # state 中最多保留的已处理公告 id 数

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# Upbit 公告 API 候选端点（依次尝试，接口结构可能随官方调整）
NOTICE_ENDPOINTS = [
    "https://api-manager.upbit.com/api/v1/notices?page={page}&per_page={per}&thread_name=general",
    "https://upbit.com/api/v1/notices?page={page}&per_page={per}&thread_name=general",
    "https://api.upbit.com/api/v1/notices?page={page}&per_page={per}&thread_name=general",
]

# ── 上架公告关键字（命中任意一个即可能是新交易对上架）────────────────────
LISTING_KEYWORDS = [
    "거래지원",            # 交易支持（通用）
    "거래 지원",           # 交易支持（带空格）
    "상장",               # 上市
    "신규",               # 新（新币/新交易对常带）
    "listing",
    "listed",
    "trading support",
    "new coin",
    "new trading",
    "digital asset",
]

# ── 排除关键字（命中任意一个则视为"非上架"，避免误报）──────────────────
EXCLUDE_KEYWORDS = [
    "종료",               # 终止（如 거래지원 종료 안내 = 交易支持终止公告）
    "중단",               # 暂停
    "일시",               # 临时
    "폐지",               # 废除（상장폐지 = 退市）
    "delist",
    "delisting",
    "delisted",
    "halt",
    "maintenance",
    "점검",               # 维护/检查
    "system check",
    "서비스 종료",         # 服务终止
    "이벤트",             # 活动公告（非上架）
    "점검 안내",
]

# 从公告标题/内容中提取上架市场（KRW/BTC/USDT）
MARKET_PATTERN = re.compile(r"\b(KRW|BTC|USDT)\b")
MARKET_ORDER = ["KRW", "BTC", "USDT"]


# ─────────────────────────── 公告过滤（纯函数，便于测试）───────────────────────────
def is_listing_notice(title: str, content: str = "") -> bool:
    """判断一条公告是否为「新交易对上架」。"""
    text = (title or "") + " " + (content or "")
    upper = text.upper()
    has_listing = any(k.upper() in upper for k in LISTING_KEYWORDS)
    has_exclude = any(k.upper() in upper for k in EXCLUDE_KEYWORDS)
    return has_listing and not has_exclude


def extract_markets(title: str, content: str = "") -> list:
    """从公告中提取涉及的上架市场（KRW/BTC/USDT），去重并按固定顺序返回。"""
    found = set(MARKET_PATTERN.findall((title or "") + " " + (content or "")))
    return [m for m in MARKET_ORDER if m in found]


# ─────────────────────────── 网络请求 ───────────────────────────
import http.cookiejar
import urllib.error

# 模拟真实浏览器的完整请求头，绕过 Cloudflare 基础风控
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://upbit.com/service_center/notice",
    "Origin": "https://upbit.com",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-site",
    "sec-ch-ua": '"Not/A)Brand";v="8", "Chromium";v="126", "Google Chrome";v="126"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "Connection": "keep-alive",
}

_cookie_jar = http.cookiejar.CookieJar()
_opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(_cookie_jar),
    urllib.request.HTTPRedirectHandler(),
)


def warm_up_cookies():
    """先访问公告页，获取 Cloudflare cookie，再请求 API。失败不影响后续尝试。"""
    try:
        req = urllib.request.Request(
            "https://upbit.com/service_center/notice", headers=BROWSER_HEADERS
        )
        _opener.open(req, timeout=30).read(1024)
        print("[info] cookie 预热完成")
    except Exception as e:  # noqa: BLE001
        print(f"[warn] cookie 预热失败（继续尝试 API）: {e}", file=sys.stderr)


def fetch_json(url: str, timeout: int = 30) -> dict:
    """GET 请求并解析 JSON，失败抛异常。"""
    req = urllib.request.Request(url, headers=BROWSER_HEADERS)
    with _opener.open(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    try:
        return json.loads(raw)
    except Exception:
        # 输出实际响应内容（截断），便于远程诊断
        print(f"::error::非JSON响应，实际内容（前400字符）: {raw[:400]!r}")
        raise


def parse_notices(data) -> list:
    """从公告 API 响应中提取公告列表，兼容多种返回结构。"""
    # 常见结构 1: {"data": {"list": [...]}}
    # 常见结构 2: {"list": [...]}
    # 常见结构 3: {"data": [...]}
    # 常见结构 4: [...] 直接是列表
    if isinstance(data, list):
        items = data
    else:
        items = []
        if isinstance(data, dict):
            d = data.get("data")
            if isinstance(d, dict):
                for key in ("list", "notices", "items"):
                    v = d.get(key)
                    if isinstance(v, list):
                        items = v
                        break
                if not items and any(isinstance(v, list) for v in d.values()):
                    items = next(v for v in d.values() if isinstance(v, list))
            elif isinstance(d, list):
                items = d
            elif isinstance(d, dict):
                for key in ("list", "notices", "items"):
                    v = d.get(key)
                    if isinstance(v, list):
                        items = v
                        break
            else:
                for key in ("list", "notices", "items"):
                    v = data.get(key)
                    if isinstance(v, list):
                        items = v
                        break
    return items


def fetch_notices(pages: int, per_page: int = 20) -> list:
    """依次尝试各端点，拉取前 pages 页公告，返回公告 dict 列表。"""
    warm_up_cookies()
    last_err = None
    for endpoint in NOTICE_ENDPOINTS:
        try:
            all_items = []
            for page in range(1, pages + 1):
                url = endpoint.format(page=page, per=per_page)
                data = fetch_json(url)
                items = parse_notices(data)
                if not items:
                    break
                all_items.extend(items)
                if len(items) < per_page:  # 最后一页
                    break
            if all_items:
                return all_items
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code} {e.reason}"
            print(f"[warn] 端点 {endpoint} HTTP {e.code}: {e.reason}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            last_err = repr(e)
            print(f"[warn] 端点 {endpoint} 请求失败: {e}", file=sys.stderr)
        time.sleep(2)
    raise RuntimeError(f"所有公告端点均请求失败: {last_err}")


def notice_id(item) -> str:
    """提取公告 id（兼容 id / notice_id / uid 字段）。"""
    for key in ("id", "notice_id", "uid", "no"):
        v = item.get(key)
        if v is not None:
            return str(v)
    return ""


# ─────────────────────────── 状态持久化 ───────────────────────────
def load_state() -> set:
    if not os.path.exists(STATE_FILE):
        return set()
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f).get("notified_ids", []))
    except Exception:
        return set()


def save_state(notified_ids: set):
    ids = sorted(notified_ids)[-STATE_MAX_IDS:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump({"notified_ids": ids}, f, ensure_ascii=False, indent=2)


# ─────────────────────────── 邮件发送 ───────────────────────────
def build_email_body(items: list) -> str:
    lines = [
        "Upbit 检测到新的上架交易对公告！",
        "=" * 60,
    ]
    for it in items:
        title = it.get("title", "（无标题）")
        nid = notice_id(it)
        created = it.get("created_at") or it.get("createdAt") or ""
        content = it.get("content") or it.get("body") or ""
        markets = extract_markets(title, content)
        lines.append(f"公告标题：{title}")
        if markets:
            lines.append(f"涉及市场：{' / '.join(markets)}")
        if created:
            lines.append(f"发布时间：{created}")
        lines.append(f"公告链接：https://upbit.com/service_center/notice?id={nid}")
        lines.append("-" * 60)
    lines.append(
        "说明：本通知由 GitHub Actions 定时轮询 Upbit 官方公告生成，"
        "发布时间可能有 5~15 分钟延迟，请以 Upbit 官方公告为准。"
    )
    return "\n".join(lines)


def send_email(subject: str, body: str):
    host = os.environ.get("SMTP_HOST", "")
    port = int(os.environ.get("SMTP_PORT", "465"))
    user = os.environ.get("SMTP_USER", "")
    pwd = os.environ.get("SMTP_PASS", "")
    to = os.environ.get("MAIL_TO", "")
    missing = [k for k, v in {
        "SMTP_HOST": host, "SMTP_USER": user, "SMTP_PASS": pwd, "MAIL_TO": to
    }.items() if not v]
    if missing:
        raise RuntimeError(f"缺少环境变量: {', '.join(missing)}")

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = formataddr((str(Header("Upbit Listing Watcher", "utf-8")), user))
    msg["To"] = to

    with smtplib.SMTP_SSL(host, port, timeout=30) as s:
        s.login(user, pwd)
        s.sendmail(user, [to], msg.as_string())


# ─────────────────────────── 主流程 ───────────────────────────
def main():
    # 测试模式：只发一封测试邮件，验证 SMTP 配置
    if os.environ.get("TEST_MAIL") in ("1", "true", "yes"):
        send_email(
            "[Upbit] 测试邮件（SMTP 配置验证）",
            "这是一封来自 Upbit Listing Watcher 的测试邮件。\n"
            "如果你收到这封邮件，说明 SMTP 配置正确，可以正常接收上架公告通知。\n"
            "（本邮件为手动触发，非公告通知）",
        )
        print("[ok] 测试邮件已发送")
        return

    pages = int(os.environ.get("UPBIT_NOTICE_PAGES", "3"))

    notified = load_state()
    # 首次运行（无状态文件）：只初始化，不发送历史公告，避免轰炸
    first_run = not os.path.exists(STATE_FILE)

    print(f"[info] 拉取公告（前 {pages} 页）...")
    items = fetch_notices(pages=pages)
    print(f"[info] 共获取 {len(items)} 条公告")

    new_listings = []
    for it in items:
        nid = notice_id(it)
        if not nid:
            continue
        if nid in notified:
            continue
        title = it.get("title", "")
        content = it.get("content") or it.get("body") or ""
        if is_listing_notice(title, content):
            new_listings.append(it)
        notified.add(nid)

    # 也把本次拉到的所有 id 记入状态，防止下次重复
    for it in items:
        nid = notice_id(it)
        if nid:
            notified.add(nid)

    save_state(notified)
    print(f"[info] 已处理公告数：{len(notified)}，新上架公告：{len(new_listings)} 条")

    # 输出检测摘要为 GitHub 注释（便于远程诊断）
    print(f"::notice::共获取 {len(items)} 条公告 | 新上架 {len(new_listings)} 条 | 已处理 {len(notified)} 个id")
    for it in items[:3]:
        title = it.get("title", "")
        print(f"::notice::最近公告: {title[:80]} | 上架匹配: {is_listing_notice(title, it.get('content') or '')}")

    if first_run:
        print("[info] 首次运行，仅初始化状态，本次不发送邮件（从下一次起监控新公告）")
        return

    if not new_listings:
        print("[info] 没有新的上架公告")
        return

    subject = f"[Upbit] 新上架交易对公告 ×{len(new_listings)}"
    body = build_email_body(new_listings)
    print(body)
    send_email(subject, body)
    print("[ok] 邮件已发送")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        msg = f"{e}"
        print(f"[error] {msg}", file=sys.stderr)
        # 输出为 GitHub Actions 注释（可在 run 页面查看，也便于远程诊断）
        print(f"::error::Upbit watcher 失败: {msg}")
        sys.exit(1)
