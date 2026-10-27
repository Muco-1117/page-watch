# Page Watch

不想反复手动刷新发行说明、公告页或公开价格说明时，用 Page Watch 定期取回页面，比较实际文本并保存差异。只需要 Python 和一个本地 SQLite 数据库。

它会忽略 HTML 脚本和样式，可只监测某个指定 ID 的区域，也可以移除日期等噪声。首次访问建立基线，后续成功检查记录变更；网络错误不会覆盖已有基线。

## 快速开始

需要 Python 3.11+，不需要第三方依赖或付费 API。在项目目录运行：

```powershell
# 用实际公开页面替换示例 URL
python page_watch.py add releases https://example.org/releases

# 第一次建立基线，之后比较变化
python page_watch.py check

# 查看任务和最近的检查状态
python page_watch.py list

# 查看任务 1 最近 100 次基线/变更记录和 unified diff
python page_watch.py history 1
```

只监测 `<main id="content">` 区域，并去掉会每天变化的日期：

```powershell
python page_watch.py add announcements https://example.org/announcements --element-id content --ignore 'Updated: [0-9-]+'
```

`--ignore` 为正则表达式，可重复传入。区域 ID 消失或处理后没有文本时，记为错误并保留旧基线，避免把错误页面当作更新。

## 定时运行

```powershell
python page_watch.py check --interval 300
```

进程运行期间每 300 秒检查一次，`Ctrl+C` 停止。最短间隔为 30 秒，执行耗时另计；所有任务按顺序检查。单个请求超时 15 秒，最大响应 2 MB。支持 ETag 和 Last-Modified 条件请求，服务器返回 304 时不会重复下载内容。

也可在 Windows 任务计划程序或 cron 中定时执行一次性 `check`。程序本身不会创建后台服务，不发送邮件、系统通知或聊天消息，输出为 JSON，便于接入其他脚本。

```powershell
python page_watch.py check --id 1
python page_watch.py remove 1
```

删除任务也会删除该任务的本地历史。若要更改 URL、区域或过滤规则，请新建任务并重新建立基线，避免比较两种不同的提取配置。

## 数据与返回值

默认数据库为相对当前工作目录的 `data/watch.db`；计划任务建议明确指定绝对路径：

```powershell
python page_watch.py --db "D:\private\watch.db" check
```

数据库保存 URL、上次成功的文本、HTTP 缓存标识、检查状态和最近的差异。它可能包含私人关注信息，不要提交或上传。备份时先退出所有监测进程，再备份整个数据库所在目录。

一次性 `check` 退出码：`0` 未变更或首次基线；`1` 有文本变化；`2` 任一检查失败。连续监测模式会逐轮输出状态，手动停止返回 `0`。其他命令成功为 `0`，失败为 `2`。

## 支持范围

- HTML 提取文本、去除脚本/样式/模板，折叠多余空白；JSON 排序格式化后比较；文本按行比较。
- 不执行 JavaScript、不登录、不发送 Cookie、不绕过验证码；适合无需登录即可访问的静态公开页面或 JSON 地址。
- 不识别仅存在于图片中的信息，不判断 CSS 是否隐藏了某个 HTML 节点。
- DOM/正文发生大幅重排可能产生较大的 diff，可使用 `--element-id` 缩小区域。
- 通过基线检查条件请求与缺失区域，但无法自动判断所有 HTTP 200 的登录页或防护页；重要页面应指定稳定区域。
- 不支持同一数据库的多个进程同时检查同一任务；单实例运行即可。
- 监测时间使用 UTC。历史查询最多显示最近 100 条，数据库记录不会自动裁剪。

## 验证

```powershell
python -m unittest discover -s tests -v
python -m py_compile page_watch.py
```

测试使用本地 HTTP 服务，无需访问真实网站，覆盖首次基线、忽略规则、正文差异、失败保留基线、区域缺失、HTML 实体和真实 304 条件请求。GitHub Actions 在 Windows/Linux 与 Python 3.11/3.13 上运行。
