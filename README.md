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

## 7. 生成图片

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
