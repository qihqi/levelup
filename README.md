# 升级 · Levelup

Python + FastAPI + WebSocket 的四人双副牌升级游戏。1–4 位真人加入同一个房间，AI 补齐空位；南北、东西对家组队。中文界面支持电脑与手机浏览器。

## 启动

需要 Python 3.11 或更高版本。支持源码目录运行或安装 wheel；前端资源随包分发，无需 Node.js 或构建步骤。

```bash
cd /Users/han/Documents/code/levelup
python3 -m venv "$HOME/.venv" # 如果已有此环境，跳过这一行
"$HOME/.venv/bin/python" -m pip install -r requirements.txt
./run.sh
```

若使用 uv，可用 `uv venv "$HOME/.venv"` 创建环境，再用 `uv pip install --python "$HOME/.venv/bin/python" -r requirements.txt` 安装依赖。

浏览器打开 **http://localhost:8765**。端口可通过 `LEVELUP_PORT=9000 ./run.sh` 修改，也可以设置 `PYTHON_BIN` 指定 Python。

同一局域网的朋友使用 `http://服务器局域网IP:8765`。创建房间时从这个地址访问，复制出的邀请链接就能在其他设备使用；`localhost` / `127.0.0.1` 只指向各自设备。其他网络的玩家需要能访问这台服务器的地址，以及支持 WebSocket 的 HTTPS 反向代理。

## 构建并安装 wheel

从仓库根目录构建：

```bash
uv build --wheel
# 或：python3 -m pip install build && python3 -m build --wheel
```

产物为 `dist/levelup-0.1.0-py3-none-any.whl`，包含游戏引擎、AI、离线模拟器、Web 服务和全部前端资源。
在目标机器安装后可从任意目录运行：

```bash
python3 -m pip install /path/to/levelup-0.1.0-py3-none-any.whl
levelup-serve --host 0.0.0.0 --port 8768
# 同一启动入口也支持：python3 -m levelup --port 8768
```

安装包也提供 `levelup-simulate` 和 `levelup-benchmark`；等同于
`python3 -m levelup.simulation` 和 `python3 -m levelup.bench_ai`。
直接用 Uvicorn 时入口为 `levelup.server:app`，保持单 worker。
日志默认写入当前工作目录的 `logs/`，而非安装目录；WebSocket 日志仍支持 `LEVELUP_LOG_DIR`。

项目代码已移入 `levelup/`，旧的 `from game import Game` 等导入需改为 `from levelup import Game`，
或对应的 `levelup.*` 模块。HTML、CSS、JavaScript 保留在源码根目录的 `static/`，
构建 wheel 时作为数据文件安装到 `share/levelup/static/`，服务器自动定位资源。
更新后需重启服务才能加载新代码；重启会清空内存房间。

## 开始玩

1. 输入昵称创建房间。也可填写朋友的六位房间号加入。
2. 选择「记牌策略 AI」（默认）或「基础 AI」。房主复制邀请链接，朋友须在开局前入座；点击空位可换座，对面是队友。
3. 房主点击「开始游戏」，空位自动由 AI 补齐。一个人可以立即开局。
4. 开局进入摸牌：全桌每 0.5 秒按座位顺序摸一张，摸到级牌或王对即可选牌点击「亮主 / 反主」，无需等自己的回合。摸完后留 60 秒最后亮主，可点「不亮」；全员不亮提前定主，再由庄家扣底、轮流出牌。「提示」只选择建议牌，不自动提交；展开「提示依据与备选出牌」可以查看前三名动作和每条规则的加减分。
5. 可开启或取消托管。断线由 AI 接管，原浏览器标签页刷新后使用保存的随机令牌恢复座位。
6. 一局结束后房主开始下一局；房主离线时控制权自动交给在线玩家。坐庄打过 A 后可开始新比赛。

房主可在「开始游戏」上方选择出牌时限：15、30、60、120 秒或不限时；默认 60 秒。扣底为出牌时限的 1.5 倍并向上取整，默认 90 秒。也可在局间通过「操作时限」修改，设置对全桌生效并保留到下一局和新比赛。超时只代行本次操作，不会永久开启托管；不限时会关闭真人出牌与扣底的倒计时，但 AI、托管和断线接管仍会出牌。摸牌通常约 50 秒，亮主不会暂停或重置摸牌计时。所有真人离线时整桌暂停，六小时后回收；恢复连接后继续逐张摸牌。

手机开局后使用整屏布局，手牌与操作按钮始终留在屏幕内；横屏时牌桌、手牌左右排列。比分和主牌在顶部常驻，详细记分、AI 策略与牌桌动态可点「牌局信息」查看。我方使用青绿色实线，对手使用暖橙色虚线，并标注「你 / 队友 / 对手」；出牌区按当前玩家视角对应四个座位方向。

牌桌中央持续显示上一墩，四家出牌采用 2×2 布局并放大牌面；点击「上墩」标题可打开更大的 2×2 详情，查看长拖拉机或甩牌。

轮到自己出牌时，不能组成合法出牌的手牌会变暗且不可点击；选牌后继续按跟门、对子和拖拉机要求更新可选牌，选中的牌仍可取消。出牌按钮只在选牌完整合法时启用。领牌时可选同门组合，甩牌是否成功仍由出牌后的规则判定。若整手牌只有一种合法出法，默认等待 0.9 秒后自动出牌；两副牌中相同花色点数的副本算同一种选择。取消手牌栏的「唯一可出时自动出牌」即可手动操作，偏好保存在本浏览器。此选项只控制唯一出法的自动出牌，托管与房间超时规则仍单独生效。

邀请链接须保留完整地址和端口：不同服务器的房间不互通。房间不存在、已满或无法恢复时，会返回大厅并持续显示原因，可直接创建新房间。连接期间可取消，手机牌桌也始终提供「返回大厅」；离开不会删除原座位的恢复令牌。

## 本桌规则

规则参考：[维基百科「升级」](https://zh.wikipedia.org/zh-hans/升级_(扑克牌))。升级有多个地方版本，本实现采用以下明确桌规，页面「玩法说明」中也可查看。

- 两副含大小王的牌，共 108 张；每人 25 张，8 张底牌；双方从 2 打起。
- **边摸边亮**：从庄家位置开始，全桌每 0.5 秒给下一位发一张，直到每人 25 张；真人与 AI 随时可用已摸到的牌亮主或反主。最后一张发出后留 60 秒；庄家可点「不改主」，其他玩家可点「不亮」，四人均确认则提前定主。底牌在定主前不会发给庄家；庄家尚未确认时，需等到倒计时结束。新亮主/反主会重置 60 秒及全员确认，AI 也会自动选择亮主或不亮。单级牌 < 同花色对级牌 < 对小王 < 对大王；王对亮无主。可用同花色对子加固自己，不能反自己。同强度不能反主，以服务器先收到的有效声明为准。
- 首局最后亮主者坐庄；首局无人亮主重发，后续局无人亮主用底牌第一张非王牌花色定主。
- 主牌包含主花色、所有级牌和大小王。无主时仅级牌与王为主。副级牌等大；普通牌的级牌空位压缩后相邻，可组成拖拉机。
- 单张、对子、任意长度拖拉机、同门甩牌。对子须花色和点数完全相同。领出顺序为南→东→北→西，牌桌按本地玩家视角旋转。
- 跟牌必须跟足花色与张数。按领出组合从长到短，优先最长可跟连对，再匹配剩余连对/对子；不足时补单张。搜索同样长度的不同分配，避免贪心拆牌破坏后续应跟组合。
- 缺门时可垫牌。完全缺门时，使用完全匹配组合的主牌才能毙牌；同大先出者胜。
- 甩牌检查其余三家（包括队友）。失败时自动出张数最少、再按等级最小的失败组合，不罚分。成功甩牌被超毙时比较最大组合。
- 5 计 5 分，10、K 各 10 分，两副共 200 分。闲家赢末墩时抠底，按末墩领出牌型：单张 ×2、对子 ×4、两连对 ×6，此后每多一对加 2。全单张甩牌 ×3，其他甩牌按最长组合计算。
- 闲家 0 分庄家升 3 级；5–35 分升 2 级；40–75 分升 1 级；80–115 分闲家上庄不升级；120 分起每多 40 分闲家升 1 级。
- 守庄成功由庄家对家坐庄，失庄由庄家下家坐庄。各队级别最多升到 A，**必须在 A 级坐庄并守庄成功才能获胜**。
- 不含炒底、钩到底、甩牌罚分及其他必打级别规则。

AI 现在有统一的 `AIStrategy.rank(context, candidates)` 接口，按合法候选动作打分排序。默认「记牌策略 AI」记录公开出牌、推断确定缺门、优先兑现已知大牌，兼顾牌型、队友、抓分门槛与末墩。「基础 AI」保留原来的打法作为对照。房主可在大厅或局间切换策略；补位、托管、超时接管与提示统一使用本桌策略。

策略只接收自己的手牌、公开牌张/张数与庄家本人知道的底牌，不接触其他人的手牌。它仍是启发式评分，不是搜索或训练模型。来源、规则、权重和扩展方法见 [AI 策略说明](docs/AI_STRATEGIES.md)。

## 结构与运行边界

```text
levelup/
  __init__.py        公共接口：Game、Card、Rules、RuleError
  __main__.py        python -m levelup 启动入口
  cli.py             Web 服务命令行入口
  game.py            纯 Python 状态机、牌型、跟牌与计分
  legal.py           完整合法出牌约束与唯一出法检测
  ai/                策略接口、候选动作、记牌、基础/规则 AI
  server.py          FastAPI、WebSocket、房间与计时器
  simulation.py      离线模拟 API、CLI、JSONL 结果与命令日志
  bench_ai.py        固定种子、交换队伍的策略对比
  ws_logging.py      WebSocket 调试日志
  assets.py          定位源码或 wheel 安装的前端资源
static/              HTML、CSS、JavaScript；作为数据文件随 wheel 安装
tests/               Python 与客户端回归测试
docs/                AI 策略说明与日志审查记录
pyproject.toml       包元数据、依赖、命令入口与 wheel 资源配置
run.sh               源码目录启动脚本
```

服务器为每个玩家生成独立视图，只传自己已经摸到的手牌、公开牌张及其他人的张数；底牌只在结算时公开。每个房间使用异步锁串行处理操作，客户端提交状态版本以拒绝过期操作；摸牌期间亮主和提示额外带 `deal_id`，同次摸牌允许较旧版本，仍核验当前手牌和反主强度。令牌通过首条 WebSocket 消息传递，不放在邀请链接内。客户端只把令牌存入当前标签页的 sessionStorage。

这是可直接运行的**单进程、内存房间版本**。服务重启会清空所有房间，不包含账户、数据库存档、观战或中途新玩家入座。运行一个 Uvicorn worker；多 worker 不共享房间。线上扩容需先引入共享房间状态与消息分发。默认最多 200 个房间，离线房间六小时回收。

`./run.sh`（等同于 `python -m levelup`）监听所有网卡，WebSocket 单条消息限制为 16 KB。HTTP 与 WebSocket 检查 Origin；公开部署使用 HTTPS 和支持 WebSocket 升级的反向代理，保留原始 Host。需要只允许本机访问时运行 `LEVELUP_HOST=127.0.0.1 ./run.sh`。

## 离线模拟（不启动 Web 服务）

规则引擎、AI 和模拟器只依赖 Python 标准库，不导入 FastAPI、Uvicorn 或 `levelup.server`。
使用 Python 3.11+，在项目目录执行：

```bash
cd /Users/han/Documents/code/levelup
python3 -m levelup.simulation --games 10 --seed 42 \
  --strategies rule_based basic rule_based basic \
  --output logs/ai-results.jsonl
```

四个策略依次对应南、东、北、西（座位 0、1、2、3）；0/2 和 1/3 各为一队。
上例让记牌 AI 对基础 AI，种子依次为 42–51，每个种子从新比赛的第一局开始，打一局就结束。
省略 `--strategies` 则四个座位都用 `rule_based`。`--match` 改为每个种子打一场完整比赛，直到一队打过 A；
`--max-rounds 200` 是每场比赛的安全上限，到达上限会报错，保留已完成局的结果，不虚报胜负。

模拟没有半秒摸牌、AI 思考延迟或真人倒计时；仍逐张摸牌，每张之后让 AI 只根据已摸到的牌立即亮主、反主。
因此与 Web 版本的反应时间调度有区别，出牌和计分使用完全相同的规则引擎。

从 Python 创建实例并运行：

```python
from levelup.simulation import Simulation

with Simulation(seed=42, strategies=("rule_based", "basic", "rule_based", "basic"),
                log_path="logs/offline.jsonl", trace=True) as sim:
    result = sim.run_round()
    print(result["result"])  # team、score、gain、bottom_points、multiplier 等
    next_result = sim.run_round()  # 应用上局升级结果、轮换庄家，继续下一局
    # sim.run_match()             # 也可以一直打到比赛结束
```

`step()` 从大厅开局、完成摸牌阶段，或执行一手 AI 扣底/出牌，到局末停止。
手动控制每一步或接入自己的模型：

```python
from levelup.simulation import Simulation

with Simulation(seed=42) as sim:
    sim.command(None, "start")
    sim.command(None, "draw")              # 摸一张；摸牌期间任何座位都可亮主
    context = sim.observation(0)            # 不可变的玩家视角，包含公开历史
    sim.step()                             # 让四个 AI 摸完并亮主
    seat = sim.game.turn                   # 庄家扣底
    ranked = sim.game.ai_rankings(seat, "basic")
    action = ranked[0].action
    sim.command(seat, action.kind, [c.id for c in action.cards])
    sim.run_round()                        # 剩余部分交给 AI
```

玩家命令为 `command(seat, "bid"/"bury"/"play", [牌 ID])`；控制命令为
`command(None, "start"/"draw"/"finish_dealing"/"next")`，控制命令不带牌。
摸完后还可用 `command(seat, "pass")` 确认不亮；离线模拟不等待真人倒计时。
非法动作抛出 `RuleError`。自定义策略实现 `AIStrategy` 并用 `levelup.ai.register()` 注册后，可将其 ID 传入模拟器。
模拟主持方持有完整 `sim.game`；给 AI/训练特征编码器传入 `sim.observation(seat)`，避免泄露别家的手牌。

也可完全跳过模拟器，直接使用纯规则对象：

```python
from levelup import Game

game = Game(seed=42)
game.start()
game.draw_card()
# game.act(seat, "bid", ids)   # 亮主必须使用该座位已经摸到的牌
# 摸完并处理亮主后调用 game.finish_dealing()，随后用 game.act 扣底、出牌。
```

结果文件为追加写入、每条刷新到文件的 JSONL：`run_start` 记录种子和策略，`round_result` 记录每局结果，
`match_result` 记录完整比赛冠军，`run_end` 记录最终阶段。`team` 为赢得本局的一队（0=南北、1=东西），
`score` 为包含抠底后的闲家分数，`gain` 为实际升级数；80 分换庄时可能赢局但升 0 级。
每次模拟有独立 `run_id`；多次运行同一文件不会覆盖此前结果。CLI 还向终端打印汇总 JSON。

加 `--trace`（API 用 `trace=True`）会记录每条命令、执行前后状态、拒绝原因和操作玩家的私有手牌。
按 `run_id` 取出 `run_start.seed`，创建 `Game(seed)`，依次应用 `accepted=true` 的命令即可重放；
重新跑 AI 时，相同种子、策略和代码版本也得到相同结果（自定义策略若另用随机数，需自行固定其种子）。
日志不是完整训练数据集：尚不包含每次决策的全部候选动作或模拟价值标签。
日志可能包含手牌，且不会自动轮转；大量模拟默认只记录结果，按需开启 trace 并管理文件大小。

## 验证

```bash
cd /Users/han/Documents/code/levelup
uv pip install --python "$HOME/.venv/bin/python" -e '.[test]'
"$HOME/.venv/bin/python" -m pytest -q
node --check static/app.js # 可选：JavaScript 语法检查
node --test tests/client_connection.test.cjs # 连接失败、超时、取消与返回大厅回归
```

测试包括：拖拉机压缩级别、同级副主、强制跟对、部分拖拉机、甩牌失败、匹配组合毙牌、抠底与升级阈值、A 必过庄；30 个固定种子的完整 AI 比赛；四个真人 WebSocket 客户端完整打一局并开始下一局；房主转移、座位调整、断线恢复、超时接管、手牌隐私与非法消息；A/K 领出、两副牌计数、缺门风险、送分、末墩、策略选择与评分解释。

运行可复现策略对比：`"$HOME/.venv/bin/python" -m levelup.bench_ai --seeds 30 --output docs/ai-benchmark.json`。

检查服务状态：`GET /health`。

## 调试日志

WebSocket 通信自动写入启动工作目录下的 `logs/websocket-<进程号>.jsonl`，每行一个 JSON 记录，立即写入文件。
每个文件到 20 MiB 轮转，保留 5 个备份（`.1` 至 `.5`）；不同进程使用不同文件，重启后旧日志仍保留。
可用 `LEVELUP_LOG_DIR=/path/to/logs ./run.sh` 指定目录。`logs/` 已加入 Git 忽略规则，也不会通过网页提供访问。
旧进程的日志需按需清理。
自动化测试的 WebSocket 日志写入 pytest 临时目录，避免故意构造的错误请求混入真实牌局日志。

记录包含 UTC 时间、房间号、连接 ID、请求 ID、收到的 JSON 和发送的 JSON。
同一操作产生的错误、提示和发给各玩家的状态广播共享 `request_id`；摸牌、AI、计时器等主动推送的
`request_id` 为 `null`。每个请求还保存操作前该玩家的完整视图，包括手牌、当前墩、级牌、主牌、轮次和版本，
方便复查拒绝跟牌等问题。日志中的座位从 0 开始。恢复令牌等认证字段会递归脱敏；非法 JSON 只记录长度，
不记录原文。日志含各玩家私有手牌，作为服务器端调试文件保存。

`response` 表示服务器尝试发送；传输失败另记 `send_failed`，不代表浏览器已经处理该消息。
断开与主动关闭连接也会记录。按房间查找（包括轮转文件）：

```bash
rg '"room":"7VFNRE"' logs/websocket-*.jsonl*
```

日志从加载此版本的服务启动后开始，无法补回之前未记录的操作。现有服务需要重启才能启用；重启会清空内存房间。

## WebSocket 协议

`GET /api/ai-strategies` 获取注册策略及默认策略。

`POST /api/rooms` 请求 `{"name":"昵称","ai_strategy":"rule_based"}`（策略可省略），返回 `{"room":"房间号","token":"恢复令牌"}`。

连接 `/ws/{room}`，首条消息发送 `{"name":"昵称","token":"恢复令牌或空字符串"}`。

服务器发送 `welcome`（座位与令牌）和 `state`（当前玩家的完整视图）。后续操作带上最近一次 `state.version`：

```json
{"action":"bid", "ids":[1,55], "version":7, "deal_id":3}
{"action":"hint", "version":8, "deal_id":3}
{"action":"bury", "ids":[1,2,3,4,5,6,7,8], "version":12}
{"action":"play", "ids":[20,74], "version":13}
{"action":"hint", "version":13}
{"action":"auto", "version":13}
{"action":"seat", "seat":2, "version":2}
{"action":"start", "version":3}
{"action":"next", "version":99}
{"action":"restart", "version":999}
{"action":"ai_strategy", "strategy":"basic", "version":1000}
{"action":"timer", "seconds":null, "version":1001}
```

示例 ID 仅示意，必须使用当前手牌 ID。操作失败返回 `{"type":"error","message":"原因"}`；提示返回 `hint`（建议 action、ids、版本、strategy、score、reasons、前三名 alternatives）。`state` 包含 `ai_strategy` 和 `ai_strategies`。策略变更仅限房主在大厅或局间操作，经验证后向全桌广播。`{"action":"ping"}` 不需要版本，返回 `pong`。房间号可分享，恢复令牌应保留给本人。

摸牌阶段为 `phase: "dealing"`；`dealt` / `deal_total` 为全桌进度，`turn` 为下一张牌的接收座位，`last_draw_seat` 为刚摸牌的座位。`deal_closing` 和 `deal_seconds_left` 表示最后亮主窗口，`draw_interval` 表示发牌间隔。`deal_id` 在新局和无人亮主重发时更新。亮主不受 `turn` 限制，不操作即不亮主；摸牌中提示的 `pass` 表示等待；摸完后可发送 `pass` 确认不亮。请求须带当前 `deal_id`、`version` 和 `bid_revision`（亮主次数），同一亮主状态允许并发确认；新亮主清空 `bid_passed`，重置 60 秒窗口。`state.bid_passed` 为已确认的座位数组。

`timer` 仅允许房主在大厅或局间设置；`seconds` 为 15、30、60、120 或 `null`（不限时）。`state` 提供 `turn_seconds`、`bury_seconds`、`timer_options`；不限时的 `seconds_left` 为 `null`。摸牌间隔与最后亮主窗口不受该设置影响。
