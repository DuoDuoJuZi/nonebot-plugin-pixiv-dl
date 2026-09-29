# 自动发布

## 触发规则

- 推送到 `main` 或提交 PR 时，`.github/workflows/release.yml` 自动运行测试、Ruff 检查并构建源码包与 wheel，产物保存在对应的 GitHub Actions 运行中
- 推送 `vX.Y.Z` 标签时，工作流再次检查和打包，标签版本必须与 `pyproject.toml` 中的 `project.version` 一致，标签提交必须已包含在 `main`
- 标签构建通过后，`publish` 任务等待 `pypi` 环境审批，再通过 PyPI Trusted Publishing 发布到 PyPI
- 普通提交与 PR 只检查和打包，不发布

## 发版步骤

1. 维护者更新 `pyproject.toml` 中的 `project.version`，提交并推送到 `main`
2. 等待对应的 GitHub Actions 检查与打包成功
3. 确认版本和标签尚未发布，在该提交创建同版本标签并推送，例如 `git tag v0.1.0` 和 `git push origin v0.1.0`
4. 在 GitHub Actions 审批 `pypi` 环境的 `publish` 任务，发布后核对 PyPI 页面和安装结果

## 发布配置

- GitHub 环境 `pypi` 需要 Required reviewers 和环境变量 `PYPI_RELEASE_ENABLED=true`
- PyPI Trusted Publisher 绑定仓库 `DuoDuoJuZi/nonebot-plugin-pixiv-dl`、工作流 `release.yml`、环境 `pypi`
- 发布认证使用 OIDC，无需 PyPI API 令牌
