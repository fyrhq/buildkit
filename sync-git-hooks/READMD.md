# Git 同步 hooks

安装到**接收 push 的服务端仓库**，通常是 bare 仓库。

## 行为

- 没有配置 `origin`：允许接收，输出 `[remote] remote origin not config!`，不检查或转发。
- 更新分支：fetch 对应 origin 分支，确认远端 tip 是收到 commit 的祖先；否则拒绝整次 push。
- origin 没有对应分支：允许接收，接收成功后在 origin 创建分支。
- 删除分支：仅删除接收仓库的分支，不删除 origin 分支。
- 新建或更新 tag：接收成功后与分支一起推送 origin，不强制覆盖已有 tag。
- 删除 tag：当前也只在接收仓库生效，不转发删除操作。
- 其他 ref：不检查、不转发。
- 查询或 fetch 失败：拒绝 push，并输出 `[remote]` 错误原因。

`pre-receive` 在 Git quarantine 环境中通过 `FETCH_HEAD` 检查刚 fetch 的远端 tip，不更新 `refs/remotes/origin/*`。这避免在接收检查期间写入引用，也避免使用过期的跟踪分支。

`post-receive` 使用本次成功接收的对象 ID 和完整 ref 名转发，输出 `[remote] push to remote/origin`。转发失败时报告错误，接收仓库已经接收的变更仍然保留。暂不处理检查后 origin 被其他人更新的并发情况。

## 安装

在希望保存脚本的目录执行以下命令，从 GitHub 下载三个文件到**当前目录**，并赋予执行权限（需要 Bash 和 curl）：

```bash
bash -c 'set -euo pipefail; files=(pre-receive post-receive install-sync-hook.sh); for f in "${files[@]}"; do if [[ -e "$f" || -L "$f" ]]; then echo "文件已存在：$f" >&2; exit 1; fi; done; d=$(mktemp -d); trap "rm -rf -- \"\$d\"" EXIT; for f in "${files[@]}"; do curl -fsSL --retry 3 "https://raw.githubusercontent.com/fyrhq/buildkit/main/sync-git-hooks/$f" -o "$d/$f"; done; for f in "${files[@]}"; do install -m 755 "$d/$f" "./$f"; done'
```

下载完成后，为目标仓库安装：

```sh
./install-sync-hook.sh /absolute/path/to/receiving.git
```

如果使用本项目中的文件，则从项目根目录运行下面的命令。

运行安装脚本，唯一参数是接收仓库路径（支持相对路径和包含空格的路径）：

```sh
./git-sync-hooks/install-sync-hook.sh /absolute/path/to/receiving.git
```

脚本将目标仓库的本地 `core.hooksPath` 设置为本目录的绝对路径，不复制文件，因此安装后请保留本目录的位置。重复安装到同一路径会直接成功。若已有其他 `core.hooksPath` 或默认目录中存在可执行 hook，脚本会停止且不修改配置，需先确定如何整合。Git 自带的 `*.sample` 不影响安装。脚本不会配置或修改 origin。

服务端运行 Git 的账号需要具备访问 origin 的凭据及写权限。origin 应指向上游仓库，避免指向自身或形成循环同步。

Git 经 SSH 等传输时可能自动再添加 `remote:` 前缀；脚本自身的消息前缀仍为 `[remote]`。
