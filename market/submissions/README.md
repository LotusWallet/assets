# DApp 收录申请表

把项目信息提交到这里，通过后就会出现在 LotusWallet 的「市场」Tab 里。

## 怎么提交

1. 复制 `_TEMPLATE.json`，重命名为 `<你的项目 id>.json`（小写字母、数字、连字符）。
2. 按里面的注释填写。**请删掉模板里的 `_readme` 字段**。
3. 向本仓库提 Pull Request。CI 会跑 `tools/build_market.py --check` 做格式与一致性校验。
4. 合并后 `market/dapps.json` 自动重新生成，客户端下次刷新即可看到 —— 不需要发新版 App。

## 收录标准

- 项目已上线运行，且有公开可查的信息与真实使用量（**不接受品牌新项目**）。
- 有官方网站，`url` 与官网域名一致。
- 合约地址、审计报告、源码（如有）尽量一并给出 —— 给得越全，审核越快。
- 不接受：仿盘、赌博、未经披露的资金盘、任何要求用户交出助记词/私钥的站点。

## 字段说明

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `id` | ✅ | 唯一标识，小写字母/数字/连字符。与文件名一致。**合并后不可更改**。 |
| `name` | ✅ | 展示名称。 |
| `url` | ✅ | 打开地址，必须 https。 |
| `category` | ✅ | 取值见 `market/curated.json` 的 `categories`。 |
| `description` | ✅ | 40-100 字中文说明；有英文版更好（另开 `descriptionEn`）。 |
| `chains` | ⬜ | 支持的链 id 列表，取值见 `chains/chains.json`。不确定就留空数组。 |
| `tags` | ⬜ | 便于搜索的关键词。 |
| `logo` | ⬜ | 方形图标 URL，256×256 或 400×400，jpg/png。 |
| `audits` / `sourceCode` / `socials` / `contact` | ⬜ | 审核参考，不对外展示。 |

## 下架

如果项目方希望下架，或我们发现条目违规，会从 `market/curated.json` /
`market/submissions/` 中移除对应条目并重新生成产物 —— 客户端在下次刷新后生效，
**不需要用户升级 App**。
