# buildkit

`deploy_helm.py` 打包业务仓库的 Helm 模板，触发指定仓库的部署 workflow，并等待对应运行结束。

镜像输入使用 `--image-info '<file>=<repository>@sha256:<64位小写十六进制摘要>'`。
需要同时更新版本信息时，可传 `<file>=<repository>:<tag>@sha256:<digest>`；Helm 保存
repository、tag 和完整 digest，但容器只使用 `repository@sha256:<digest>`。
摘要取自构建推送步骤的 `outputs.digest`，多架构镜像使用最终 image index 的摘要。

过渡期仍接受旧的 `<file>=<repository>:<tag>` 输入，由 Helm 接收端使用已认证的
registry 查询解析为完整摘要。解析失败时停止发布，不回退为 tag 部署；私有 CNB 镜像
需要在 Helm 仓库配置 `DOCKER_REPO_KEY`，私有 GHCR 镜像需要具有读取权限的 Token。

一次发布多个镜像时，使用 `--image-updates-file release-images.json`，文件内容是 UTF-8
JSON 列表，每条指定 `file`、`path`（从 Values 根开始的键名数组）、`repository`、完整
`digest` 和可选的 `tag`。例如 Agency 的目标分别是：

```text
file=agency, path=["agency", "image"]
file=agency, path=["agency", "builtinWorker", "executionImage"]
```

两个目标分别提供各自构建推送返回的 digest。新输入必须包含完整 digest，不做 registry 查询；
与非空 `--image-info` 互斥。空列表、重复或重叠目标、非法字段、未知字段和重复 JSON 键均报错，
不会发起 dispatch。列表最多 100 条，键路径最多 32 层，最终 inputs JSON 最多 65,535 字符。
目标对象和待写字段必须已存在，结构初始化需另行确认；省略 tag 时保留原值。

共享脚本保持单文件下载。其多镜像输入校验器与 Helm 的 `scripts/image_updates.py`
遵循同一契约，修改时需同步维护。完整输入限制、Agency 可复制调用示例和发布顺序见
Helm 仓库 `PROJECT_HELM_ACTION_GUIDE.md` 的“单次发布多个镜像”一节。
先发布 Helm 接收能力，再发布此脚本，最后接入业务工作流；旧单镜像调用方无需迁移。

触发前使用同一个 `GH_TOKEN` 读取目标 workflow，确认 CI 凭据能够访问它。GitHub API 失败时输出
请求方法、请求地址、HTTP 状态码、GitHub 错误说明、请求 ID，以及返回的权限和 SSO 提示。
如果请求被重定向，还会记录最终响应地址。不会输出 Token、完整请求体或原始非 JSON 错误页面，
失败的部署 POST 不自动重试。

读取 workflow 失败时不会触发部署；读取成功后 POST 失败可进一步判断触发权限或输入问题。
这些检查使用 CI 中实际保存的 Token；其他设备上 GitHub CLI 登录成功不能代替此项验证。

脚本等待 dispatch 返回的准确 workflow run ID，Action 成功时提示查看该运行的 summary。
部署暂停时成功仅代表同步和 Chart 校验完成；未暂停时成功仅代表提交了异步部署请求，
均不表示集群升级或认证行为验收完成。

离线回归验证：`python3 -B -m unittest discover -s tests -v`。
