# buildkit

`deploy_helm.py` 打包业务仓库的 Helm 模板，触发指定仓库的部署 workflow，并等待对应运行结束。

触发前使用同一个 `GH_TOKEN` 读取目标 workflow，确认 CI 凭据能够访问它。GitHub API 失败时输出
请求方法、请求地址、HTTP 状态码、GitHub 错误说明、请求 ID，以及返回的权限和 SSO 提示。
如果请求被重定向，还会记录最终响应地址。不会输出 Token、完整请求体或原始非 JSON 错误页面，
失败的部署 POST 不自动重试。

读取 workflow 失败时不会触发部署；读取成功后 POST 失败可进一步判断触发权限或输入问题。
这些检查使用 CI 中实际保存的 Token；其他设备上 GitHub CLI 登录成功不能代替此项验证。

离线回归验证：`python3 -B -m unittest discover -s tests -v`。
