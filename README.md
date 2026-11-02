# buildkit

`deploy_helm.py` 打包业务仓库的 Helm 模板，触发指定仓库的部署 workflow，并等待对应运行结束。

镜像输入使用 `--image-info '<file>=<repository>@sha256:<64位小写十六进制摘要>'`。
需要同时更新版本信息时，可传 `<file>=<repository>:<tag>@sha256:<digest>`；Helm 保存
repository、tag 和完整 digest，但容器只使用 `repository@sha256:<digest>`。
摘要取自构建推送步骤的 `outputs.digest`，多架构镜像使用最终 image index 的摘要。

过渡期仍接受旧的 `<file>=<repository>:<tag>` 输入，由 Helm 接收端使用已认证的
registry 查询解析为完整摘要。解析失败时停止发布，不回退为 tag 部署；私有 CNB 镜像
需要在 Helm 仓库配置 `DOCKER_REPO_KEY`，私有 GHCR 镜像需要具有读取权限的 Token。

触发前使用同一个 `GH_TOKEN` 读取目标 workflow，确认 CI 凭据能够访问它。GitHub API 失败时输出
请求方法、请求地址、HTTP 状态码、GitHub 错误说明、请求 ID，以及返回的权限和 SSO 提示。
如果请求被重定向，还会记录最终响应地址。不会输出 Token、完整请求体或原始非 JSON 错误页面，
失败的部署 POST 不自动重试。

读取 workflow 失败时不会触发部署；读取成功后 POST 失败可进一步判断触发权限或输入问题。
这些检查使用 CI 中实际保存的 Token；其他设备上 GitHub CLI 登录成功不能代替此项验证。

离线回归验证：`python3 -B -m unittest discover -s tests -v`。
