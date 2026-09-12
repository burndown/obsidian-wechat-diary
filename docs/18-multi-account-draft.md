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

**这一步的风险点**：垫片必须用 `enumerable: false`。`JSON.stringify` 只序列化**自有可枚举**属性，
但它对可枚举的 getter **是会调用并序列化的**——不加这个开关，`ilink`/`profile`/`session`
就会被原样写回 `data.json`，与"迁移后这三个键消失"的目标正好相反。用例断言落盘 JSON 的顶层没有这三个键。

### 3.2 落地第 1 步时抓到的两个坑（第 2/3 步要记住）

两个都是"账户层与既有代码怎么咬合"的问题，都不是设计阶段想到的，是测试抓出来的：

1. **`unbind()` 会把 `this.data` 整个换成 `DEFAULT_DATA()`**（0.4.0 原代码，为了清干净状态）。
   换完 `accounts` 空、垫片也没了 → 凭据写到空气里：**没有 `secretStorage` 的宿主上
   `keepToken` 会静默失效**（token 落在兜底字段的路径断了），后续 `session` 写入还会抛异常。
   修法：把"迁移 + 兜底账户 + 装垫片"抽成 `_installAccounts()`，`onload` 与 `unbind` 两处都必须走它。
   → 第 2/3 步再遇到"重置 data"的地方（如果有），同样要记得重装账户层。
2. **`onload` 的 data 字面量必须把 `stored.accounts` 带过来**。漏了它，`migrateAccounts` 会以为
   这是老数据而**重新迁移一次**：`profile`/`session` 当场丢光（用户的称呼、提醒记账全没），
   只有 token 会从密钥回填——症状是"看起来还能用，但设置和状态被悄悄重置"。
   用例【D18】的"二次启动幂等"就是盯这个的。

### 3.3 落地第 5 步抓到的坑（设置页与账户层咬合处）

1. **`deleteAccount` 删最后一个账户时，兜底的 diary 必须在 `splice` 之前读**。
   `installSettingsShim` 把那 17 个键定义成指向 `data.accounts[0].diary` 的访问器；一旦把唯一的账户
   从数组里摘掉，`this.settings.diaryFolder` 立刻变 `undefined`。于是
   `accountDiaryFromSettings(this.settings)` 写在 `splice` 之后，会把用户配好的文件夹/格式/提醒
   全丢成默认值——症状是"只是删了个账户，设置也跟着回出厂"。修法：`splice` 之前
   `Object.assign({}, acct.diary)` 兜住（正文 §8 的 `keptDiary`）。证据：把它挪回 `splice` 之后跑
   bindtest，`【D18-5】`「用户配好的文件夹没有因这次重置而丢」挂，738/739。
2. **把 `const st = plugin.settings` 换成账户 diary 时，全局字段会被顺手带走**。
   `dayStartHour` 原来是从 `st`（= `plugin.settings`）读写的，现在 `st` 是账户 diary——继续读会读到
   `undefined`（下拉显示回 4 点），继续写会把 `dayStartHour` 塞进账户 diary 且
   `setDayStartHour(undefined)` 变成 NaN，**「一天从几点开始」从此静默失效**。`saveVoiceAudio` /
   `timezone` / AI 三项同理（它们都不在 `ACCOUNT_DIARY_FIELDS` 里）。这一类**没有自动用例兜底**
   （设置页 DOM 不渲染），只能逐个拿 `ACCOUNT_DIARY_FIELDS` 名单核对。
3. **`_fmtCustom` 是 UI 状态，不能搭车进账户 diary**：`st._fmtCustom` 原本就是
   `plugin.settings._fmtCustom`；`st` 换成账户 diary 后若不动它，这个键会落进 `accounts[0].diary`，
   既改落盘形状，又让"自定义路径格式"这一档变成每账户一份。留在 `plugin.settings`。
4. **`_pathSuggest` 需要"拒绝"这条路**：它的 `onPick` 原来只被当"已经选定"。同树校验要"不落盘 +
   输入框回退"，所以改成 `onPick` 返回 `false` 时 `t.setValue(prev)` 且不更新 `saved`。不改的话页面
   显示的是被拒绝的值、`data.json` 里却是旧值——比直接报错更难查。
5. **第二层比的是"根 + 渲染出的相对路径"，而根是字面文件夹名**。最初以为
   `folder="日记/[x]"` 会和 `pathFormat="[x]/YYYY-MM-DD"` 撞——其实 folder 不做 moment 解析，
   前者是名字真的叫 `[x]` 的文件夹。实测用例「方括号字面量 folder 也能撞上」当场挂，才把期望改成
   "**渲染出的** `[x]`"对"**根文件夹真的叫** `x`"（`日记` + `[x]/YYYY-MM-DD` vs `日记/x` + `YYYY-MM-DD`）。



### 3.4 真机测试抓到的坑（0.5.0-beta.1 → beta.2）

**账户密钥名用了非法字符，真机第一次绑定就炸。**

- **现象**（谷雨 2026-09-12，beta.1 真机）：打开「扫码绑定微信」，弹窗里红字
  「**登录出错: 密钥 ID 无效。请仅使用小写字母、数字和破折号，最多 64 个字符。**」
- **原因**：`ACCOUNT_TOKEN_KEY = SECRET_BOT_TOKEN + ":" + id` → `wechat-diary-ilink-bot-token:a1`。
  **Obsidian 的 `SecretStorage` 只接受小写字母 / 数字 / 破折号、最长 64 字符**，冒号非法，
  `setSecret` 当场抛错。身份密钥同病。
- **为什么 739 条断言全过**：`tests/bindtest.js` 的 `secretStorage` 桩是个**来者不拒的普通对象**
  （`setSecret: (k,v) => { secrets[k]=v }`），宿主会拒绝的 key 它照收。**桩比宿主宽松，就是在骗自己。**
- **修法**（两件事都要做，缺一条这类 bug 还会回来）：
  1. 密钥后缀改用 `-`；顺手加 `accountKeySuffix()` 做兜底规整（手改过 `data.json` 的怪 id 也不该让
     密钥读写整个失败），并把长度截到 12 位（总长 44 < 64）。
  2. **桩按宿主的规则强制校验**：`SECRET_ID_RE = /^[a-z0-9-]{1,64}$/`，不合格就抛**同一句错误文案**。
     另加 5 条断言把这条规则显式写进测试（桩哪天被放松，断言仍盯着）。
- **变异验证**：把 `-` 改回 `:` 再跑 —— 桩立刻以
  「密钥 ID 无效。请仅使用小写字母、数字和破折号，最多 64 个字符。」崩掉（exit 2）；
  改回来 744 条全过。**同一类错误从此在测试阶段就会被拦下，不必等真机。**
- **教训（写给以后加设置项的人）**：凡是"我们自己拼一个标识符交给宿主"的地方（密钥名、vault 路径、
  文件名、命令 id），**桩必须复制宿主的校验规则**，否则测试的覆盖率是假的。
  本仓库同一类的既有先例：路径格式有 `validatePathFormat` / `renderedPathError` 自校验（那是对的）。
- **真机验证（2026-09-12，谷雨）**：
  - ✅ **绑定不再报错**（beta.2）。
  - ✅ **两个账户各写各的树（测试项 B4，本次重构最核心的一条）**：账户 1 发「甲-测试1」落在
    `黑高日记/2026/2026-09-12.md`，账户 2 发「乙-测试1」落在 `段日记/2026/2026-09-12.md` ——
    **两个不同文件夹、两个不同文件**。这同时证明了「每个账户一整套日记设置」里的 folder 是按账户生效的，
    也证明了按账户的消息路由（第 3 步最易错的那处）是对的。
- **仍未验**（`18-manual-test-plan.md` §4）：**D1/D3 同树拦截**、F1/F2 并发与补收、C 组隔离、
  A3/A4 落盘形状。注意其中几项**失败时不报错**（只会悄悄写进同一棵树、或丢消息），
  必须按清单主动验，不能靠"看着正常"。

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
   - ⚠️ **落地修正（第 5 步，2026-09-13）**：这里的举例写错了——`pathFormat=YYYY` 渲染成 `2026`，
     A 落 `日记/2026.md`、B 落 `日记/2026/2026-09-10.md`，两者**不撞**；而且 `YYYY` 本身过不了
     `validatePathFormat(requireDaily)`（两天会渲染成同一个文件）。真正撞的是 `folder=日记` +
     `pathFormat=YYYY/YYYY-MM-DD`（就是默认格式）对 `folder=日记/2026` + `pathFormat=YYYY-MM-DD`，
     两边都是 `日记/2026/2026-09-10.md`。实现与用例按这个来；没有加"folder 嵌套即冲突"的第三层
     （那会误伤 `生活号` 放 `日记/2026` 下这种其实不撞的配置）。
2. ✅ **`reminderEnabled` / `reminderTime` 下放**（2026-09-10 拍板）。`_reminderTick` 按账户遍历、
   各读各自的时间与开关——"工作号 18:00、生活号 22:00"由此成立。
3. ✅ **`webClip*` 开关与剪藏目录下放**（同日拍板）。代价是 `WebClipper` 要按账户实例化
   （与 writer 同一批改），剪藏目录默认也跟着该账户的日记根走。
4. ✅ **时区 / 一天从几点开始 / 夜间提示起点：保持全局**（§2 表里已写明它们卡在模块级状态上）。
   确实要每账户不同的话，那是单独一轮"把时间底座改成可传参"的改动，不混在这次重构里。
5. ✅ **账户数上限：软上限 5 个**（2026-09-13 第 5 步落地，`MAX_ACCOUNTS = 5`；到顶时 `Notice`
   说明原因，不静默失败）。每条是一份并发长轮询，且设置页要能看。
6. ✅ **跨账户的"今天记了几段"：只报自己账户的**（确认无误：第 3 步起 writer 与提醒都按账户走，
   「在吗」/`countDay` 各数各自的树；`【D18-3】` 的正反两条用例钉着）。
7. ⏸ 设置页账户列表是否显示每个账户的"最近一次收到消息时间"（诊断用，0.4.0 已有 `lastAliveTs`）
   —— 第 5 步拍板**本轮不显示**：账户列表先只显示绑定状态（已绑定 / 待认领 / 未绑定），
   `lastAliveTs` 诊断等真有人需要时再加（它还在每个账户的 `ilink` 里，加它不需要动数据模型）。

## 8. 实现顺序（建议）

1. **数据模型 + 迁移 + 兼容垫片 + 凭据分层**（纯加层，不改任何行为：writer/clipper 仍读全局、
   管道仍是单条，垫片让 26 处 `data.ilink` 引用一行都不用动）→ 用例 1/2/11 全过，可先合。
2. **每账户设置 + writer/clipper/agent 按账户**：`diary` 块（日记五项 / 附件三件 / 共用笔记三件 /
   提醒两项 / 剪藏七项）落地，`DiaryWriter` 与 `WebClipper` 改读账户的 diary（全局那份变成
   "新账户默认值"），`DiaryAgent` 按账户实例化 → 用例 3/7/10/12。

   **落地记录（2026-09-10，已实现）**：`DiaryWriter(plugin, ai, account)` / `WebClipper(plugin, account)` /
   `DiaryAgent(plugin, account)` 都接受账户，新增 `_st(key)`（账户有值用账户，`undefined` 回落全局）与
   `_settingsView()`（把账户值覆盖到 settings 浅拷贝上，喂给 `defaultWebClipFolder` / `webClipMaxImages` /
   `webClipMaxTotalImageBytes` / `shouldClipWebUrl` 这几个吃 "settings 形状" 的纯函数——最小改法：那些函数
   签名不能动，webcliptest 直接测它们）。插件侧新增 `_rebuildAccountServices()`，`onload` 与 `unbind`
   两处都走它；`writer`/`clipper`/`agent` 三个旧别名保留，仍指第一个账户（消息路由是第 3 步）。

   **与规格的偏差**：
   - 规格给的 `_st` 单独用会破坏单账户行为（见下面新坑），所以补了一个**反向垫片** `installSettingsShim`：
     把 `settings` 上那 17 个键定义成指向 `accounts[0].diary` 的**可枚举访问器**。设置页第 5 步才改，
     在那之前它写的仍是全局 `settings`；不接通两边，用户改设置会被账户里的旧快照盖住。可枚举是为了
     `JSON.stringify` 照旧把值写回 `data.json`（回退 0.4.0 时 `settings.diaryFolder` 还在）。第 5 步删。
   - `DiaryAgent` 也加了 `_st`/`_settingsView`（规格只要求 writer/clipper）：剪藏开关 `webClipEnabled`、
     站点范围 `webClipOtherSites`、欢迎语里的 `diaryFolder` 都在 agent 路由上读，不按账户读第 3 步就会
     拿错账户的开关。`this.plugin.clipper` 也换成 `this.clipper`（本账户的实例）。
   - `WebClipper` 构造函数是 `(plugin, account, deps)`，并从 `account` 上兼容读一次旧签名的
     `directImageRequest`（webcliptest 把注入点放在第 2 个参数；真实账户没有这个字段，不会混淆）。
   - 用例 D18-2.6 断言的是 `writers[id].webClipFolder()` 而不是 `clippers[id].webClipFolder()`——
     剪藏目录的落盘决策在 `DiaryWriter`，`WebClipper` 只抓正文，没有这个方法。

   **新坑（本步测试抓出来的，和 §3.2 第一条同类）**：`_st` 的"账户值优先"会**把迁移后的全局设置盖住**。
   迁移把全局值（含 `DEFAULT_SETTINGS` 里的默认值）原样拷成账户里**明确的**值，于是 `undefined` 回落
   永远轮不到全局；而设置页此刻写的还是 `plugin.settings`。证据：把 `installSettingsShim` 注释掉跑 bindtest，
   621/627——B15 的"改 `settings.sharedDailyNote` 后应按共用模式写"6 条全挂（写进了独立模式文件）。
   修法就是上面那条反向垫片：单账户下 `settings` 与 `accounts[0].diary` 是同一份存储，两边读写都即时同步。

3. **管道按账户化**（`pipelines` 表 + `_handleIncoming(msg, account)` + 各自 `buf`/`pauseUntil`）
   → 用例 5/8/9。这一步风险最高（陌生人判定改错会让第二个账户完全收不到消息）。

   **落地记录（2026-09-12，已实现，与第 4 步合并提交）**：`this.pipelines = { [id]: { client, running,
   failCount, noticedDown, pollSettledTs, sleepCancels: Set, skippedCount } }`（见 `newPipelineState()`）；
   方法签名落成 `startPipeline(id)` / `stopPipeline(id?)`（不传 = 全部停）/ `_loop(id)` /
   `_handleIncoming(msg, id)` / `_isPaused(id)` / `_clearSkipBacklog(id?)` / `_interruptibleSleep(ms, id)`。
   `_rebuildAccountServices()` 里加 `_reconcilePipelines()`：按账户对账 pipelines，**保留在跑的那条**
   （第 5 步"添加账户"不能打断账户 #1），删掉已不存在的账户。`onload` 改为遍历账户逐个 `startPipeline(a.id)`。

   **与规格的偏差**：
   - **46 处单例引用改完后，main.js 内部 `this._client/_running/...` 只剩 QrLoginModal 自己的
     `this._client`（那是扫码轮询的局部 client，与管道无关）**。但因为**既有 bindtest 断言直接读写
     `p._client/_running/_pollSettledTs/_skipBacklog/_skippedCount/_declinedClaims`（一条都不许改）**，
     补了 `installPipelineShim(plugin)`：把这些名字定义成**指向首个账户**的访问器。单账户下与旧行为
     逐字等价；多账户下它们不再代表第二条管道（内部代码一律走 `pipelines[id]`）。与第 1/2 步的
     `installAccountShim`/`installSettingsShim` 同一条纪律，第 5 步设置页改完后可删。
   - **`skipBacklog` 的真源改成 `account.ilink.skipBacklog`（每账户），不再是实例布尔**。恢复分支与
     `adoptOwner` 本来就写这个字段（且落盘），所以读完直接是它；`_skipBacklog` 只是首个账户的兼容视图。
     跳过计数（诊断用、不必跨重启）放进 `pipelines[id].skippedCount`。
   - **`_setStatus(text, id)`**：状态栏只有一个位置。单账户显示 `📖 微信日记: <text>`（与旧版逐字相同，
     回归基线依赖它）；多账户显示 `📖 微信日记: N 个账户 · <label> <text>`——总数 + 最近一次状态变化
     属于谁。不做轮播（会让人以为状态在跳），每账户详情留给第 5 步的设置页账户列表。
   - 绑定生命周期按账户：`onLoginConfirmed(payload, id)` / `adoptOwner(userId, id)` /
     `_askClaimOwner(from, id)` / `bindState(id)`（不传 = 首个账户）/ `QrLoginModal(app, plugin, accountId)`
     （重扫码写回原槽，"添加账户"传新槽；设置页此刻仍不传 = 首个账户）。`_declinedClaims` 变成每账户一个
     `Set`（`_declinedSet(id)`，`_declinedClaims` 是首个账户的兼容视图）。`onLoginConfirmed` 只停目标账户的
     管道（重扫账户 A 不打断 B）。

   **新坑（本步测试抓出来的，两个都与"还偷偷读全局垫片"有关）**：
   1. **`DiaryAgent.onMessage` 的兜底白名单也在比 `this.plugin.data.ilink.userId`（= 账户 #1）**。
      `_handleIncoming` 按账户放行之后，第二个账户的消息会在 agent 这一层被当陌生人丢掉 —— 症状正是
      "第二个账户完全收不到消息"。修法：拿 `this.account.ilink.userId`（account 为 null 时才回落垫片）。
      证据：把这一行退回全局垫片跑 D18-3，`a2 的消息写进 a2 的文件夹` 挂（写进了 `甲/…`），669/670。
   2. **`DiaryAgent` 里下载媒体（语音原声/图片/语音兜底/文件视频）读的是 `this.plugin._client`**，
      按账户化后那是账户 #1 的兼容视图 —— 第二个账户会拿**别人的 token/baseUrl** 去下载。新增
      `_pipeClient()`（`plugin.pipelines[account.id].client`，account 为 null 时回落 `plugin._client`），
      4 处全部改走它。
   3. 另有一处**有意的偏差**：`fileMd5s`（附件去重表）在 §2 表里被列为"全局缓存"，但数据模型第 1 步
      已把它放进 `account.ilink`。与其让第二个账户跨账户写账户 #1 的字段（它自己的表永远是空的），
      不如按账户走（新增 `_ilink()`）——代价是"同一份文件从两个号发来各存一份"。彻底全局化要新增顶层
      `data.fileMd5s`，属于数据模型变更，不在本轮。

4. **提醒按账户化**（各读各自的时间与开关）→ 用例 6/13。

   **落地记录（2026-09-12，已实现，与第 3 步合并提交）**：`_reminderTick()` 遍历账户，逐账户
   `try/catch` 调 `_reminderTickOne(id)`（一个账户抛错不连累其余）。每账户：自己的 `pipelines[id]`
   没在跑 / 没 settle / 被 `il.skipBacklog` 挡住 / 被自己的 `pauseUntil` 暂停 → 跳过；用
   `_st("reminderEnabled"/"reminderTime", id)`（账户设了用账户、`undefined` 回落全局，`=== undefined`
   判据保证账户的 `false` 压过全局的 `true`）；用 `this.writers[id].countDay()` **在它自己的日记树里数段**；
   用自己的 `client` 发给自己 `il.userId`、带自己的 `contextToken`；`reminded_date`/`reminder_streak`/
   `reminder_idx`/`reminder_last_result` 落自己的 `session`。新增 `_st(key, id)`（插件的账户设置解析，
   与 `DiaryWriter._st`/`DiaryAgent._st` 同义）。

   **与规格的偏差**：规格说的"账户有值用账户、`undefined` 回落全局"直接落成 `_st`；没有别的偏差。

   **用例**：新增 `【D18-3】`（bindtest，24 条断言，覆盖规格 §6 的 5/6/8/9 + 各写各的树 + 凭据隔离）：
   陌生人判定按账户（a2 的 userId 进 a1 管道 → 不写不回复；自己的 → 正常）、各写各的树（a2 的段落只出现在
   `乙/`，`甲/` 里没有）、独立启停（`stopPipeline("a1")` 后 a2 仍在跑且 500ms 宽限后 client 没被 destroy）、
   -14 冷却独立（a1 的 pauseUntil 变了、a2 的一动不动且提醒照发）、提醒各推各的（A 记了 B 没记 → 只提醒 B；
   都空 → 各发各的；A 关提醒 → 即使空也不提醒 A）、提醒数的是自己那棵树（反向也测）、凭据隔离
   （两条管道各自 token/baseUrl/userId，且不是同一个 client）。单账户零影响由既有 646 条兜底。
   实测 `npm run verify`：bindtest **670**（646 + 24），webcliptest **92**，既有断言零删改。

5. **设置页重构**：账户列表 + 选中账户编辑它那一套 + 添加/删除/重绑 + 两层同树校验
   → 用例 4/12 + 手测扫码。

   **落地记录（2026-09-13，已实现）**：

   - **设置页结构**：顶部「微信账户」区，一行一个账户——可改的 `label` 输入框、绑定状态文案
     （已绑定显示 `userId` 前 18 位 / 待认领 / 未绑定）、「扫码绑定 / 重新扫码」「解除绑定 /
     清除残留凭据」「删除」三个按钮；紧接着一行是该账户**自己的**「日记文件夹」（复用既有
     `_pathSuggest` 文件夹选择器）。点行标题（避开控件区域）把该账户设成 `data.activeAccount`
     并 `this.display()`。底部「添加账户」（软上限 5，到顶 `Notice` 说明原因，建成后立刻开该账户的
     `QrLoginModal`）。下面「日记」「附件」「写进已有的每日笔记」「提醒」「剪藏」几组外观不变，
     读写目标换成 `plugin.activeAccount().diary`。
   - **"编辑当前账户"的落点**：设置页新增 `_aid()/_acct()/_writer()/_d()/_view()` 五个帮手，
     `const st = this._d()` 取代原先的 `plugin.settings`；凡读 `plugin.writer` 的帮手
     （`_foreignCheck` / `_headingCollisionCheck` / 路径格式预览行 / 附件预览行 / `_suggestImportOnEnable` /
     `_importDailyNotes`）改走 `_writer()`；吃 "settings 形状" 的纯函数（`defaultWebClipFolder` /
     `webClipMaxImages` / `webClipMaxTotalImageBytes`）喂 `_view()`。全局字段（`timezone` /
     `dayStartHour` / `saveVoiceAudio` / AI 三项 / `_fmtCustom`）仍读 `plugin.settings`。
   - **两层同树校验**：新增纯函数 `validateAccountTree(diary, others, { momentLib, today })`
     → `{ ok, error, conflictId }`。第一层 folder 归一化（去首尾空白与 `/`；`"/"` 与 `""` 都算库根）
     后唯一；第二层各用各的 `pathFormat`，经**既有** `validatePathFormat` + `renderPath` 渲染
     "今天"、拼成 `根/渲染路径.md` 再比一次，撞了也拒；两层报错都点名对方 `label`。
     接在五处：①行内账户 folder 选择器 ②「日记文件夹」③路径格式预览行 ④路径格式预设下拉
     ⑤「从每日笔记设置导入」。做法是"不合法就不落盘"：folder 选择器 `onPick` 返回 `false` →
     `_pathSuggest` 回退输入框且不更新 `saved`；路径格式预览行变红写"(没有保存)"；
     离散动作（下拉/导入）弹 `Notice`。
   - **加载兜底**：`_warnTreeConflicts()` 在 `onload` 里逐对比较，撞了就一条 `Notice` 点名两个账户，
     **不拒绝启动、不改用户数据**。
   - **增删**：`createAccount()`（纯内存：`nextAccountId` / `nextAccountLabel` /
     `defaultAccountFolder`；diary 继承上一个账户、folder 在上一账户根后面缀 label 并逐个让开）/
     `addAccount()`（上限检查 + 落盘 + `_rebuildAccountServices()` 对账，不打断在跑的管道）/
     `deleteAccount(id)`（停该账户管道、清 `:id` 密钥与身份副本、摘数组、重建服务、落盘；
     **不碰 vault 里已写的文件**；删最后一个时立刻补一个空账户并保留用户的 diary）/
     `unbindAccount(id, keepToken)`（单账户解绑，保留 diary/label）。

   **与规格的偏差**：

   - **规格没写"解除绑定"按钮去哪**。旧页面的「解除绑定 / 清除残留凭据」是 v0.2.1 的关键修复
     （半绑定必须清得掉），不能消失；但 `plugin.unbind()` 会把整个 `data` 换成 `DEFAULT_DATA()`
     （所有账户一起没），两个账户时是陷阱。于是新增 `unbindAccount(id, keepToken)` 并把它下放到
     每个账户行：只动选中账户、保留 diary；单账户下与旧 `unbind` 等价（旧 `unbind` 保留 settings，
     这里保留 diary，重置的都是 `ilink`/`profile`/`session`）。
   - **旧顶部「微信」标题 + 独立「绑定状态」块被「微信账户」列表取代**，不再是两个地方都能扫码。
   - **§7-1 的举例写错了**（详见 §7-1 的落地修正）：真正撞的是 `日记` + `YYYY/YYYY-MM-DD`
     对 `日记/2026` + `YYYY-MM-DD`。第二层按"当天整条文件路径相等"实现，**没有**加"folder 嵌套
     即冲突"的第三层。
   - **每个账户占两行 Setting**（规格说"每行显示"，但一行里塞 2 个输入框 + 3 个按钮在 Obsidian 的
     `setting-item-control` 里会挤成一团）：第一行身份 + 状态 + 三个按钮，第二行该账户的日记文件夹。
   - **`_fmtCustom` 留在 `plugin.settings`**：它是 UI 状态不是日记设置，放进账户 diary 会改落盘形状，
     还会让"自定义档位"变成每账户一份。
   - 文件夹是**字面量**、`pathFormat` 才是 moment 格式：`[x]` 出现在 folder 里不做日期解析
     （详见 §3.3-5）。

   **落地第 5 步抓到的坑**：见 §3.3（删最后一个账户的兜底次序、全局字段被 `st` 带走、
   `_fmtCustom`、`_pathSuggest` 的拒绝路径、第二层的"根是字面量"）。

   **用例**：新增 `【D18-5】`（bindtest，69 条断言）：`validateAccountTree` 第一层
   （`/日记` / `日记/` / ` 日记 ` 归一化等价、库根、`[x]`、空参数不炸）与第二层（真撞的 pair、
   报错带文件路径、对方/自己格式非法都不炸、第一层优先）；默认 folder 让开（撞了加 `-2`、
   库根继承用 label、斜杠空白先归一化、`nextAccountId`/`nextAccountLabel`）；`addAccount`
   （id/label/diary 继承/空状态/落盘/服务重建/不打断在跑管道/上限 5 与提示）；`deleteAccount`
   （只删 a2、密钥清空、a1 一字节不动、选中邻居、删最后一个补空账户且保住 folder）；
   `unbindAccount`（只动一个、keepToken 回 half）；设置页帮手（`_d()` 是当前账户、改 a2 不碰 a1、
   `plugin.settings` 仍钉在账户 #1、`_writer()`/`_view()` 跟着账户）；加载兜底警告。
   实测 `npm run verify`：bindtest **739**（670 + 69），webcliptest **92**，既有断言零删改。

每步一个提交，**第 1 步合进去不改变任何行为**，2 之后随时可停（第 2 步做完就已经"两个账户各写各的树"，
只是还不能同时在线）。

**五步全部实现（2026-09-10）**：bindtest 585 → 627 → 646 → 670 → **739**，webcliptest 全程 **92** 不动，
每一步既有断言**零删改**。决策记录见 `00-decisions.md` D15。

---

## 9. 落地后剩下的

### 9.1 待拆：三个兼容垫片

`installAccountShim` / `installSettingsShim` / `installPipelineShim`（见 §3.1 与各步落地记录、
`00-decisions.md` D15 的表）。它们是"每一步都不改既有断言"这个验证策略的代价，**不是设计的一部分**。

拆它们要连带改掉既有断言里对旧字段名的引用（`p.data.ilink` 约 39 处、`p._client` / `p._running` /
`p._pollSettledTs` / `p._skipBacklog`、`p._stored.accounts[0].ilink`、以及"改 `plugin.settings` 后
应按共用模式写"那 6 条 B15）。建议单独一轮做，验收标准就是"拆完 `verify` 仍全过"。

### 9.2 待手工验证（自动测不到的部分）

设置页 DOM 不渲染、扫码要真机。第 5 步的变异验证已经确认**哪几处接线没有被用例覆盖**：

- 行内文件夹选择器的同树拒绝（把它改坏，739 条**全过**——说明没兜住）
- 账户列表的渲染与"点行切换"（含：点输入框不会重画、打字不丢焦点）
- 添加账户 → 立刻扫码 → `onLoginConfirmed(payload, newId)` 写进**新槽**；到第 6 个是否 `Notice`
- 重新扫码把绑写回**原槽**（不新建账户、不串 token）
- 删除账户的二次确认、以及"vault 文件一个没动"
- `_warnTreeConflicts` 的真实提示时机（手改 `data.json` 造同树后重启）
- 多账户时的行高与滚动位置

### 9.3 如果以后要时区 / 一天边界也按账户

那是"把时间底座改成可传参"的**独立一轮**：`_dateFmt` / `_timeFmt` / `_weekdayFmt` / `_dayStartHour` /
`_nudgeNightHour` 现在全是模块级状态，被 `todayStr`/`hhmmStr`/`weekdayForDate`/`logicalTodayStr`/
`isNightNow`/`reminderDue` 直接读。与本次重构解耦，别混着做。
