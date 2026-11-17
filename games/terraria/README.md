# 泰拉瑞亚 PC 原版服务器

此目录是独立的 Docker Compose 项目，目标环境为 Linux x86-64。使用 Docker Compose 在目标主机的部署目录运行。

## 启动

进入 `docker-compose.yaml` 所在目录。若当前账号的 UID/GID 为 `1000:1000`，可以直接执行以下命令。

```sh
install -d -m 0750 data data/worlds
test -f .env || cp .env.example .env
docker compose up -d --build
docker compose logs -f terraria
```

## 配置

在同目录的 `.env` 中设置部署参数。省略这些变量时，Compose 使用下表默认值。

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `HOST` | `127.0.0.1` | 宿主机端口绑定地址 |
| `PORT` | `7777` | 宿主机 TCP 端口，容器内使用 `7777` |
| `PUID` | `1000` | 容器运行 UID |
| `PGID` | `1000` | 容器运行 GID |
| `WORLD_FILE` | 空 | 已有 `.wld` 文件名。留空时使用 `default.wld` |
| `WORLD_SIZE` | `medium` | 首次生成世界的大小，可选 `small`、`medium`、`large` |
| `WORLD_DIFFICULTY` | `classic` | 首次生成世界的难度，可选 `classic`、`expert`、`master`、`journey` |
| `SAVE_INTERVAL_MINUTES` | `10` | 定时保存并备份的间隔，单位分钟，正整数 |
| `BACKUP_KEEP` | `5` | 每个世界保留的定时快照数量，正整数 |

`./data` 是宿主机上的数据目录。修改 UID/GID 或迁移已有数据前，确认新的运行账号能够读写目录和其中的文件。修改 `.env` 后，运行 `docker compose config --quiet` 检查配置，再执行 `docker compose up -d` 应用。

默认只有 Docker 宿主机可以访问发布端口。要让局域网 PC 客户端连接，将 `HOST` 设为宿主机的局域网 IP。`0.0.0.0` 会绑定全部 IPv4 接口。客户端从「多人游戏 → 通过 IP 加入」输入宿主机地址和 `PORT`。游戏关闭 UPnP 自动端口转发。

## 存档

`.env` 中的 `WORLD_FILE` 留空时，使用 `data/worlds/default.wld`。首次启动若没有该存档，会按配置生成世界。世界大小和难度只在生成时生效。

要加载已有世界，先将 `.wld` 文件放入 `data/worlds/`，然后在 `.env` 填写文件名。

```dotenv
WORLD_FILE='我的世界.wld'
```

执行 `docker compose up -d` 应用新的存档配置。指定的存档缺失时，服务会报错并退出。

## 定时保存和备份

服务器就绪后每隔 `SAVE_INTERVAL_MINUTES` 分钟查询在线玩家。有人在线时发送 `save`；空服时比较当前存档与上次快照，只有本次运行期间产生了尚未备份的新存档才补一份快照。原版空服会休眠，存档没有变化时跳过保存和备份，保留已有历史。

连续因空服且存档未变化而跳过时，只记录第一次跳过及例行查询回显。开始一次保存后，下一次跳过会重新记录。其他服务端消息及错误照常输出。

发送 `save` 后，等待原版服务端写入、校验主存档并报告进入备份阶段，再复制当前 `.wld`。查询或保存的完成标记超过 2 分钟未出现时，会跳过当次快照并记录日志。

定时快照直接存放在 `data/backups/<世界文件名>.d/YYYY-MM-DD_HH-mm.wld`，例如 `data/backups/我的世界.wld.d/2026-11-15_00-30.wld`。容器只读挂载宿主机的 `/etc/localtime`，按系统时区生成精确到分钟的文件名。同一分钟有多份备份时，使用 `2026-11-15_00-30.1.wld`、`2026-11-15_00-30.2.wld` 等名称。

新快照完整写入后，清理该世界超出 `BACKUP_KEEP` 的旧快照。默认每 10 分钟一次，保留最近 5 份。不同世界独立轮换，升级前备份单独保留。该世界下次生成定时快照时，会兼容迁移旧 `periodic/<世界文件名>/` 和嵌套的时间戳目录。旧 UTC 时间戳转换为系统时区。手动备份目录 `data/worlds.bak/` 独立保留。

恢复前先停止服务，并保留当前世界文件。将选中的历史 `.wld` 复制到 `data/worlds/` 下的新文件名，再通过 `.env` 的 `WORLD_FILE` 指定该文件并启动。快照包含世界地图和箱子等世界数据，角色背包与装备由客户端保存。

## 更新和停止

每次容器启动时检查 Terraria 官网公布的 PC 专用服务端版本。运行 `docker compose restart terraria` 可以立即触发检查。同一版本直接复用。试启动新版前，先将当前世界及其现有回滚文件完整备份到 `data/backups/`，等新版报告 `Server started` 后再将它设为当前版本。

检查、安装或试启动失败时，使用上次成功启用的版本。新版在 10 分钟内未就绪视为试启动失败。试启动失败或在就绪前中断时，先恢复升级前的世界文件，再继续启动。首次启动需要能访问 Terraria 官网。

`docker compose stop terraria` 会向游戏控制台发送 `exit`，让游戏保存世界后退出。世界和游戏生成的回滚文件在 `data/worlds/`，下载并解压的服务端版本在 `data/server/`。

手动备份时先停止服务，再复制 `data/worlds/` 到备份位置。恢复时可以把备份的 `.wld` 复制到 `data/worlds/` 下的新文件名，在 `.env` 中设置 `WORLD_FILE` 后重新启动。

## 回归测试

在此目录运行 `python3 -m unittest -v`，验证更新回退、定时保存、快照轮换和存档路径约束。测试使用临时目录和替代进程。
