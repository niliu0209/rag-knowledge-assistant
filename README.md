# 知识库问答助手（RAG）

本地私有知识库 RAG 问答助手：上传 PDF/Word 文档，自动解析、切片、向量化入库；提问时检索相关内容，由 LLM 生成**带引用来源**的回答。单机 Docker Compose 部署，数据全部落在本地。

## 功能

- **文档管理**：上传 PDF/Word（≤20MB，可加分类标签：开发调试 / 业务报告 / 其他）、列表查看、单个/批量删除（批量全有或全无：任一失败全部回滚）
- **知识库问答**：向量检索（top-5 + 相关阈值过滤）→ LLM 生成回答，引用来源可展开核对（文档名 + 原文片段 + 页码）；检索无结果时明确回答"知识库中没有相关内容"，不编造
- **会话式多轮**：连续提问可引用上文（最近 10 条对话作为上下文），会话之间相互隔离
- **提供商灵活**：免费预设（SiliconFlow 免费 API：Qwen/Qwen3-14B + BAAI/bge-m3）+ 自带 Key（BYOK：SiliconFlow / DeepSeek / OpenAI / 自定义），统一 OpenAI 兼容协议，LLM 与 embedding 同一抽象层
- **检索质量可评估**：每次问答的检索片段（距离 + 相关性标记）落库，阈值效果持续可回溯调优
- **安全**：API Key 加密存储（Fernet，库中无明文）、数据隔离骨架（user_id 贯穿所有查询）、上传内容隐私说明

## 快速开始

前置：Docker + Docker Compose（Linux / WSL2 / macOS）。

```bash
# 1. 环境变量（可全部留空——默认使用本地 ./data 目录）
cp .env.example .env

# 2. 构建并启动（国内网络可追加 --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple）
docker compose up -d --build

# 3. 打开界面（S2-3 起经 Caddy 反代：本地自动 HTTPS 证书）
#    https://localhost   （首次访问有本地证书提示，点「继续访问」即可）
```

使用流程：**提供商配置**页选择免费预设或填入自己的 Key → **文档上传**页上传 PDF/Word → **问答**页提问（支持连续对话）。未登录可查看「📖 隐私说明」页；页脚备案号由部署方配置。

## 公开部署（邀请制 + HTTPS）

- 本服务设计为可公开部署：邀请码注册（非开放注册）+ Caddy 自动 HTTPS（Let's Encrypt）+ 隐私说明页 + 页脚备案号
- 上线前配置 `.env`：`RAG_COOKIE_SECURE=true`（HTTPS 下 Secure cookie）、`CADDY_DOMAIN=你的域名`（如 example.com；Let's Encrypt 自动签发，需 80/443 公网可达）、`RAG_ICP_NUMBER=备案号`（ICP 备案通过后；公安联网备案通过后补 `RAG_POLICE_NUMBER`）；服务器防火墙放行 80/443
- 访问日志（Caddy）落 `rag-logs` 卷；API 访问/应用日志落 `/data/logs`（rag-data 卷内）

## 架构

```
caddy  (Caddy 2：80/443 反代 + 自动 HTTPS 证书，对外唯一入口；api/ui 栈内不可达)
  │
  └─ ui  (Streamlit，纯客户端：只调 API，不直连数据库、不持有密钥)
       │
       └─ api  (FastAPI：上传 / 解析 / 切片 / 向量化 / 检索 / 问答编排)
            ├─ SQLite  rag.db        文档记录、问答评估记录、加密后的 Key
            ├─ Chroma  knowledge_blocks  向量切片（metadata 带 user_id/doc_id/page）
            └─ data/uploads/         原始文档
```

- 编排层只对外暴露稳定接口：**提问 → 带引用回答**；内部实现可演进（后续可升级 LangGraph 图，调用方无感知）
- 数据模型从第一天带 `user_id` 维度与隔离设计（当前单用户恒为 `default`，公开部署前加认证层即可多用户化）
- 文档分类（开发调试/业务报告/其他）仅为元数据标签，不做差异化解析/切片/提示词

## 技术栈

FastAPI · Streamlit · ChromaDB · SQLite · LlamaIndex（解析/切片原语）· cryptography（Key 加密）· Docker Compose

## 目录结构

```
app/        FastAPI 服务（api 路由薄层 / 业务服务 / 数据层）
ui/         Streamlit 前端（纯客户端）
rag/        文档解析与切片
tests/      pytest 测试套件（123 个用例）
Caddyfile   公开部署反代配置（域名经 env CADDY_DOMAIN 注入）
dev-docs/   项目内部文档（产品边界、架构、阶段实施记录——含设计取舍证据）
```

## 测试

```bash
pytest          # 123 个用例：上传/删除补偿、迁移、加密、检索阈值、多轮历史、批量删除、认证/隔离、请求体限制、备案号页脚
```

## 隐私与安全说明

- **上传的文档内容会发送给你选择的 LLM API 提供商**（免费预设或 BYOK）；请勿上传敏感内容；应用内「📖 隐私说明」页（未登录可看）已如实告知数据流向、Key 加密存储、邀请制范围与删除权利
- **API Key 加密存储**：数据库无明文（`enc$v1$` 前缀密文）；主密钥 `RAG_KEY_ENCRYPTION_KEY` 环境变量注入（未注入时自动生成 `data/secrets/rag_key.bin`，0600 权限）
- **主密钥请妥善保管**：丢失或被替换后已保存的 Key 无法解密，须重新输入；迁移前备份（`rag.db.pre-v003-*.bak`）为唯一回滚路径
- 所有数据落在本地 `data/`（SQLite + Chroma + uploads），`.gitignore` 已排除
- 公开部署安全：仅 80/443 对外（api/ui 栈内）；HSTS/安全头由 Caddy 注入；请求体上限 26MiB（413 兜底）；访问/错误日志落盘且脱敏

## 已知边界（如实说明）

- 检索相关阈值基于当前文档分布实测校准（Chroma 归一化平方 L2 距离 1.025）：标准问法命中 ≤0.927、无关问法 ≥1.031；新文档类型或 embedding 分布漂移时可能误判，评估记录（qa_records）持续监测
- 多轮对话中检索仍基于当前问题，历史仅作上下文理解指代——需引用历史主题的问法（如"它们分别是哪几天？"）可能检索不到，此时诚实回答
- 免费模型政策多变：限流（429）自动退避重试，持续失败请切换预设或配置 Key
