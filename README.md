# PaperLab monorepo

当前版本：**0.5.0**。默认通过 SSH 隧道进行双向增量同步，详见
[SSH 同步配置与恢复](docs/ssh-sync.md) 和 [更新记录](CHANGELOG.md)。

本仓库包含两个完全独立、可分别安装和发布的应用：

- [`paperlab_app/`](paperlab_app/)：本地 Mac 的唯一交互入口。
- [`paperlab_local/`](paperlab_local/)：App 内置的 Python 数据与任务 sidecar，负责采集、
  离线管理、PDF、摘要中文概要、Markdown 精读笔记及增量同步，不再提供浏览器页面。
- [`paperlab_publish/`](paperlab_publish/)：运行在云服务器，负责公网只读页面、
  管理员编辑和增量同步服务端。

两个项目没有 Python 或运行时依赖关系，不能互相 import、读取数据库或访问文件目录。
唯一边界是 [`protocol/`](protocol/) 中描述的版本化 HTTPS 协议。该目录不是共享代码包。

## 独立安装

```bash
make install-local
make install-publish
```

两个应用使用不同的命令、虚拟环境、配置和发布周期：

```bash
npm --prefix paperlab_app run dev
paperlab_publish/.venv/bin/paperlab-publish web serve
```

## 验证

```bash
make test
```

验证包括两个项目各自的测试套件，以及禁止跨项目 import 的边界检查。
