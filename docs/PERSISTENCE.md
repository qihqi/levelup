# 牌桌存档与部署

服务使用 Python 标准库 SQLite，启动时自动建表，无需额外数据库服务或依赖。
默认文件是 `~/.local/share/levelup/state.sqlite3`（支持 `XDG_DATA_HOME`）；
可用 `LEVELUP_DB_PATH` 指向运行用户可写的固定路径。不要放在 `static/` 下。
继续使用一个 Uvicorn worker：SQLite 负责恢复存档，活动连接和房间锁仍属于单个进程。

## 已有 systemd 服务

若此前按 Raspberry Pi 部署步骤开启了 `ProtectSystem=strict` 或 `ProtectHome=true/read-only`，
请给数据库增加专用可写目录。在 `/etc/levelup.env` 中加入：

```ini
LEVELUP_DB_PATH=/var/lib/levelup/state.sqlite3
```

在 `levelup.service` 的 `[Service]` 中加入：

```ini
StateDirectory=levelup
StateDirectoryMode=0700
```

`systemd` 会创建 `/var/lib/levelup`，设置为该服务运行用户所有，并允许服务写入，
即使其他系统目录被设为只读。保留已有 `User=`、`EnvironmentFile=`、日志目录和启动命令。
无需更改 uv 的安装流程，也无需更改 nginx/Caddy 的静态文件或 WebSocket 转发配置。

```sh
sudo systemctl daemon-reload
sudo systemctl restart levelup
sudo systemctl status levelup
```

## 保存与恢复的范围

- 首次创建或加入牌桌时生成随机访客身份，身份及昵称保存在浏览器 localStorage。
  服务器保存身份的 SHA-256 哈希；昵称与成员关系存入 SQLite。
- 每次状态变更先提交存档再广播，包括逐张摸牌。保存手牌、底牌、剩余牌堆、随机数状态、
  当前及历史出牌、比分、级别、座位、创建者、AI 设置和剩余计时。
- 任何真人断开后暂停整桌，取消进行中的 AI 搜索；全部真人归队后从剩余时间继续。
  服务重启后的所有真人都视为离线，即使之前开启托管，也需要归队。
- 创建者保留房主权限，可移除其他真人，让 AI 接管原手牌。被移除者失去恢复该座位的权限；
  若仍在等待开局，可作为新成员再次加入空位。
- 六小时无人在线仅卸载内存，不删存档。完成整场比赛的桌子不出现在未完成列表，数据仍保留。
  目前没有自动清理数据库的功能。

数据库包含全部私有手牌、底牌和单桌恢复令牌，文件权限设为 `0600`；新建数据目录为 `0700`。
不要发布数据库或备份。运行中的 SQLite 使用 WAL，不应只复制主文件来备份。
可以用 SQLite 备份 API；例如以服务运行用户执行（需已安装 `sqlite3` 命令，备份目录也应私有）：

```sh
sqlite3 /var/lib/levelup/state.sqlite3 ".backup '/private-backups/levelup.sqlite3'"
```

恢复备份前停止服务，保存当前数据库及其 `-wal` / `-shm` 文件到别处，
将备份放到 `LEVELUP_DB_PATH`，设置正确的服务用户所有权与 `0600` 权限，再启动服务。
不要把旧的 WAL 文件和恢复后的数据库混用。

访客身份不是跨设备账户。浏览器清除网站数据或更换协议、域名、端口后，无法自动找回身份；
只知道昵称或房间号不能恢复已经开局的座位。旧版仅存在内存中的桌子无法在首次升级时迁移。
未来若修改存档格式，需要为当前 `schema: 1` 显式添加迁移逻辑。
