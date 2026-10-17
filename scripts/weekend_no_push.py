#!/usr/bin/env python3
"""周末只更新网站、不推企业微信。

按北京时间判断星期：周六/周日把 config/config.yaml 的 notification.enabled
就地改成 false。只改 CI 检出的工作副本，不提交回仓库。

关掉这个总开关后 TrendRadar 仍然照常抓取、生成 HTML、写存储
（__main__.py 里 HTML 生成只看 STORAGE.FORMATS.HTML），仅跳过发送。
"""

import datetime
import os
import re
import sys

CONFIG_PATH = os.environ.get("CONFIG_PATH", "config/config.yaml")
# 中国无夏令时，固定 +08:00 就是北京时间，省掉对 tzdata 的依赖
BEIJING = datetime.timezone(datetime.timedelta(hours=8))
CN_WEEKDAY = "一二三四五六日"


def is_weekend(now=None):
    now = now or datetime.datetime.now(BEIJING)
    return now.weekday() >= 5, now


def disable_push(path):
    """把 notification 块里的 enabled 改成 false，返回 (是否改动, 原因)。"""
    with open(path, encoding="utf-8") as f:
        lines = f.read().split("\n")

    try:
        head = next(i for i, l in enumerate(lines) if re.match(r"^notification:\s*(#.*)?$", l))
    except StopIteration:
        return False, "找不到顶层 notification: 段"

    body = None
    for i in range(head + 1, len(lines)):
        if re.match(r"^\S", lines[i]):
            break
        if re.match(r"^\s+enabled:", lines[i]):
            body = i
            break
    if body is None:
        return False, "notification 段里找不到 enabled 行"

    new = re.sub(r"^(\s+enabled:\s*)(true|false)(.*)$", r"\1false\3", lines[body])
    if not new.endswith(("false",)) and "false" not in new:
        return False, "enabled 行不是预期写法: %s" % lines[body].strip()
    if new == lines[body]:
        return False, "已经是 false"
    lines[body] = new
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))
    return True, "第 %d 行 → false" % (body + 1)


def main():
    force = None
    if "--force" in sys.argv:
        force = sys.argv[sys.argv.index("--force") + 1]

    weekend, now = is_weekend()
    if force == "weekend":
        weekend = True
    elif force == "weekday":
        weekend = False

    day = "周" + CN_WEEKDAY[now.weekday()]
    print("北京时间 %s（%s）" % (now.strftime("%Y-%m-%d %H:%M:%S"), day))

    if not weekend:
        print("工作日：照常推送企业微信群 + 更新网站")
        return 0

    if not os.path.exists(CONFIG_PATH):
        print("::error::找不到 %s，无法关闭推送" % CONFIG_PATH)
        return 1

    changed, why = disable_push(CONFIG_PATH)
    if not changed and "已经是 false" not in why:
        print("::error::没能定位 notification.enabled（%s）——宁可让这一步红掉，"
              "也不要周末误推群聊" % why)
        return 1

    print("周末：已关闭群推送，只更新 news 网站 · %s" % why)
    print("::notice::周末模式：%s · notification.enabled=false · 只更新网站不推群" % day)
    return 0


if __name__ == "__main__":
    sys.exit(main())
