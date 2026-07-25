# PaperLab

PaperLab 是一个面向 CS 顶会的本地论文数据库、检索、深读报告和 Topic 综述工具。代码托管在 GitHub；SQLite、PDF、单篇 reports、图片和 Topic reviews 通过私有 S3 兼容对象存储同步，不进入 Git 历史。

## 功能

- 增量采集 23 个 AI、体系结构、网络、存储和系统顶会论文。
- 按标题、作者、单位、摘要、关键词、会议和年份检索。
- 下载论文 PDF，生成中文深读 report。
- 将论文收藏到 Topic，并基于已有 reports 生成 Topic review。
- 使用 Cloudflare R2、AWS S3、MinIO 等私有存储跨设备同步数据。

## 快速安装

支持 macOS、Linux 和 Windows WSL，要求 Python 3.10 以上；推荐 Python 3.13 和 [uv](https://docs.astral.sh/uv/)。

```bash
git clone https://github.com/james-plur/LocalPaperLab.git
cd LocalPaperLab

curl -LsSf https://astral.sh/uv/install.sh | sh
# 或一步：make install
uv venv .venv --python 3.13
uv pip install -e ".[sync]"

cp topics.example.yaml topics.yaml
cp .env.example .env
```

也可用 Makefile：

```bash
make install
```

激活环境：

```bash
source .venv/bin/activate              # macOS / Linux / WSL
# Windows PowerShell: .venv\Scripts\Activate.ps1
```

启动一个空的本地实例：

```bash
paperlab web serve
```

浏览器访问 <http://127.0.0.1:8765>。

## 配置

`topics.yaml` 中的相对路径以配置文件所在目录为基准。默认配置将所有运行数据放在仓库工作目录：

```yaml
mcp:
  command: uvx
  args: [paper-search-mcp]

pdf_root: .
db_path: data/papers.db
```

LLM API key 可以写入 `.env`，也可以在 Web 页面 LLM 设置中保存。页面保存的 key 位于权限为 `0600` 的 `data/llm_config.json`，不会返回浏览器，也不会同步或提交 GitHub。

## 日常使用

```bash
paperlab update                       # 增量更新最近会议
paperlab update --full                # 扫描完整年份范围
paperlab search "kv cache transfer"
paperlab download <paper-id>
paperlab report <paper-id>
paperlab web serve
```

macOS 可安装后台 Web 服务：

```bash
paperlab web install
paperlab web status
paperlab web remove
```

## 配置 Cloudflare R2

1. 在 Cloudflare 控制台创建一个私有 R2 bucket，例如 `paperlab-data`。
2. 创建只允许访问该 bucket 的 R2 API token。
3. 复制 S3 endpoint、Access Key ID 和 Secret Access Key。
4. 写入本机 `.env`：

```dotenv
PAPERLAB_SYNC_ENDPOINT=https://ACCOUNT_ID.r2.cloudflarestorage.com
PAPERLAB_SYNC_BUCKET=paperlab-data
PAPERLAB_SYNC_REGION=auto
PAPERLAB_SYNC_PREFIX=
AWS_ACCESS_KEY_ID=YOUR_ACCESS_KEY_ID
AWS_SECRET_ACCESS_KEY=YOUR_SECRET_ACCESS_KEY
```

使用 AWS S3 时，将 endpoint 留空并配置正确 region；boto3 也支持标准 AWS profile、IAM role 和环境变量认证。

## 首台设备：首次上传

先查看同步计划，不产生远端写入：

```bash
paperlab sync push --dry-run
```

确认后上传：

```bash
paperlab sync push
paperlab sync status
```

Push 会：

1. 通过 SQLite Backup API 创建一致性数据库快照。
2. 使用 Zstandard 压缩数据库。
3. 对 PDF、reports、figures 和 reviews 计算 SHA-256。
4. 只上传相对上一个 manifest 发生变化的文件。
5. 先上传内容对象，最后原子更新 `manifests/latest.json`。

远端目录采用内容寻址，不会因为本地重命名而重复上传相同文件：

```text
database/<sha256>.db.zst
pdfs/<sha256>.pdf
artifacts/<sha256>.<ext>
reviews/<sha256>.md
manifests/history/<revision>.json
manifests/latest.json
```

## 新设备：恢复数据

安装并配置 `.env` 后：

```bash
paperlab sync pull --force
paperlab web serve
```

默认恢复数据库、reports、figures 和 reviews，不下载全部 PDF。使用 Web 打开缺失 PDF 时仍可从论文来源重新下载。

需要完整复制所有 PDF：

```bash
paperlab sync pull --all --force
```

首次 `--force` 是因为新 clone 创建的本地数据库尚未与远端 revision 关联。Pull 替换数据库前会在 `data/` 下创建带时间戳的本地备份。

如果数据库自上次同步后有本地修改，pull 会拒绝覆盖；应先 push，或者在确认远端版本应覆盖本地版本后使用 `--force`。

## 多设备工作流

SQLite 不进行逐行自动合并。每次修改前先 pull，完成后再 push：

```bash
paperlab sync pull
paperlab web serve
# 添加论文、Topic、report 或 review
paperlab sync push
```

执行 pull 前必须停止本机 `paperlab web serve` 和其他正在写数据库的 PaperLab 进程；push 使用 SQLite 在线快照，可以在 Web 服务运行时执行。

如果其他设备已推送新 revision，本机 push 会拒绝覆盖：

```text
remote data changed since the last pull; run `paperlab sync pull` first
```

不要在两台设备上同时生成或编辑数据。`--force` 只用于明确知道远端或本地副本应被覆盖的恢复场景。

## 同步命令

```bash
paperlab sync status               # 比较本地与远端 revision
paperlab sync push --dry-run       # 统计待上传内容
paperlab sync push                 # 上传数据库和变更文件
paperlab sync pull                 # 拉取数据库、reports、reviews
paperlab sync pull --all           # 同时拉取全部 PDF
paperlab sync gc                   # 列出未被最新 manifest 引用的对象
paperlab sync gc --apply           # 删除这些远端对象
```

`gc` 默认只预览。历史 manifest 永久保留，但清理后的旧 manifest 可能引用已删除内容，因此执行 `--apply` 前应确认不再需要回滚到旧 revision。

## GitHub 托管

仓库只提交代码和模板。以下内容已被 `.gitignore` 排除：

- `data/*`
- `pdfs/*`
- `reports/*`
- `.env`
- LLM 和对象存储凭据
- 本地同步状态

创建 GitHub 仓库后：

```bash
git remote add origin git@github.com:james-plur/LocalPaperLab.git
git branch -M main
git push -u origin main
```

`.github/workflows/ci.yml` 会在 Python 3.10–3.13 上运行编译和测试，并检查运行数据与密钥没有进入仓库。

## 从旧 Git LFS 仓库迁移

PaperLab 不再使用 Git LFS。若旧历史曾提交数据库或 PDF，应在首次推送 GitHub 前处理：

1. 确认 `paperlab sync push` 已成功，且能在临时目录完成一次恢复。
2. 备份整个仓库或创建 Git bundle。
3. 从 Git 索引和历史中移除 `data/`、`pdfs/`、`reports/`。
4. 清理本地 `.git/lfs` 对象。

仓库提供了带确认门槛和恢复 bundle 的脚本。必须先提交或暂存好当前工作、保证工作树干净，并安装 `git-filter-repo`：

```bash
uv tool install git-filter-repo
PAPERLAB_CONFIRM_HISTORY_REWRITE=YES scripts/clean_lfs_history.sh
```

脚本先创建 `paperlab-before-lfs-clean-<timestamp>.bundle`，再从全部历史移除数据库、PDF、reports 和 `.gitattributes`，最后清理不可达 Git/LFS 对象。确认 GitHub 推送和云端恢复都正常之前不要删除 bundle。

历史改写会改变所有 commit ID。不要在尚未验证云端数据时执行，也不要在其他协作者已经基于旧历史开发后直接强制推送。

## 数据安全

- Bucket 必须保持私有，并使用最小权限 token。
- `.env`、`data/llm_config.json` 和 `data/sync-state.json` 不同步。
- Web 服务默认只监听 `127.0.0.1`。
- Pull 会校验数据库和每个文件的 SHA-256。
- 数据库通过临时文件恢复并使用 `os.replace` 原子替换。
- 发布或共享 PDF 前应确认论文版权许可；私有同步不等同于获得公开再分发授权。

## 测试

```bash
python -m compileall -q paperlab
python -m unittest discover -s tests
```

测试使用临时 SQLite 和模拟对象存储，不访问正式 bucket。
