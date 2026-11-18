# 泰拉瑞亚 PC 原版服务器

使用 Docker Compose 运行 Terraria PC 原版服务端，目标平台为 Linux x86-64。预构建镜像发布在 `ghcr.io/gene9831/terraria`，可直接拉取，无需登录 GHCR。

## 使用

### 快速启动

在目标主机创建应用目录，只需复制本目录中的 `docker-compose.yaml` 和 `.env.example`。已有部署保留现有的 `.env` 和 `data/`。

进入该目录，创建配置文件和宿主机数据目录。以下命令适用于宿主机账号 UID/GID 为 `1000:1000` 的情况；其他身份请按下方配置说明调整数据权限。`data/` 内的世界、备份和服务端缓存目录由容器按需创建。

```sh
mkdir -p data
test -f .env || cp .env.example .env
```

启动前按[配置](#配置)编辑 `.env`，设置访问地址、端口及需要加载的世界，再拉取镜像并启动：

```sh
docker compose config --quiet
docker compose pull
docker compose up -d
docker compose logs -f terraria
```

### 配置

在同目录的 `.env` 中设置部署参数。省略这些变量时，Compose 使用下表默认值。

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `IMAGE` | `ghcr.io/gene9831/terraria:latest` | 镜像地址和标签，可改为固定构建或其他镜像源 |
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

### 连接游戏

默认只有 Docker 宿主机可以访问发布端口。要让局域网 PC 客户端连接，将 `HOST` 设为宿主机的局域网 IP。`0.0.0.0` 会绑定全部 IPv4 接口。客户端从「多人游戏 → 通过 IP 加入」输入宿主机地址和 `PORT`。游戏关闭 UPnP 自动端口转发。

### 存档与备份

世界文件和原版回滚文件保存在 `data/worlds/`，自动备份保存在 `data/backups/`，服务端下载缓存保存在 `data/server/`。

#### 加载已有世界

`.env` 中的 `WORLD_FILE` 留空时，使用 `data/worlds/default.wld`。首次启动若没有该存档，会按配置生成世界。世界大小和难度只在生成时生效。

要加载已有世界，先将 `.wld` 文件放入 `data/worlds/`，然后在 `.env` 填写文件名。

```dotenv
WORLD_FILE='我的世界.wld'
```

执行 `docker compose up -d` 应用新的存档配置。指定的存档缺失时，服务会报错并退出。

#### 自动备份

服务器就绪后每隔 `SAVE_INTERVAL_MINUTES` 分钟查询在线玩家。有人在线时发送 `save`；空服时比较当前存档与上次快照，只有本次运行期间产生了尚未备份的新存档才补一份快照。原版空服会休眠，存档没有变化时跳过保存和备份，保留已有历史。

连续因空服且存档未变化而跳过时，只记录第一次跳过及例行查询回显。开始一次保存后，下一次跳过会重新记录。其他服务端消息及错误照常输出。

自动备份会先请求保存，待主存档写入和校验完成后再复制 `.wld`。查询或保存的完成标记超过 2 分钟未出现时，会跳过当次快照并记录日志。

定时快照直接存放在 `data/backups/<世界文件名>.d/YYYY-MM-DD_HH-mm.wld`，例如 `data/backups/我的世界.wld.d/2026-11-15_00-30.wld`。容器只读挂载宿主机的 `/etc/localtime`，按系统时区生成精确到分钟的文件名。同一分钟有多份备份时，使用 `2026-11-15_00-30.1.wld`、`2026-11-15_00-30.2.wld` 等名称。

新快照完整写入后，清理该世界超出 `BACKUP_KEEP` 的旧快照。默认每 10 分钟一次，保留最近 5 份。不同世界独立轮换，升级前备份单独保留。该世界下次生成定时快照时，会兼容迁移旧 `periodic/<世界文件名>/` 和嵌套的时间戳目录。旧 UTC 时间戳转换为系统时区。手动备份目录 `data/worlds.bak/` 独立保留。

#### 手动备份与恢复

手动备份前先执行 `docker compose stop terraria`，再将整个 `data/worlds/` 复制到备份位置。

恢复前先停止服务，并保留当前世界文件。将选中的历史 `.wld` 复制到 `data/worlds/` 下的新文件名，再通过 `.env` 的 `WORLD_FILE` 指定该文件并启动。快照包含世界地图和箱子等世界数据，角色背包与装备由客户端保存。

### 更新与停止

#### 更新容器镜像

`latest` 对应 `main` 最近一次成功发布的镜像，也可在 `.env` 中使用 `IMAGE=ghcr.io/gene9831/terraria:sha-<完整提交哈希>` 固定构建版本。

更新容器镜像时执行以下命令，Compose 会让旧容器正常保存退出后切换到新镜像，保留 `.env` 和 `data/`。需要固定构建版本时，先修改 `.env` 中的 `IMAGE`，再执行相同命令。

```sh
docker compose pull
docker compose up -d
```

#### 更新游戏服务端

Terraria 服务端本体在容器启动时下载并缓存到 `data/server/`。每次容器启动时检查 Terraria 官网公布的 PC 专用服务端版本。运行 `docker compose restart terraria` 可以立即触发检查。同一版本直接复用。试启动新版前，先将当前世界及其现有回滚文件完整备份到 `data/backups/`，等新版报告 `Server started` 后再将它设为当前版本。

镜像标签固定的是启动程序与运行环境，服务端版本仍由启动时的更新检查决定。

检查、安装或试启动失败时，使用上次成功启用的版本。新版在 10 分钟内未就绪视为试启动失败。试启动失败或在就绪前中断时，先恢复升级前的世界文件，再继续启动。首次启动需要能访问 Terraria 官网。

#### 正常停服

```sh
docker compose stop terraria
```

停服时会向游戏控制台发送 `exit`，让游戏保存世界后退出。

## 开发

开发需要完整仓库。以下命令均在 `games/terraria/` 目录运行。

### 回归测试

使用 Python 3.13 运行测试，验证更新回退、定时保存、快照轮换和存档路径约束。测试使用临时目录和替代进程。

```sh
python3 -m unittest -v
```

### 本地构建与运行

使用 Docker 构建 Linux amd64 镜像：

```sh
docker build --platform linux/amd64 --tag terraria:dev .
```

按[快速启动](#快速启动)准备 `.env` 和数据目录后，可用本地镜像运行；`--pull never` 使用本机构建的镜像：

```sh
IMAGE=terraria:dev docker compose up -d --pull never
```

### 自动构建与发布

[Terraria 工作流](../../.github/workflows/terraria.yaml)在本目录或工作流自身发生改动时运行：PR 验证 Compose 配置、构建镜像，并在镜像中运行回归测试；相关改动推送到 `main` 后，测试通过才向 GHCR 发布 `latest` 和 `sha-<完整提交哈希>` 标签。在 Actions 页面手动触发时，只有选择 `main` 分支才会发布。

发布使用工作流自带的 `GITHUB_TOKEN`，无需额外配置凭据。工作流只发布镜像，服务器更新由部署者执行。
