#!/usr/bin/env python3
"""给已有的微信日记文件补 frontmatter 的 author（对应插件设置里的「作者」，契约 v1.7）。

为什么需要它：插件只在**它正在写的那一天**的文件里补 author（`DiaryWriter._ensureAuthor`），
历史文件它永远不会再碰。所以旧库要用这个脚本补一次。

默认**只演习**（打印要改哪些文件），加 `--apply` 才真正写盘。

用法（把作者名换成你的）：
    python3 backfill-author.py --vault ~/Documents/ai_workspace \\
        --rule '黑高日记=heigao' --rule '段日记=duan'
    # 看清楚要改哪些、改几个之后，再：
    python3 backfill-author.py --vault ~/Documents/ai_workspace \\
        --rule '黑高日记=heigao' --rule '段日记=duan' --apply

自检（不需要库，临时目录里跑一遍全套规则）：
    python3 backfill-author.py --selftest

规则（与插件的 `_ensureAuthor` 保持一致，免得两边行为不同）：
- 只在 frontmatter 区间内动（开头 `---` 到闭合 `---`）；没有 frontmatter 的文件跳过
- **已经有 `author:` 的文件跳过** —— 绝不覆盖（可能是你手写的，也可能是模板带的另一个值）
- 默认只补 `source: wechat-diary` 的文件（插件自己建的）。`--include-all` 才连你手写的也补
- 值按 YAML 转义：特殊字符加双引号（不加引号会让整份 frontmatter 解析失败）
- 换行符跟文件本身（CRLF 文件插入 CRLF，不制造混合行尾）
- 不碰正文：除了 frontmatter 里插一行，其余字节原样
"""

import argparse
import os
import re
import sys

FM_DELIM = re.compile(r"^---[ \t]*\r?$")
HAS_AUTHOR = re.compile(r"^author[ \t]*:", re.M)
HAS_SOURCE = re.compile(r"^source:[ \t]*[\"']?wechat-diary[\"']?[ \t]*\r?$", re.M)
# 允许裸写的字符集：中日韩 / 字母 / 数字 / 下划线 / 连字符 / 点 / 空格（首尾不能是空格）
BARE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff.\- ]*$")


def yaml_scalar(value):
    """与 main.js 的 yamlScalar 同一套规则。"""
    s = "" if value is None else str(value)
    if not s:
        return '""'
    if BARE.match(s) and s == s.strip():
        return s
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def frontmatter_end(text):
    """返回闭合 --- 之后的位置；没有 frontmatter 返回 0（与 main.js 的 frontmatterEnd 一致）。"""
    n = len(text)
    e = text.find("\n")
    if e == -1:
        e = n
    if not FM_DELIM.match(text[:e]):
        return 0
    pos = e + 1
    while pos < n:
        le = text.find("\n", pos)
        if le == -1:
            le = n
        if FM_DELIM.match(text[pos:le]):
            return le + 1 if le < n else n
        pos = le + 1
    return 0


def plan_change(text, author):
    """返回改好的文本；不需要改则返回 None。"""
    end = frontmatter_end(text)
    if not end:
        return None                      # 没有 frontmatter：不动
    fm = text[:end]
    if HAS_AUTHOR.search(fm):
        return None                      # 已有 author：绝不覆盖
    m = re.search(r"(^|\n)(---[ \t]*\r?\n)$", fm)
    if not m:
        return None
    nl = "\r\n" if "\r\n" in fm else "\n"
    ins = "author: " + yaml_scalar(author) + nl
    return fm[:m.start()] + m.group(1) + ins + m.group(2) + text[end:]


def scan(root, author, apply, include_all):
    """扫一个树。返回 (统计, 改动清单)。"""
    st = {"total": 0, "changed": 0, "has": 0, "nofm": 0, "notours": 0}
    paths = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]   # 跳过 .obsidian 等
        for name in sorted(filenames):
            if not name.endswith(".md"):
                continue
            path = os.path.join(dirpath, name)
            try:
                with open(path, "r", encoding="utf-8", newline="") as f:
                    text = f.read()
            except (UnicodeDecodeError, OSError) as e:
                print("  ⚠️ 读不了，跳过：" + path + "  (" + str(e) + ")")
                continue
            st["total"] += 1
            end = frontmatter_end(text)
            if not end:
                st["nofm"] += 1
                continue
            if HAS_AUTHOR.search(text[:end]):
                st["has"] += 1
                continue
            if not include_all and not HAS_SOURCE.search(text[:end]):
                st["notours"] += 1
                continue
            new = plan_change(text, author)
            if new is None or new == text:
                continue
            paths.append(path)
            st["changed"] += 1
            if apply:
                with open(path, "w", encoding="utf-8", newline="") as f:
                    f.write(new)
    return st, paths


def backfill(vault, rules, apply=False, include_all=False):
    total = {"total": 0, "changed": 0, "has": 0, "nofm": 0, "notours": 0}
    for folder, author in rules:
        root = os.path.join(vault, folder) if folder else vault
        if not os.path.isdir(root):
            print("⚠️ 跳过不存在的文件夹：" + root)
            continue
        print("\n=== " + (folder or "(库根)") + "  →  author: " + author + " ===")
        st, paths = scan(root, author, apply, include_all)
        for p in paths:
            print(("  ✏️  " if apply else "  ·  ") + os.path.relpath(p, vault))
        for k in total:
            total[k] += st[k]
    return total


def selftest():
    import tempfile

    ok = True

    def chk(name, cond, extra=""):
        nonlocal ok
        print(("  ✓ " if cond else "  ✗ ") + name + ("" if cond else "  → " + str(extra)))
        ok = ok and bool(cond)

    PLAIN = "---\ndate: 2026-09-12\nweekday: 周六\nsource: wechat-diary\n---\n\n# 2026-09-12\n\n**10:00**\n\n内容\n"
    CRLF = "---\r\ndate: 2026-09-12\r\nsource: wechat-diary\r\n---\r\n\r\n# x\r\n\r\n**10:00**\r\n\r\n内容\r\n"
    with tempfile.TemporaryDirectory() as d:
        def put(rel, s):
            p = os.path.join(d, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8", newline="") as f:
                f.write(s)
            return p

        def get(rel):
            with open(os.path.join(d, rel), "r", encoding="utf-8", newline="") as f:
                return f.read()

        a = put("甲/2026/a.md", PLAIN)                                     # 该补
        b = put("甲/2026/b.md", PLAIN.replace("source: wechat-diary", "source: wechat-diary\nauthor: someone"))  # 已有
        c = put("甲/2026/c.md", "# 我手写的\n")                             # 没 frontmatter
        e = put("甲/2026/d.md", "---\ndate: x\n---\n\n# 别人的\n")          # 非插件建
        f = put("甲/2026/e.md", CRLF)                                      # CRLF 该补
        put(".obsidian/g.md", PLAIN)                                       # 隐藏目录不扫
        put("甲/2026/h.txt", PLAIN)                                        # 非 md

        print("— 演习（不写盘）")
        st, _ = scan(os.path.join(d, "甲"), "duan", False, False)
        chk("只挑出该补的 2 个", st["changed"] == 2 and st["total"] == 5, st)
        chk("已有 author 记 1 个", st["has"] == 1, st)
        chk("没 frontmatter 记 1 个", st["nofm"] == 1, st)
        chk("非插件建记 1 个", st["notours"] == 1, st)
        chk("演习没有真的写盘", get("甲/2026/a.md") == PLAIN)
        chk(".obsidian 没被扫到(total=5)", st["total"] == 5, st["total"])

        print("— 真写")
        st2, _ = scan(os.path.join(d, "甲"), "duan", True, False)
        chk("写了 2 个", st2["changed"] == 2, st2)
        chk("author 插在 source 之后、--- 之前", "source: wechat-diary\nauthor: duan\n---\n" in get("甲/2026/a.md"), get("甲/2026/a.md")[:90])
        chk("正文一字不动", get("甲/2026/a.md").endswith("**10:00**\n\n内容\n"), get("甲/2026/a.md")[-30:])
        chk("已有 author 不被覆盖", "author: someone" in get("甲/2026/b.md") and "author: duan" not in get("甲/2026/b.md"))
        chk("没 frontmatter 的没被动", get("甲/2026/c.md") == "# 我手写的\n")
        chk("非插件建的默认没被动", get("甲/2026/d.md") == "---\ndate: x\n---\n\n# 别人的\n")
        chk("CRLF 文件用 CRLF 插入", "author: duan\r\n---\r\n" in get("甲/2026/e.md"), repr(get("甲/2026/e.md")[:70]))
        chk("CRLF 文件没有混合行尾", bool(re.search(r"author: duan\n", get("甲/2026/e.md"))) is False)

        print("— --include-all")
        st3, _ = scan(os.path.join(d, "甲"), "duan", True, True)
        chk("连非插件建的也补", st3["changed"] == 1 and "author: duan" in get("甲/2026/d.md"), st3)

        print("— 值要转义 + 空值")
        chk("带冒号 → 加引号", yaml_scalar("a: b") == '"a: b"', yaml_scalar("a: b"))
        chk("中文/普通名裸写", yaml_scalar("黑高") == "黑高" and yaml_scalar("duan") == "duan")
        chk("首尾空白 → 加引号", yaml_scalar("  x  ") == '"  x  "', yaml_scalar("  x  "))
        chk("空值 → 空串", yaml_scalar("") == '""' and yaml_scalar(None) == '""')
        p = put("乙/x.md", PLAIN)
        scan(os.path.join(d, "乙"), "a: b", True, False)
        chk("写进文件的值真的被引号包住", 'author: "a: b"' in get("乙/x.md"), get("乙/x.md")[:90])

    print("\n自检" + ("全部通过" if ok else "有失败"))
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description="给已有的微信日记文件补 frontmatter author")
    ap.add_argument("--vault", help="库根目录（绝对路径或 ~ 开头）")
    ap.add_argument("--rule", action="append", default=[], metavar="文件夹=作者",
                    help="可重复；相对库根的文件夹 + 该文件夹下文件的作者名")
    ap.add_argument("--apply", action="store_true", help="真正写盘（默认只演习）")
    ap.add_argument("--include-all", action="store_true",
                    help="连没有 source: wechat-diary 的 .md 也补（默认只补插件建的）")
    ap.add_argument("--selftest", action="store_true", help="在临时目录里跑一遍全套规则，不需要库")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if not args.vault:
        ap.error("要么给 --vault，要么用 --selftest")
    if not args.rule:
        ap.error("至少给一条 --rule '文件夹=作者'")
    rules = []
    for r in args.rule:
        if "=" not in r:
            ap.error("--rule 的格式是 '文件夹=作者'：" + r)
        folder, author = r.split("=", 1)
        rules.append((folder.strip().strip("/"), author.strip()))

    vault = os.path.expanduser(args.vault)
    if not os.path.isdir(vault):
        print("库目录不存在：" + vault, file=sys.stderr)
        return 2

    t = backfill(vault, rules, args.apply, args.include_all)
    print("\n扫描 " + str(t["total"]) + " 个 .md；" + ("已补 " if args.apply else "待补 ") + str(t["changed"]) + " 个")
    print("跳过：已有 author " + str(t["has"]) + " 个 · 没有 frontmatter " + str(t["nofm"]) +
          " 个 · 非插件建的 " + str(t["notours"]) + " 个" +
          ("（要连它们也补：加 --include-all）" if t["notours"] and not args.include_all else ""))
    if not args.apply and t["changed"]:
        print("\n确认无误后加 --apply 真正写入。建议先备份那个文件夹。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
