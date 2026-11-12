# ReadLess 宿主接入代码

本目录保存研究助手在完整 ReadLess 项目中的页面、服务桥接、资料同步和相关测试，按宿主相对路径组织。它不是独立网站，不能在此目录直接运行 `npm start`。

- `src/`：研究对话、引用显示、Markdown 渲染、本地文件夹设置及进度提示。
- `scripts/`：研究页面构建、同源 API 桥接、网站资料与文件夹索引管理。
- `research-agent/scripts/`：读取 ReadLess 当前账号和公开资料的同步程序。
- `tests/`：需要完整 ReadLess 宿主环境的 Node／浏览器回归测试。
- `docs/`：资料来源和文件夹功能约定。

这些模块依赖宿主已有的账号／文章存储、`local-request.mjs`、`research-mode.mjs`、构建工具、品牌资源及部分未修改的研究组件。本仓库没有复制文章库、个人草稿、图片、账号数据、API 密钥、同步缓存、SQLite 数据库或浏览器记录。

合入 ReadLess 时按相对路径逐项同步，并保留宿主分支的运行模式、Compose 挂载及私有配置路径。独立后端默认读取 `.secrets/secrets.json`；ReadLess 宿主通过 `RA_SECRETS_FILE` 指向自己的私有配置，不能把两套默认路径直接互换。

独立后端不需要这些模块也能使用 CLI/API、单目录 RAG 和研究流程。要接入额外资料库，需要只读提供相应来源目录和 manifest，并设置后端路径：

| 环境变量 | 内容 |
| --- | --- |
| `RA_USER_FOLDERS_PATH` | 已登记文件夹的只读快照及 `manifest.json` |
| `RA_SITE_SOURCES_PATH` | 网站快照、来源清单及有效权限清单 |
| `RA_READLESS_ARTICLES_PATH` | 本地已发布原创文章 |
| `RA_READLESS_CLOUD_PATH` | 当前账号同步得到的文章资料 |

网站权限核对和同步凭据由宿主负责；Python 检索器不会自行登录网站或扩展用户权限。来源协议见 [资料范围](docs/research-rag-sources.md) 和 [文件夹入口](docs/research-folders.md)。

本目录的前端测试在原 ReadLess 宿主中执行。独立仓库的 `uv run pytest tests/unit -q` 只验证 Python 后端，不冒称验证了完整网站。
