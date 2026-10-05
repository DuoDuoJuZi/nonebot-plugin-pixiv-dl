# 文档

- `README.md` 面向普通用户，只保留安装、配置、指令、主要行为和必要注意事项
- 不在 README 中展开实现细节，例如内部流程、接口调用、编码参数、线程、缓存、超时和临时文件处理
- 新功能只补充用户实际需要知道的内容，避免因实现变化顺带扩写 README

# 自动发布

## 触发规则

- 推送到 `main` 或提交 PR 时，`.github/workflows/release.yml` 自动运行测试、Ruff 检查并构建源码包与 wheel，产物保存在对应的 GitHub Actions 运行中
- 推送 `vX.Y.Z` 标签时，工作流再次检查和打包，标签版本必须与 `pyproject.toml` 中的 `project.version` 一致，标签提交必须已包含在 `main`
- 标签构建通过后，`publish` 任务校验 `pypi` 环境变量，再通过 PyPI Trusted Publishing 自动发布到 PyPI
- 普通提交与 PR 只检查和打包，不发布

## 发版步骤

1. 维护者更新 `pyproject.toml` 中的 `project.version`，提交并推送到 `main`
2. 等待对应的 GitHub Actions 检查与打包成功
3. 确认版本和标签尚未发布，在该提交创建同版本标签并推送，例如 `git tag v0.1.0` 和 `git push origin v0.1.0`
4. 等待标签工作流发布成功，核对 PyPI 版本和发行文件

## 发布配置

- GitHub 环境 `pypi` 保留环境变量 `PYPI_RELEASE_ENABLED=true`，当前未设置 Required reviewers，推送版本标签即确认发布
- PyPI Trusted Publisher 绑定仓库 `DuoDuoJuZi/nonebot-plugin-pixiv-dl`、工作流 `release.yml`、环境 `pypi`
- 发布认证使用 OIDC，无需 PyPI API 令牌
