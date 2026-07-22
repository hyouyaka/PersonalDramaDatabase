# CommenTasks Scripts

这个仓库现在把数据维护拆成了 3 段：

1. 更新源库和 `missevan&manbo-cvid-map.json`
2. 更新两个播放量库
3. 根据源库和播放量库重建 SQLite

脚本之间默认不会自动串行调用，方便手动控制和断点续跑。

## 主要文件

- `missevan-drama-info.json`
  猫耳源库（含 `author`）
- `manbo-drama-info.json`
  漫播源库（含 `author`）
- `missevan-watch-counts.json`
  猫耳播放量缓存
- `manbo-watch-counts.json`
  漫播播放量缓存
- `missevan&manbo-cvid-map.json`
  双平台统一 CVID / alias 映射
- `DramasByCV.sqlite`
  最终聚合库

## GUI

脚本：`commen_tasks_gui.py`

用途：

- 提供桌面版操作入口，集中执行源库更新、播放量刷新、SQLite 重建和出图
- 提供 SQLite 浏览页，可分页查看表数据、做筛选，并执行只读 SQL

依赖：

```powershell
python -m pip install PySide6
```

运行：

```powershell
python commen_tasks_gui.py
```

说明：

- GUI 当前使用 `PySide6`
- 默认会根据系统 DPI 自动选择合适的界面缩放
- 也可以在 GUI 里手动切换 `100% / 125% / 150% / 175% / 200% / 自动`
- SQLite 页只允许执行 `SELECT` / `WITH` 开头的只读 SQL

## 1. 追加猫耳 ID

脚本：`append_missevan_ids.py`

用途：

- 追加或刷新指定猫耳 `dramaId`
- 更新 `missevan-drama-info.json`
- 同步写入剧目 `author`
- 更新 `missevan-watch-counts.json`
- 仅对本次追加剧集涉及到的主役 CV 保守更新 `missevan&manbo-cvid-map.json`
- 不更新 SQLite

用法：

```powershell
python append_missevan_ids.py <drama_id> [<drama_id> ...]
```

示例：

```powershell
python append_missevan_ids.py 92701
python append_missevan_ids.py 87590 91872 88696
```

说明：

- `cvid map updated` 表示这次涉及到的 CV 已在 map 中，但补充了平台 ID 或 alias
- `cvid map created` 表示这次涉及到的 CV 在 map 中没有唯一命中，因此新建了记录
- `cvid map unchanged` 表示这次涉及到的 CV 早就在 map 中，且平台 ID 已匹配，所以没有做任何改动
- append 只会观察这次追加的 `dramaId` 所涉及到的猫耳主役 CV，不会再扫漫播整库
- 如果 `cvid-map` 里出现多条可能命中的记录，脚本不会自动合并，只会在输出里提示 `ambiguous`
- 猫耳请求自带限频和 `418` 退避

## 2. 追加漫播 ID

脚本：`append_manbo_ids.py`

用途：

- 追加或刷新指定漫播 `dramaId`
- 更新 `manbo-drama-info.json`
- 同步写入剧目 `author`
- 更新 `manbo-watch-counts.json`
- 仅对本次追加剧集涉及到的主役 CV 保守更新 `missevan&manbo-cvid-map.json`
- 不更新 SQLite

用法：

```powershell
python append_manbo_ids.py <drama_id> [<drama_id> ...]
```

示例：

```powershell
python append_manbo_ids.py 2195128381907927121
python append_manbo_ids.py 2067945724439429338 2118896513449984153
```

说明：

- `cvid map updated` 表示这次涉及到的 CV 已在 map 中，但补充了平台 ID 或 alias
- `cvid map created` 表示这次涉及到的 CV 在 map 中没有唯一命中，因此新建了记录
- `cvid map unchanged` 表示这次涉及到的 CV 早就在 map 中，且平台 ID 已匹配，所以没有做任何改动
- append 只会观察这次追加的 `dramaId` 所涉及到的漫播主役 CV，不会再扫猫耳整库
- 如果 `cvid-map` 里出现多条可能命中的记录，脚本不会自动合并，只会在输出里提示 `ambiguous`

## 3. 刷新播放量库

脚本：`refresh_watch_counts.py`

用途：

- 只更新 `missevan-watch-counts.json`
- 只更新 `manbo-watch-counts.json`
- 不更新 `drama-info`
- 不更新 SQLite

缓存规则：

- 如果某条 `fetched_at` 距当前不足 1 小时，则跳过
- 适合猫耳触发 `418` 后断点继续

远端 Upstash 播放量存储：

- `missevan:watchcount:YYYY-MM-DD` / `manbo:watchcount:YYYY-MM-DD`：按日期保存的快照
- `missevan:watchcount:latest` / `manbo:watchcount:latest`：当前最新快照
- `missevan:watchcount:index` / `manbo:watchcount:index`：快照日期索引，最多保留 32 期
- `missevan:watchcount:history` / `manbo:watchcount:history`：Redis Hash，field 为 dramaId，value 为包含 `name` 和 `points` 的 JSON 字符串
- 发布顺序为“dated snapshot → latest → HSET 暂存 history → index → 清理过期 points/field → 删除淘汰快照”；index 或 history 写入失败会使任务失败，重试可安全重复执行
- 首次发布 index 时会通过 SCAN 回填已有日期；读取端优先使用 index，过渡期 index 缺失或不可用时使用带缓存的 SCAN fallback

watchcount index/history 会在常规发布时自动初始化和维护。v2 首发回填只读取现有 Upstash 数据，不请求平台 API：

```powershell
python sync_new_drama_ids.py --backfill-info-v2
python fetch_rank_data.py --backfill-rank-trend-v2
python build_cv_ranks.py --backfill-cv-trend-v2
```

清理已确认的非目标剧集时先预检，再显式应用；命令不会修改当前 CV 排名或 CV trend：

```powershell
python sync_new_drama_ids.py --purge-non-target-records
python sync_new_drama_ids.py --purge-non-target-records --apply
```

清理会为所有待改写远端键生成 `recovery_backups` 备份，并在目标集合、远端版本或 CV 资源摘要不符合预期时停止。

Info、普通榜、CV 榜及 trend v2 现为权威数据：正文与 Meta 使用 CAS 原子发布，冲突会重试，发布或校验失败会使任务失败。已存在的 legacy key 仅作为兼容副本同步，不会重新创建已退役的 legacy key。`UPSTASH_V2_PUBLISH_MODE=off` 只保留给非强制的旧版 v1→v2 兼容调用，不会关闭当前权威 v2 发布流程，不能作为紧急回滚开关。

兼容期若需把已有 Info v1 校准为权威 v2，先预检再应用；命令会备份原始 v1/v2，并使用双端 CAS 防止覆盖并发更新：

```powershell
python sync_new_drama_ids.py --sync-info-v1-from-v2
python sync_new_drama_ids.py --sync-info-v1-from-v2 --apply
```

用法：

```powershell
python refresh_watch_counts.py
python refresh_watch_counts.py --platform missevan
python refresh_watch_counts.py --platform manbo
python refresh_watch_counts.py --missevan 86686
python refresh_watch_counts.py --manbo 2195128381907927121
python refresh_watch_counts.py --missevan 86686 87590
python refresh_watch_counts.py --manbo 2195128381907927121 2067945724439429338
```

说明：

- `--platform all` 是默认值
- `--missevan` 和 `--manbo` 可以直接指定一个或多个 `dramaId`
- 只要传了 `--missevan` 或 `--manbo`，脚本就只刷新你指定的 ID，不会再跑全量
- 猫耳命中 `418` 时会先保存已完成进度，再退出

## 4. 清理漫播收费规则

脚本：`clean_manbo_pricing.py`

用途：

- 实时请求漫播 `dramaDetail?dramaId=...`
- 清理 `manbo-drama-info.json` 中命中的免费剧和 `100红豆剧`
- 同步删除 `manbo-watch-counts.json` 中对应 `dramaId`
- 不更新 `missevan&manbo-cvid-map.json`
- 不更新 SQLite

当前删除规则：

- 免费剧：
  `data.price=0`、`data.memberPrice=0`，
  且所有 `setRespList[].price=0`、`setRespList[].memberPrice=0`、`setRespList[].vipFree=0`
- `100红豆剧`：
  `data.price=100`、`data.memberPrice=100`，
  且所有 `setRespList[].price=0`、`setRespList[].memberPrice=0`、`setRespList[].vipFree=0`

用法：

```powershell
python clean_manbo_pricing.py
```

说明：

- 脚本会直接改写 `manbo-drama-info.json` 和 `manbo-watch-counts.json`
- 会在控制台输出本次删掉的 `dramaId + 标题 + 分类`
- 如果某些 `dramaId` 在脚本例外名单中，会被强制保留
- 跑完之后如果要同步最终库，需要手动执行：

```powershell
python rebuild_sqlite_from_libraries.py
```

## 5. 重建 SQLite

脚本：`rebuild_sqlite_from_libraries.py`

用途：

- 根据两个 `drama-info`、两个 `watch-count` 和 `missevan&manbo-cvid-map.json` 重建 `DramasByCV.sqlite`
- 可选同时导出 Excel

用法：

```powershell
python rebuild_sqlite_from_libraries.py
python rebuild_sqlite_from_libraries.py --export-workbook
```

聚合规则：

- 同平台 + 同 catalog + 同基础系列名 的多季合并为一行
- 跨平台或跨 catalog 不合并
- `role_names` 会 trim、去掉汉字之间空格、去重，并使用 `/` 连接
- `create_month` 取同系列最早的非空值
- `total_play_count` 为同系列全部 `dramaId` 播放量之和

## 6. 导出 Excel

脚本：`export_sqlite_to_workbook.py`

用途：

- 根据当前 `DramasByCV.sqlite` 导出 `DramasByCV_merged.xlsx`

用法：

```powershell
python export_sqlite_to_workbook.py
```

## 7. 抓取榜单与弹幕数据

脚本：`fetch_rank_data.py`

用途：

- 抓取双平台榜单并写入 `ranks.json`
- 合并 Upstash ongoing ID，补齐脱榜但仍需关注的剧目
- 按 12 小时缓存窗口决定是否刷新剧目 detail
- 刷新弹幕 UID 统计，并上传 partial / history / full rank store 到 Upstash

普通模式：

- 会先加载当前 store（优先远端 partial + 最新 metrics，失败再回退本地 `ranks.json`）
- 拉取榜单后，把榜单 ID 和 ongoing ID 合并去重
- 如果某个 drama 的 `fetched_at` 仍在 12 小时内，则默认跳过 detail 刷新
- 传 `--force` 时忽略 12 小时缓存，直接刷新选中的剧目
- 双平台同时开启时，猫耳和漫播的榜单抓取并行，detail 刷新也并行

弹幕规则：

- 默认会随 detail 一起刷新弹幕 UID 统计
- 传 `--skip-danmaku` 时，本次被更新到的 drama 会显式把 `danmaku_uid_count` 写成 `null`
- 除猫耳人气/畅销榜第 31–50 位外，不在弹幕目标子集里的 drama 会继续刷新 detail，并保留已有 `danmaku_uid_count`
- 猫耳人气周榜、人气月榜、畅销周榜、畅销月榜保存前 50 位；仅前 30 位计算弹幕 UID
- 仅位于上述榜单第 31–50 位、未进入任一榜前 30 且不在 ongoing 的 drama，会把 `danmaku_uid_count` 写成字符串 `"无需抓取"`
- `danmaku_uid_count` 的公开类型为非负整数或 `"无需抓取"`；补填和 `--only-danmaku` 均跳过该字符串

`--only-danmaku`：

- 不拉榜单，也不重新读取 ongoing
- 直接遍历当前 store 里已有的 drama metrics
- 传 `--force` 时，直接刷新这些现有 drama 的弹幕
- 不传 `--force` 时，只有在 `danmaku_uid_count` 为正且 `fetched_at` 仍在 12 小时缓存内时才跳过；其他情况都会刷新

常用命令：

```powershell
python fetch_rank_data.py
python fetch_rank_data.py --skip-danmaku
python fetch_rank_data.py --force
python fetch_rank_data.py --only-danmaku
python fetch_rank_data.py --only-danmaku --force
python fetch_rank_data.py --missevan-only
python fetch_rank_data.py --manbo-only
```

## 8. 生成图片

榜单图：

```powershell
python render_rank_images.py
```

明细图：

```powershell
python render_rank_detail_images.py
```

这两个脚本都依赖当前的 `DramasByCV.sqlite`。

执行时会在控制台依次要求输入两个日期：

- 猫耳数据截至日期
- 漫播数据截至日期

输入后，图片页脚会自动使用下面这种格式：

```text
猫耳数据截至date，漫播数据截至date
```

例如：

```text
请输入猫耳数据截至日期（如 2026/4/7）：2026/4/6
请输入漫播数据截至日期（如 2026/4/7）：2026/4/7
```

如果两张图想使用同一组日期，需要分别在各自脚本执行时各输入一次。

## 推荐顺序

如果你要完整更新一次数据，建议顺序是：

```powershell
python append_missevan_ids.py <ids>
python append_manbo_ids.py <ids>
python clean_manbo_pricing.py
python refresh_watch_counts.py
python rebuild_sqlite_from_libraries.py --export-workbook
python render_rank_images.py
python render_rank_detail_images.py
```

如果你只是在补源库，不想动 SQLite，就停在前两步或前三步。

## 注意事项

- `missevan&manbo-cvid-map.json` 当前允许存在重复或歧义记录
- 所有脚本都按“保守更新”处理，不会自动帮你合并疑似重复 CV
- 如果输出里出现 `ambiguous`，建议先手动检查 map 再继续后续步骤
