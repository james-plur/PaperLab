# PaperLab

PaperLab 是一个面向 CS 顶会的增量论文数据库与检索工具。

## 核心功能

### 1. 信息源更新

```bash
paperlab update -c topics.yaml          # 增量检索最近一年的顶会论文
paperlab update -c topics.yaml --full   # 全量检索五年内的顶会论文
```

采集 23 个 AI/系统顶会（NeurIPS、ICML、ICLR、AAAI、IJCAI、KDD、MLSys、ACL、EMNLP、
NAACL、CVPR、ICCV、ECCV、ISCA、MICRO、HPCA、ASPLOS、SIGCOMM、NSDI、INFOCOM、FAST、
OSDI、SOSP）的完整主会名录，逐级补全标题、作者、单位、摘要、DOI、arXiv ID、
主页链接和关键词。

### 2. 定向检索

```bash
paperlab search "kv cache transfer"             # 英文
paperlab search "KV cache传输优化"               # 中文
```

Web 搜索框输入关键词即时筛选；按 Enter 切换语义搜索（基于关键词变体匹配 +
空格不敏感匹配，支持中英文混合输入）。首次检索到的论文会通过 LLM 提取关键词
并存入数据库，使后续检索越来越高效。关键词可手动编辑：

```bash
paperlab keywords set <paper_id> "kw1,kw2,kw3"
paperlab keywords add <paper_id> "new_keyword"
paperlab keywords batch                        # 批量提取缺失关键词
```

### 3. 论文下载

```bash
paperlab download <paper_id>       # 按 ID 下载
paperlab download "Mooncake"       # 按标题关键词下载
```

Web 页面每篇论文卡片有 Download 按钮，点击即下载。下载顺序：arXiv 直链 →
arXiv 标题搜索 → USENIX proceedings → DOI 直链。

## Web UI

```bash
paperlab web serve                 # 启动 Web 服务 (http://127.0.0.1:8765)
paperlab web install               # 安装为 macOS LaunchAgent
```

功能：分页浏览、关键词搜索、语义搜索（回车触发）、会议/年份筛选、
每篇论文的 PDF 下载/打开、关键词和元信息在线编辑并持久化到数据库。

## 安装

```bash
cd /Users/luchenda/tools/paperlab
uv venv .venv --python 3.13
uv pip install -e .
cp .env.example .env   # 填入 API key
```

## 配置

`topics.yaml` 配置会议列表、年份范围、LLM 模型和数据路径。
LLM 默认使用 cc-switch 本地代理 (`http://127.0.0.1:15721/v1`)，
也可通过 `.env` 中的 `OPENAI_BASE_URL` 和 `OPENAI_API_KEY` 切换到 OpenAI。

## 数据位置

- SQLite: `~/papers/paperlab/data/papers.db`
- PDF: `~/papers/paperlab/pdfs/`
- 日志: `~/papers/paperlab/logs/`

## 技术栈

- Python 3.13, SQLite, OpenAI-compatible LLM API
- 会议名录: DBLP + 官网爬取
- 元数据补全: OpenAlex (DOI 批量) → Semantic Scholar → arXiv → proceedings 爬取
- PDF 下载: arXiv → USENIX → Unpaywall
