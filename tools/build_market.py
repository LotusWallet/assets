#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LotusWallet/assets —— 生成客户端消费的 DApp 市场清单 `market/dapps.json`。

数据来源
--------
    market/curated.json        团队维护的精选条目（含分类定义）
    market/submissions/*.json  社区提交（每个 DApp 一个文件，_TEMPLATE.json 除外）
    chains/chains.json         用于校验条目里写的链 id 是否真实存在

产出
----
    market/dapps.json          客户端 `MarketRepository` 直接解析的结构

为什么要在 CI 里做「生成」而不是手写产物
----------------------------------------
手写产物容易和源数据脱节（改了一个忘了另一个）。这里源数据与产物分离：
提交 PR 只动 submissions/，产物由 CI 重新生成 —— 结果可复现、diff 可 review。

用法
----
    python tools/build_market.py            # 生成 market/dapps.json
    python tools/build_market.py --check    # 只校验，不写文件（CI 用）
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CURATED = REPO / "market" / "curated.json"
SUBMISSIONS = REPO / "market" / "submissions"
CHAINS = REPO / "chains" / "chains.json"
OUT = REPO / "market" / "dapps.json"

# 产物里只暴露客户端真正用的字段；审核参考类字段（contact / audits / socials）
# 不进产物 —— 少暴露一点，就少一份被爬走的可能。
PUBLIC_FIELDS = ("id", "name", "url", "category", "description", "iconUrl", "chains", "tags", "featured", "order")


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def normalize(raw: dict, source: str, problems: list[str]) -> dict | None:
    """把一条原始记录规范成客户端的 MarketDapp 形状。"""
    dapp_id = (raw.get("id") or "").strip()
    if not dapp_id:
        problems.append(f"{source}: 缺少 id")
        return None
    if not (raw.get("name") or "").strip():
        problems.append(f"{source}: 缺少 name")
        return None
    url = (raw.get("url") or "").strip()
    if not url.startswith("https://"):
        problems.append(f"{source}: url 必须是 https（当前：{url!r}）")
        return None

    # 兼容社区提交里常见的 logo/iconUrl 两种写法
    icon = raw.get("iconUrl") or raw.get("logo")

    return {
        "id": dapp_id,
        "name": raw["name"].strip(),
        "url": url,
        "category": (raw.get("category") or "").strip(),
        "description": (raw.get("description") or "").strip() or None,
        "iconUrl": icon,
        "chains": [c for c in (raw.get("chains") or []) if isinstance(c, str)],
        "tags": [t for t in (raw.get("tags") or []) if isinstance(t, str)],
        "featured": bool(raw.get("featured", False)),
        "order": int(raw.get("order", 100)),
        # 人工/团队试过的条目才置 true，客户端会打「已试用」角标
        "reviewed": bool(raw.get("reviewed", False)),
    }


def build(problems: list[str]) -> dict:
    curated = load(CURATED)
    categories = curated["categories"]
    valid_categories = {c["id"] for c in categories}

    known_chains = {c["id"] for c in load(CHAINS)["chains"]}

    entries: dict[str, dict] = {}
    for raw in curated["dapps"]:
        d = normalize(raw, "market/curated.json", problems)
        if d is None:
            continue
        # 精选清单里的重复 id 也必须报错。此前只查了 submissions，
        # 团队自己的清单反而没人查 —— 而这里重复的后果是**静默的**：
        # entries 按 id 去重，后写覆盖先写，条目凭空少一条且不报任何问题。
        if d["id"] in entries:
            problems.append(f"market/curated.json: id {d['id']!r} 重复出现")
            continue
        entries[d["id"]] = d

    for path in sorted(SUBMISSIONS.glob("*.json")):
        if path.name.startswith("_"):
            continue
        d = normalize(load(path), f"market/submissions/{path.name}", problems)
        if d is None:
            continue
        if d["id"] in entries:
            problems.append(f"market/submissions/{path.name}: id {d['id']!r} 与已有条目冲突")
            continue
        if path.stem != d["id"]:
            problems.append(f"market/submissions/{path.name}: 文件名应与 id（{d['id']}）一致")
        entries[d["id"]] = d

    dapps = []
    for d in entries.values():
        if d["category"] not in valid_categories:
            problems.append(f"{d['id']}: category {d['category']!r} 不在 curated.json 的 categories 里")
        unknown = [c for c in d["chains"] if c not in known_chains]
        if unknown:
            # 不算致命：链可能还没进白名单（例如 Solana 这类暂不支持的链）。
            print(f"  ! {d['id']}: 引用了未知链 id {unknown}（客户端会忽略）", file=sys.stderr)
        # 只暴露公开字段，且去掉空值时保持产物精简
        dapps.append({k: d[k] for k in PUBLIC_FIELDS if d.get(k) not in (None, [], "")})

    dapps.sort(key=lambda x: (x.get("order", 100), x["id"]))

    return {
        "version": 1,
        "updatedAt": now_iso(),
        "source": "https://github.com/LotusWallet/assets",
        "categories": categories,
        "dapps": dapps,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 DApp 市场清单")
    parser.add_argument("--check", action="store_true", help="只校验，不写文件")
    args = parser.parse_args()

    problems: list[str] = []
    doc = build(problems)

    if problems:
        print("发现问题：", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        if args.check or any("url 必须" in p or "缺少" in p for p in problems):
            return 1

    if args.check:
        if not OUT.exists():
            print(f"缺少产物 {OUT.relative_to(REPO)}", file=sys.stderr)
            return 1
        existing = load(OUT)
        if existing.get("version") != doc["version"] or len(existing.get("dapps", [])) != len(doc["dapps"]):
            print("产物与源数据不一致，请重跑 tools/build_market.py", file=sys.stderr)
            return 1
        print(f"校验通过（{len(doc['dapps'])} 个 DApp）")
        return 0

    text = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
    OUT.write_bytes(text.replace("\r\n", "\n").encode("utf-8"))
    print(f"已生成 {OUT.relative_to(REPO)}：{len(doc['dapps'])} 个 DApp，{len(doc['categories'])} 个分类")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
