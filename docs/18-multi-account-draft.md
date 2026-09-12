# #18 多账户：两个微信账户同时连接——设计稿 (v1)

> 状态: **设计稿，未实现、未评审**。基线 = 上游 0.4.0（`main` 已被重置回 `4f26b5a`，
> 与 `ArtemisLin/obsidian-wechat-diary` 逐字节相同）。行号指 0.4.0 的 `main.js`。
> 来源: 谷雨 2026-09-10 需求「能否添加第二个微信账户连接」。
> 三条已拍板: ①**每个账户一个独立日记树**；②**提醒各推各的**；③**状态每账户独立**。

## 0. 一句话

把"一个 bot = 一个主人 = 一个日记文件夹"的单例结构，改成"N 个账户各带自己的凭据、管道、日记树和状态"，
互不干扰；现有单账户用户在升级后**无感**（绑定不断、文件夹不变、行为不变）。

## 1. 为什么这不是小改动（0.4.0 的单例清单）

| 层 | 现状（0.4.0 行号） | 问题 |
|---|---|---|
| 凭据 | `getBotToken`/`setBotToken`（6838/6844）、`getBindIdentity`/`setBindIdentity`（6863/6874），密钥 key 各一个（2340/2345） | 只能存一份 token 与一份身份 |
| 账户状态 | `data.ilink` 单个对象（`DEFAULT_DATA`） | `buf`/`contextTokens`/`pauseUntil` 会被第二个账户踩掉 |
| 管道 | `this._client` + `this._running`/`_failCount`/`_noticedDown` + `_loop()`（6983/7170） | 第二个账户一起轮询会共用游标与故障计数 |
| 判定"是不是我" | `_handleIncoming`（7248）里和 `data.ilink.userId` 比 | 两个主人时第二个人的消息会被当陌生人丢掉 |
| 管道启动 | `onload` 的 `if (this.getBotToken()) this.startPipeline()`（6820）、`onLoginConfirmed`（6895） | 只起一条 |
| 习惯状态 | `data.profile` / `data.session` 全局一份 | 提醒/收尾/称呼互相影响（本次要拆开） |
| 写入 | `DiaryWriter._root()`（4391）读 `settings.diaryFolder` | 两个账户写同一个文件夹，混在一起 |
| 提醒 | `_reminderTick`（7106）读全局 session + 单个 `il` | 只可能提醒一个人 |

## 2. 数据模型

```js
data.accounts = [
  {
    id,                    // 稳定标识(生成后不变): "a1"/"a2"…; 密钥 key 与状态索引都用它
    label,                 // 用户可读名(设置页显示, 默认 "账户 1"), 同时是新账户文件夹的默认名
    diary: { ... },        // 该账户的一整套日记/附件/共用笔记/提醒/剪藏设置(见下)
    botId, userId, baseUrl, buf, contextTokens, recentSeqs, pauseUntil,
    lastAliveTs, loginTime, botTokenFallback, skipBacklog,
    profile: { state, name, finalize_count, nudge_count },
    session: { mode, entered_date, chat_count_today, last_activity_ts,
               cost_reminder_shown_date, nudged_date,
               reminded_date, reminder_streak, reminder_idx, reminder_last_result },
  },
]
data.activeAccount = "<id>"   // 只影响设置页显示哪一个, 不影响行为
```

**密钥**（不进 vault，D5 的既有原则）：`wechat-diary-ilink-bot-token:<id>`、
`wechat-diary-bind-identity:<id>`。旧的无后缀 key **保留不动**（见 §3）。

**每个账户自带一整套日记与提醒设置**（2026-09-10 拍板：不只 folder——"日记格式什么的"也要各配各的；
随后又拍板**提醒、剪藏也下放**）：

```js
data.accounts[i].diary = {
  folder,                        // 该账户的日记根(各自独立的一棵树)
  pathFormat,                    // 路径格式(该账户)
  attachmentMode, attachmentFolder, attachmentSubFormat,   // 附件位置三件套
  sharedDailyNote, sectionHeading, templatePath,           // 共用每日笔记那一组
  reminderEnabled, reminderTime,                            // 提醒：谁提醒、几点提醒
  webClipEnabled, webClipOtherSites, webClipFolder, webClipSaveImages,
  webClipMaxImages, webClipMaxTotalImageMb, webClipMaxChars, // 剪藏：开关与目录(目录默认跟着日记根)
}
```

理由：这一组字段本来就只被 `DiaryWriter` / `WebClipper` / `_reminderTick` 消费，而这三者这次**本来就要
按账户跑**（writer 按账户实例化、clipper 按账户实例化、提醒按账户遍历）。既然已经按账户持有它们，
把设置挂在账户上几乎不额外花钱，却让"工作号按年/月分文件夹、18:00 提醒、剪藏开；生活号不分文件夹、
22:00 提醒、剪藏关"这种诉求成立。

**仍然全局的东西**（不是偷懒，是它们卡在模块级状态或跨账户共享语义上）：

| 保持全局 | 为什么 |
|---|---|
| `timezone` | `setTimezone()` 写的是模块级 `_dateFmt/_timeFmt/_weekdayFmt`，`todayStr`/`hhmmStr`/`weekdayForDate` 全走它。要按账户就得把时区当参数穿进**每一条**时间调用（含 `renderPath`、段头、frontmatter）——那是动时间底座，收益（一个人跨时区写同一个库）远小于风险。**本轮不动** |
| `dayStartHour` / `nudgeNightHour` | 同样是模块级（`_dayStartHour`/`_nudgeNightHour`），被 `logicalTodayStr`/`isNightNow`/`reminderDue`/`isLateNight` 直接读。按账户下放要改这些纯函数的签名，连带影响所有"逻辑日"判定与既有测试基线。**本轮不动** |
| `fileMd5s` / `webClips`（缓存） | 同一个文件/链接从任一账户发来都该复用已存的那份——与"谁的"无关，分开反而会重复存。注意区分：**剪藏的开关与目录下放了**（上表），这里说的是"已剪藏 URL → vault 路径"这份缓存 |



## 3. 升级迁移（最关键的一段：不能让老用户掉绑定）

`onload` 里，在现有 `Object.assign` 合并之后加一步：

```
若 data.accounts 为空 且 (旧 data.ilink.userId 或旧 token 存在):
    建账户 #1:
      id = "a1", label = "账户 1"
      folder = settings.diaryFolder            ← 老用户的文件夹原样, 零破坏
      除 folder 外的字段从旧 data.ilink 拷贝
      profile / session 从旧的全局对象整份拷进来
      旧无后缀密钥 **拷一份**到 :a1 (不删旧的)
```

- **拷贝而不是搬移**：万一用户回退到 0.4.0，旧 key 还在，绑定照样能用。代价只是密钥存储里多一份副本。
- `data.ilink` / `data.profile` / `data.session` **保留不删**（同理由：可回退），但代码不再读它们。
  等确认不需要回退之后再清理——那是一次单独的、明确的删除。
- 账户 id 用 `a1`/`a2`…而不是用户可改的 label：label 改名不该让密钥 key 跟着动。

### 3.1 兼容垫片（让第 1 步真的是"不改行为"）

第 1 步之后，"唯一真相"是 `data.accounts`，但**管道、writer、agent 都还只有一个**（多份在 2/3 步才来）。
如果第 1 步顺手把 26 处 `data.ilink` 引用全改掉，那就不是"纯加层"了，而是一次性大改。

做法：第 1 步在 `onload` 里把迁移做掉，然后把三个旧字段变成**指向账户 #1 的视图**：

```js
Object.defineProperty(this.data, "ilink",   { get: () => this.data.accounts[0], configurable: true });
Object.defineProperty(this.data, "profile", { get: () => this.data.accounts[0].profile, configurable: true });
Object.defineProperty(this.data, "session", { get: () => this.data.accounts[0].session, configurable: true });
```

读与写都照旧（`this.data.ilink.userId = x` 也落在账户 #1 上），所以**管道/agent/提醒一行都不用动**，
第 1 步天然行为中性。第 2/3 步按账户改完之后，把这三个垫片删掉，`data.json` 里也不再写旧字段。
（旧字段在磁盘上的残留、以及旧的无后缀密钥，按 §3 末尾的原则保留到确认不需要回退为止。）

**这一步的风险点**：垫片是 `defineProperty` 覆盖在合并后的 data 上——`persist()` 序列化
`this.data` 时会**跳过 getter**（`JSON.stringify` 只取自有可枚举属性），所以迁移后的 `data.json`
里不会再有 `ilink`/`profile`/`session` 三个键。这正好是我们要的，但要在用例里断言：
迁移后落盘的 data.json 里这三键消失、`accounts[0]` 里有等价内容。


## 4. 运行时结构

```
plugin
 ├─ accounts: Account[]                    ← 由 data.accounts 派生(每项带存取器)
 ├─ pipelines: { [id]: { client, running, failCount, noticedDown, pollSettledTs } }
 ├─ writers:   { [id]: DiaryWriter }       ← 每个账户一个, 读该账户的 diary 设置
 ├─ clippers:  { [id]: WebClipper }        ← 每个账户一个(剪藏开关与目录按账户, 见 §7-3)
 └─ agents:    { [id]: DiaryAgent }        ← 每个账户一个(profile/session 是账户私有的)
```

- `startPipeline(id)` / `stopPipeline(id)`：一条账户一条长轮询，各自 `buf`、各自 `pauseUntil`、
  各自的 `_pollSettledTs`。**并发上限**：账户多时 `_raw` 的 socket 池要按账户算（0.4.0 的
  `maxSockets = 2` 是给单条长轮询留的）。
- `_handleIncoming(msg, account)`：陌生人判定的基准从 `data.ilink.userId` 换成
  **该条管道对应账户的 `userId`**。这一处改错会让第二个账户完全收不到消息，测试要盯死。
- `_reminderTick`：遍历账户，各自用自己 folder 数段、各自发自己那份提醒（②的拍板）。
- `onLoginConfirmed` / `QrLoginModal`：绑定目标从一个隐式单槽变成**显式指定的账户槽**
  （新增账户走"添加账户"→ 扫码 → 写进新槽；重绑旧账户写回原槽）。
- 设置页：**账户列表 + 选中哪个账户就编辑它那一套**。顶部是账户列表（label / 文件夹 / 路径格式预览 /
  绑定状态 / 删除 / 重新扫码 /「添加账户」），下面是选中账户的「日记」与「附件」两组设置。
  也就是现在那两组控件从"全局唯一"变成"跟着选中的账户走"。
  **这是一次设置页重构，不是加几行**：`_render()` 现在把「日记文件夹 / 路径格式 / 附件位置 /
  共用每日笔记」一次性铺开，改完要按 `activeAccount` 渲染，并且在切换账户时重画。
- 新账户的默认值：`folder` 默认取 label（如「账户 2」→ `日记-账户2`，避开与 #1 撞车）、
  `pathFormat` 与附件设置**继承上一个账户**——加一个账户就几步就填完，不要让用户从零配。

## 5. 兼容与边界

- **单账户用户零影响**：迁移后 `folder` = 原 `diaryFolder`，路径、附件、剪藏、提醒行为全不变。
  回归基线：0.3.1 黄金文件回归（`tests/bindtest.js`【G】）必须仍然全过。
- **共用每日笔记模式**（`sharedDailyNote`）**按账户各配**：每个账户的 `folder` 就是它自己那份
  "每日笔记所在目录"，各写各的 `## 微信随手记` 一节（节标题也可各配）。
  注意 §7-1 已拍板**两个账户不许指向同一个日记树**，所以"两个号写进同一份每日笔记"这个用法
  是被拦掉的——如果以后确实要，那要单独设计"多账户写入同一文件"的归属标记（段头标明谁说的），
  不属于本轮。
- **删除账户**：只删该账户的数据/密钥/管道，不碰 vault 里已写的文件（与「撤回只删引用、不删附件」同一条纪律）。
- **协议侧**：两个微信账户各自扫码绑定各自的 bot，各自一份 token —— 协议上没有共享状态需要处理。

## 6. 测试计划（bindtest）

1. **迁移**：老 data.json（有 `ilink.userId` + 无后缀 key）→ 建出账户 #1，folder = 原 diaryFolder，
   profile/session 整份搬进来，旧 key 仍在，绑定状态 = bound。
2. **零影响回归**：【G】黄金文件回归 + 【29】等既有写入用例全过（独立模式字节不变）。
3. **双账户隔离**：A 写进 A 的文件夹、B 写进 B 的；A 的 folder 改动不影响 B。
4. **凭据隔离**：A/B 各自 token 存在各自 key；A 解绑不影响 B。
5. **陌生人判定**：A 的管道收到 B 的 userId → 丢弃；收到 A 的 → 处理（`_handleIncoming(msg, account)`）。
6. **提醒各推各的**：A 今天记了、B 没记 → 只提醒 B；两个都没记 → 各发各的（文案轮换指针也各一份）。
7. **状态隔离**：A 说「晚安」不影响 B 当天的提醒；称呼各一份。
8. **管道生命周期**：停 A 不影响 B 继续跑；A 遇 -14 冷却不影响 B。
9. **并发上限**：两个账户同时长轮询时 `_raw` 的连接预算不互相饿死（用例可断言 socket 池参数）。
10. **每账户日记设置隔离**：A 改 `pathFormat` 不影响 B；A 用共用每日笔记模式、B 用独立文件模式可以并存；
    A 的 `attachmentMode`/自定义附件文件夹独立；改 A 的 folder 不影响 B 已写的历史文件。
11. **迁移字段完整性**：账户 #1 的 `diary` 块逐字段等于迁移那一刻的全局值（folder / pathFormat /
    附件三件 / 共用笔记那一组 / 提醒两项 / 剪藏七项），老用户"渲染出的路径与升级前逐字节相同"
    ——这条由【G】黄金回归兜底。另断言：迁移后落盘的 `data.json` 里 `ilink`/`profile`/`session`
    三个旧键消失（垫片是 getter，`JSON.stringify` 会跳过），`accounts[0]` 里有等价内容（§3.1）。
12. **同树拦截（两层）**：① 两个账户 folder 归一化后相同 → 拒；② folder 不同但渲染出的当天路径
    相同（`日记`+`YYYY` vs `日记/2026`+`YYYY-MM-DD`）→ 拒；报错里要出现另一个账户的 label。
13. **提醒/剪藏按账户**：A 关提醒、B 开 → 只提醒 B；A 的时间 18:00 生效而 B 的 22:00 不变；
    A 剪藏开、B 关 → 只有 A 的链接会被抓；A 的剪藏目录跟着 A 的日记根。

## 7. 待定（实现前需要拍板）

1. ✅ **两个账户不许指向同一个日记树——直接拦掉**（2026-09-10 拍板）。校验分两层，两层都要做：
   - **① folder 唯一**：归一化后（去首尾 `/` 与空白）不许与其它账户相同。
   - **② 渲染出的路径唯一**：只比 folder 不够——`folder=日记` + `pathFormat=YYYY` 与
     `folder=日记/2026` + `pathFormat=YYYY-MM-DD` 会落到同一个文件。所以再用各账户自己的
     `renderPath(pathFormat, 今天, moment)` 渲染当天路径比一次，撞了也拒。这一层直接复用既有的
     `validatePathFormat` / `renderedPathError`，不新写渲染逻辑。
   - 报错要说清**跟哪个账户撞的**（"与「工作号」的日记会写进同一个文件"），否则用户不知道改哪个。
2. ✅ **`reminderEnabled` / `reminderTime` 下放**（2026-09-10 拍板）。`_reminderTick` 按账户遍历、
   各读各自的时间与开关——"工作号 18:00、生活号 22:00"由此成立。
3. ✅ **`webClip*` 开关与剪藏目录下放**（同日拍板）。代价是 `WebClipper` 要按账户实例化
   （与 writer 同一批改），剪藏目录默认也跟着该账户的日记根走。
4. ✅ **时区 / 一天从几点开始 / 夜间提示起点：保持全局**（§2 表里已写明它们卡在模块级状态上）。
   确实要每账户不同的话，那是单独一轮"把时间底座改成可传参"的改动，不混在这次重构里。
5. **账户数上限**：建议软上限 5 个（每条是一份并发长轮询，且设置页要能看）。
6. **跨账户的"今天记了几段"**：`在吗` 只报自己账户的（③的必然结果），确认即可。
7. 设置页账户列表是否显示每个账户的"最近一次收到消息时间"（诊断用，0.4.0 已有 `lastAliveTs`）。

## 8. 实现顺序（建议）

1. **数据模型 + 迁移 + 兼容垫片 + 凭据分层**（纯加层，不改任何行为：writer/clipper 仍读全局、
   管道仍是单条，垫片让 26 处 `data.ilink` 引用一行都不用动）→ 用例 1/2/11 全过，可先合。
2. **每账户设置 + writer/clipper/agent 按账户**：`diary` 块（日记五项 / 附件三件 / 共用笔记三件 /
   提醒两项 / 剪藏七项）落地，`DiaryWriter` 与 `WebClipper` 改读账户的 diary（全局那份变成
   "新账户默认值"），`DiaryAgent` 按账户实例化 → 用例 3/7/10/12。
3. **管道按账户化**（`pipelines` 表 + `_handleIncoming(msg, account)` + 各自 `buf`/`pauseUntil`）
   → 用例 5/8/9。这一步风险最高（陌生人判定改错会让第二个账户完全收不到消息）。
4. **提醒按账户化**（各读各自的时间与开关）→ 用例 6/13。
5. **设置页重构**：账户列表 + 选中账户编辑它那一套 + 添加/删除/重绑 + 两层同树校验
   → 用例 4/12 + 手测扫码。

每步一个提交，**第 1 步合进去不改变任何行为**，2 之后随时可停（第 2 步做完就已经"两个账户各写各的树"，
只是还不能同时在线）。
