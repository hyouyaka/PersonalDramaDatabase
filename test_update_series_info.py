import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import update_series_info as series


def entry(title, ids, platform='猫耳', category='广播剧', **extra):
    return {'series title': title, 'platform': platform, 'category': category, 'dramaIds': ids, **extra}


def drama(id, title, cvs=('1',), category='广播剧', platform='猫耳', names=None):
    base, match, marked = series.parse_title(title)
    return series.Drama(platform, str(id), title, base, match, category,
                        frozenset(map(str, cvs)), names if names is not None else {str(v): f'演员{v}' for v in cvs}, marked)


def existing(**entries):
    return {'猫耳:人工组': entry('人工组', ['9999'], custom='保留'), **entries}


class TitleTests(unittest.TestCase):
    def test_all_markers(self):
        titles = [
            '作品第一季', '作品·第十二季', '作品【第1季】', '作品（第一季）',
            '作品 第一季（上）', '作品 第一季（下）', '作品 第三季 中',
            '作品 上季', '作品 下季', '作品 完结季（下）', '作品 最终季',
            '作品 番外篇', '作品 独家番外', '作品·番外·老树开花',
            '作品 第一季※起明篇※', '作品 第一季 ·「开学季」',
            '作品 第一季（CV：演员甲）', '作品 第一册', '作品·第二卷',
            '作品 上', '作品 下', '作品-上篇', '作品（中）', '作品 · 第 12 季',
        ]
        for title in titles:
            with self.subTest(title=title):
                self.assertEqual(series.parse_title(title), ('作品', '作品', True))

    def test_version_and_internal_brackets_preserved(self):
        self.assertEqual(series.parse_title('魔道祖师日语版 第二季（下）'), ('魔道祖师日语版', '魔道祖师日语版', True))
        self.assertEqual(series.parse_title('Vomic《作品》第二季（CV：甲）')[0], 'Vomic《作品》')
        self.assertEqual(series.parse_title('作品[穿书] 第一季')[0], '作品[穿书]')

    def test_ordinary_titles_and_full_season(self):
        for title in ['蝉鸣乐队·盛夏季', '鬼话连篇', '穿书后我靠吸大师兄续命', '作品']:
            self.assertEqual(series.parse_title(title), (title, series.normalize_title(title), False))
        self.assertEqual(series.parse_title('作品 全一季'), ('作品', '作品', False))
        self.assertEqual(series.normalize_title('Ａ　作品 ·'), series.normalize_title('A作品'))

    def test_post_season_versions_preserved(self):
        for qualifier in ['日语版', '英语版', '韩语版', '全新版', '重制版', '修订版']:
            for title in [f'作品 第一季（{qualifier}）', f'作品 第一季 {qualifier}',
                          f'作品【第1季】[{qualifier}]']:
                with self.subTest(title=title):
                    base = f'作品{qualifier}'
                    self.assertEqual(series.parse_title(title), (base, base, True))
        self.assertEqual(series.parse_title('作品 全一季（日语版）'), ('作品日语版', '作品日语版', False))

    def test_versions_with_parts_and_other_descriptions(self):
        titles = ['作品 第一季（上）（日语版）（CV：甲）',
                  '作品 第一季※起明篇※ · 日语版',
                  '作品 第二季（日语版，下）',
                  '作品 第二季（下）（CV：甲）（日语版）',
                  '作品 第一季（日语版，CV：演员甲）']
        for title in titles:
            with self.subTest(title=title):
                self.assertEqual(series.parse_title(title), ('作品日语版', '作品日语版', True))
        self.assertEqual(series.parse_title('Vomic《作品》第二季（CV：甲）（日语版）')[0], 'Vomic《作品》日语版')

    def test_duplicate_version_not_appended(self):
        for title in ['作品日语版 第一季（日语版）', '作品 第一季（日语版）（日语版）',
                      '作品 全新版 第一季（全新版）']:
            with self.subTest(title=title):
                base = '作品全新版' if '全新版' in title else '作品日语版'
                display = '作品 全新版' if title.startswith('作品 全新版') else base
                self.assertEqual(series.parse_title(title), (display, base, True))

    def test_cv_description_does_not_supply_versions(self):
        titles = ['作品 第一季（CV：日语版）', '作品 第一季 CV：英语版',
                  '作品 第一季（CV：甲（日语版）、演员乙英语版）',
                  '作品 第一季[cV: 全新版]']
        for title in titles:
            with self.subTest(title=title):
                self.assertEqual(series.parse_title(title), ('作品', '作品', True))
        self.assertEqual(series.parse_title('作品 第一季（CV：甲（日语版）、英语版）（重制版）')[0], '作品重制版')


class GroupingTests(unittest.TestCase):
    def update(self, rows, old=None):
        return series.build_update(old or existing(), rows)

    def test_bare_title_and_extra_episode(self):
        result, _ = self.update([drama(1, '作品'), drama(2, '作品番外篇')])
        self.assertEqual(result['猫耳:作品']['dramaIds'], ['1', '2'])

    def test_full_season_does_not_trigger_series(self):
        result, _ = self.update([drama(1, '作品'), drama(2, '作品 全一季')])
        self.assertNotIn('猫耳:作品', result)

    def test_cv_transitive_and_numeric_order(self):
        rows = [drama(10, '作品 第一季', ('1',)), drama(2, '作品 第二季', ('1', '2')), drama(30, '作品 第三季', ('2',))]
        result, _ = self.update(rows)
        self.assertEqual(result['猫耳:作品']['dramaIds'], ['2', '10', '30'])
        self.assertEqual(series.build_update(result, rows)[0], result)

    def test_cv_missing_no_merge(self):
        for cvs in [(), ('2',)]:
            result, _ = self.update([drama(1, '作品 第一季'), drama(2, '作品 第二季', cvs)])
            self.assertNotIn('猫耳:作品', result)

    def test_platform_and_language_are_separate(self):
        rows = [drama(1, '作品 第一季'), drama(2, '作品 第二季'),
                drama(3, '作品 第一季', platform='漫播'), drama(4, '作品 第二季', platform='漫播'),
                drama(5, '作品日语版 第一季'), drama(6, '作品日语版 第二季')]
        result, _ = self.update(rows)
        self.assertEqual(set(result) - {'猫耳:人工组'}, {'猫耳:作品', '漫播:作品', '猫耳:作品日语版'})

    def test_post_season_versions_separate_even_with_shared_cvs(self):
        rows = [drama(1, '作品 第一季'), drama(2, '作品 第二季'),
                drama(3, '作品 第一季（日语版）'), drama(4, '作品 第二季（日语版）')]
        result, _ = self.update(rows)
        self.assertEqual(result['猫耳:作品']['dramaIds'], ['1', '2'])
        self.assertEqual(result['猫耳:作品日语版']['dramaIds'], ['3', '4'])
        self.assertEqual(series.build_update(result, rows)[0], result)

    def test_pre_and_post_season_versions_join(self):
        rows = [drama(1, '作品日语版 第一季'), drama(2, '作品 第二季（日语版）'),
                drama(3, '作品日语版 番外篇（日语版）')]
        result, _ = self.update(rows)
        self.assertEqual(result['猫耳:作品日语版']['dramaIds'], ['1', '2', '3'])
        self.assertEqual(series.build_update(result, rows)[0], result)

    def test_existing_manual_mixed_versions_are_not_split(self):
        old = existing(**{'猫耳:人工版本组': entry('人工版本组', ['1', '2', '3', '4'])})
        rows = [drama(1, '作品 第一季'), drama(2, '作品 第二季'),
                drama(3, '作品 第一季（日语版）'), drama(4, '作品 第二季（日语版）')]
        result, _ = self.update(rows, old)
        self.assertEqual(result, old)

    def test_category_suffix_and_singleton_exception(self):
        rows = [drama(1, '作品 第一季'), drama(2, '作品 第二季'), drama(3, '作品 第一季', category='有声剧')]
        result, _ = self.update(rows)
        self.assertIn('猫耳:作品', result)
        rows.append(drama(4, '作品 第二季', category='有声剧'))
        result, _ = self.update(rows)
        self.assertIn('猫耳:作品 广播剧', result)
        self.assertIn('猫耳:作品 有声剧', result)
        self.assertNotIn('猫耳:作品', result)

    def test_cv_suffix_and_duplicate_names(self):
        rows = [drama(1, '作品 第一季', ('1',), names={'1': '甲'}), drama(2, '作品 第二季', ('1',), names={'1': '甲'}),
                drama(3, '作品 第一季', ('2',), names={'2': '乙'}), drama(4, '作品 第二季', ('2',), names={'2': '乙'})]
        result, _ = self.update(rows)
        self.assertIn('猫耳:作品（甲）', result)
        self.assertIn('猫耳:作品（乙）', result)
        for row in rows:
            row.cv_names = {v: '同名' for v in row.cvs}
        result, _ = self.update(rows)
        self.assertIn('猫耳:作品（同名；CV ID:1）', result)
        self.assertIn('猫耳:作品（同名；CV ID:2）', result)

    def test_missing_names_block_collision(self):
        rows = [drama(i, f'作品 第{i}季', (cv,), names={}) for i, cv in [(1, '1'), (2, '1'), (3, '2'), (4, '2')]]
        result, changes = self.update(rows)
        self.assertEqual(result, existing())
        self.assertTrue(changes.warnings)

    def test_old_members_order_extra_fields_and_manual_title(self):
        old = existing(**{'猫耳:人工作品组': entry('人工作品组', ['50', '1', '777'], custom={'a': 1})})
        rows = [drama(1, '作品 第一季'), drama(10, '作品 第二季'), drama(2, '作品 第三季')]
        result, changes = self.update(rows, old)
        self.assertEqual(result['猫耳:人工作品组']['dramaIds'], ['50', '1', '777', '2', '10'])
        self.assertEqual(result['猫耳:人工作品组']['custom'], {'a': 1})
        self.assertEqual(old['猫耳:人工作品组']['dramaIds'], ['50', '1', '777'])
        self.assertIn(('猫耳:人工作品组', '777'), changes.missing)

    def test_category_mismatch_preserves_old_metadata(self):
        old = existing(**{'猫耳:作品': entry('作品', ['1'], category='有声剧')})
        rows = [drama(1, '作品 第一季'), drama(2, '作品 第二季')]
        result, changes = self.update(rows, old)
        self.assertEqual(result['猫耳:作品']['category'], '有声剧')
        self.assertEqual(result['猫耳:作品']['dramaIds'], ['1', '2'])
        self.assertTrue(changes.warnings)

    def test_multiple_old_targets_are_not_merged(self):
        old = existing(**{'猫耳:组甲': entry('组甲', ['1']), '猫耳:组乙': entry('组乙', ['2'])})
        result, changes = self.update([drama(1, '作品 第一季'), drama(2, '作品 第二季'), drama(3, '作品 第三季')], old)
        self.assertEqual(result, old)
        self.assertTrue(changes.warnings)

    def test_old_missing_anchor_does_not_match_by_title_only(self):
        old = existing(**{'猫耳:作品': entry('作品', ['777'])})
        result, changes = self.update([drama(1, '作品 第一季'), drama(2, '作品 第二季')], old)
        self.assertEqual(result, old)
        self.assertTrue(changes.warnings)

    def test_existing_series_renamed_on_new_category_collision(self):
        old = existing(**{'猫耳:作品': entry('作品', ['1', '2'], custom=True)})
        rows = [drama(1, '作品 第一季'), drama(2, '作品 第二季'),
                drama(3, '作品 第一季', category='有声剧'), drama(4, '作品 第二季', category='有声剧')]
        result, changes = self.update(rows, old)
        self.assertNotIn('猫耳:作品', result)
        self.assertTrue(result['猫耳:作品 广播剧']['custom'])
        self.assertEqual(changes.renamed, [('猫耳:作品', '猫耳:作品 广播剧')])
        self.assertEqual(series.build_update(result, rows)[0], result)

    def test_explicit_manual_name_preserved(self):
        old = existing(**{'猫耳:作品（甲版）': entry('作品（甲版）', ['1', '2'])})
        rows = [drama(1, '作品 第一季'), drama(2, '作品 第二季'), drama(3, '作品 第一季', ('2',)), drama(4, '作品 第二季', ('2',))]
        result, _ = self.update(rows, old)
        self.assertIn('猫耳:作品（甲版）', result)
        self.assertIn('猫耳:作品（演员2）', result)

    def test_manual_disconnected_components_sort_all_new_members(self):
        old = existing(**{'猫耳:作品': entry('作品', ['1', '2', '777'])})
        rows = [drama(1, '作品 第一季', ('1',)), drama(50, '作品 第二季', ('1',)),
                drama(2, '作品 第一季', ('2',)), drama(3, '作品 第二季', ('2',))]
        result, changes = self.update(rows, old)
        self.assertEqual(result['猫耳:作品']['dramaIds'], ['1', '2', '777', '3', '50'])
        self.assertEqual(changes.appended['猫耳:作品'], ['3', '50'])

    def test_disconnected_manual_series_renamed_once_and_keeps_all_additions(self):
        old = existing(**{'猫耳:作品': entry('作品', ['1', '2', '777'], custom=True)})
        rows = [drama(1, '作品 第一季', ('1',)), drama(50, '作品 第二季', ('1',)),
                drama(2, '作品 第一季', ('2',)), drama(3, '作品 第二季', ('2',)),
                drama(4, '作品 第一季', ('3',)), drama(5, '作品 第二季', ('3',))]
        result, changes = self.update(rows, old)
        key = '猫耳:作品（演员1）'
        self.assertEqual(result[key]['dramaIds'], ['1', '2', '777', '3', '50'])
        self.assertTrue(result[key]['custom'])
        self.assertEqual(changes.renamed, [('猫耳:作品', key)])
        self.assertEqual(changes.appended, {key: ['3', '50']})
        self.assertEqual(series.build_update(result, rows)[0], result)

    def test_manual_series_multiple_base_titles_uses_final_key_for_additions(self):
        old = existing(**{'猫耳:B': entry('B', ['1', '2', '777'], custom=True)})
        rows = [drama(1, 'A 第一季', ('1',)), drama(50, 'A 第二季', ('1',)),
                drama(2, 'B 第一季', ('2',)), drama(3, 'B 第二季', ('2',)),
                drama(4, 'B 第一季', ('3',)), drama(5, 'B 第二季', ('3',))]
        result, changes = self.update(rows, old)
        key = '猫耳:B（演员2）'
        self.assertEqual(result[key]['dramaIds'], ['1', '2', '777', '3', '50'])
        self.assertTrue(result[key]['custom'])
        self.assertEqual(changes.renamed, [('猫耳:B', key)])
        self.assertEqual(changes.appended, {key: ['3', '50']})
        self.assertEqual(series.build_update(result, rows)[0], result)

    def test_conflicting_manual_series_rename_requests_preserve_name_and_additions(self):
        old = existing(**{'猫耳:作品': entry('作品', ['1', '2', '777'], custom=True)})
        rows = [drama(1, '作品 第一季', ('1',)), drama(50, '作品 第二季', ('1',)),
                drama(2, '作品 第一季', ('2',), category='有声剧'),
                drama(3, '作品 第二季', ('2',), category='有声剧'),
                drama(4, '作品 第一季', ('3',)), drama(5, '作品 第二季', ('3',))]
        result, changes = self.update(rows, old)
        self.assertEqual(result['猫耳:作品']['dramaIds'], ['1', '2', '777', '3', '50'])
        self.assertTrue(result['猫耳:作品']['custom'])
        self.assertEqual(changes.appended, {'猫耳:作品': ['3', '50']})
        self.assertEqual(changes.renamed, [])
        self.assertTrue(any('多个候选要求不同改名' in warning for warning in changes.warnings))
        self.assertEqual(series.build_update(result, rows)[0], result)
        self.assertEqual(series.build_update(old, list(reversed(rows)))[0], result)

    def test_conflicting_destination_never_overwrites_manual_series(self):
        old = existing(**{'猫耳:作品 广播剧': entry('作品 广播剧', ['999'], custom=True)})
        rows = [drama(1, '作品 第一季'), drama(2, '作品 第二季'),
                drama(3, '作品 第一季', category='有声剧'), drama(4, '作品 第二季', category='有声剧')]
        result, changes = self.update(rows, old)
        self.assertEqual(result['猫耳:作品 广播剧'], old['猫耳:作品 广播剧'])
        self.assertIn('猫耳:作品 有声剧', result)
        self.assertTrue(any('名称冲突' in warning for warning in changes.warnings))

    def test_cv_name_combination_disambiguates_shared_display_name(self):
        rows = [drama(1, '作品 第一季', ('1', '2'), names={'1': '同名', '2': '甲'}),
                drama(2, '作品 第二季', ('1', '2'), names={'1': '同名', '2': '甲'}),
                drama(3, '作品 第一季', ('3', '4'), names={'3': '同名', '4': '乙'}),
                drama(4, '作品 第二季', ('3', '4'), names={'3': '同名', '4': '乙'})]
        result, _ = self.update(rows)
        self.assertIn('猫耳:作品（同名、甲）', result)
        self.assertIn('猫耳:作品（同名、乙）', result)

    def test_migrations_union_and_idempotence(self):
        for source, target in series.MIGRATIONS.items():
            old = existing(**{source: entry(source.split(':')[1], ['1', '9'], custom=True),
                              target: entry(target.split(':')[1], ['2', '1'])})
            result, changes = self.update([], old)
            self.assertNotIn(source, result)
            self.assertEqual(result[target]['dramaIds'], ['2', '1', '9'])
            self.assertTrue(result[target]['custom'])
            self.assertIn(source, changes.deleted)
            self.assertEqual(series.build_update(result, [])[0], result)


class RecordsTests(unittest.TestCase):
    def test_manbo_names_fall_back_at_the_same_index(self):
        row = {'dramaId': 1, 'name': '作品 第一季', 'catalog': 1,
               'mainCvIds': [1, 2, 3, 4, 5, 6],
               'mainCvNames': ['实名', '', '  ', None],
               'mainCvNicknames': ['昵称1', '昵称2', '昵称3', '昵称4', '昵称5']}
        parsed = series.read_drama_records({'records': [row]}, {})[0]
        self.assertEqual(parsed.cv_names, {'1': '实名', '2': '昵称2', '3': '昵称3',
                                           '4': '昵称4', '5': '昵称5'})
        self.assertEqual(parsed.cvs, frozenset(map(str, range(1, 7))))

    def test_manbo_empty_names_still_disambiguate_series_using_nicknames(self):
        records = []
        for id, season, cv, nickname in [(1, '第一季', 10, '甲'), (2, '第二季', 10, '甲'),
                                         (3, '第一季', 20, '乙'), (4, '第二季', 20, '乙')]:
            records.append({'dramaId': id, 'name': f'作品 {season}', 'catalog': 1,
                            'mainCvIds': [cv], 'mainCvNames': [''], 'mainCvNicknames': [nickname]})
        rows = series.read_drama_records({'records': records}, {})
        result, changes = series.build_update(existing(), rows)
        self.assertEqual(result['漫播:作品（甲）']['dramaIds'], ['1', '2'])
        self.assertEqual(result['漫播:作品（乙）']['dramaIds'], ['3', '4'])
        self.assertFalse(changes.warnings)
        self.assertEqual(series.build_update(result, rows)[0], result)

    def test_ids_types_and_unknown_cvs(self):
        manbo = {'records': [{'dramaId': 3, 'name': '作品 第一季', 'catalog': 1,
                              'mainCvIds': [100, 0, None], 'mainCvNames': ['演员', '', '']}]}
        missevan = {'1': {'dramaId': 1, 'title': '作品 第一季', 'catalog': 96, 'maincvs': [10], 'cvnames': {'10': '演员'}},
                    '2': {'dramaId': 2, 'title': '作品 第二季', 'catalog': 89, 'maincvs': [11], 'fallbackCvNames': ['主役未知']}}
        rows = series.read_drama_records(manbo, missevan)
        self.assertEqual(rows[0].cvs, frozenset({'100'}))
        self.assertEqual(rows[1].category, '有声漫')
        self.assertEqual(rows[2].cvs, frozenset())

    def test_bad_record_rejected(self):
        with self.assertRaises(ValueError):
            series.read_drama_records({'records': [{'dramaId': 1}]}, {})


class FakeUpstash:
    def __init__(self, *, conflict=False, verify_failure=False, unchanged=False):
        self.rows = [drama(1, '作品 第一季'), drama(2, '作品 第二季')]
        old = existing()
        if unchanged:
            old, _ = series.build_update(old, self.rows)
        self.values = {series.SERIES_INFO_KEY: json.dumps(old), series.MANBO_INFO_KEY: json.dumps({'records': []}),
                       series.MISSEVAN_INFO_KEY: '{}'}
        self.calls = []
        self.conflict = conflict
        self.verify_failure = verify_failure
        self.published = False

    def __call__(self, command):
        self.calls.append(command)
        if command[0] == 'GET':
            if self.published and self.verify_failure and command[1] == series.SERIES_INFO_KEY:
                return '{}'
            return self.values[command[1]]
        if command[0] == 'EVAL':
            if self.conflict:
                return 0
            self.values[series.SERIES_INFO_KEY] = command[-1]
            self.published = True
            return 1
        raise AssertionError(command)


class IOTests(unittest.TestCase):
    def run_update(self, fake, directory, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()), \
             patch.object(series, 'assert_info_download_is_safe'), \
             patch.object(series, 'read_drama_records', return_value=fake.rows):
            return series.update_series_info(upstash=fake, path=Path(directory) / 'series.json',
                                             backup_root=Path(directory) / 'backups', backup_local=Mock(), **kwargs)

    def test_dry_run_has_no_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeUpstash()
            self.run_update(fake, directory, dry_run=True)
            self.assertFalse(fake.published)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_success_snapshots_atomic_local_and_remote(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeUpstash()
            result = self.run_update(fake, directory)
            self.assertEqual(json.loads((Path(directory) / 'series.json').read_text(encoding='utf-8')), result)
            self.assertEqual(json.loads(fake.values[series.SERIES_INFO_KEY]), result)
            self.assertEqual(len(list((Path(directory) / 'backups').rglob('*.json'))), 3)
            self.assertEqual(list(Path(directory).glob('*.tmp')), [])

    def test_unchanged_does_not_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeUpstash(unchanged=True)
            self.run_update(fake, directory)
            self.assertFalse(fake.published)

    def test_conflict_reloads_all_three_keys_three_times(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeUpstash(conflict=True)
            with self.assertRaisesRegex(RuntimeError, '连续三次'):
                self.run_update(fake, directory)
            for key in series.KEYS:
                self.assertEqual(sum(c == ['GET', key] for c in fake.calls), 3)
            self.assertFalse((Path(directory) / 'series.json').exists())

    def test_conflict_retry_preserves_concurrent_manual_edit(self):
        fake = FakeUpstash()
        original_call = fake.__call__
        attempts = []
        def call(command):
            if command[0] == 'EVAL' and not attempts:
                attempts.append(1)
                payload = json.loads(fake.values[series.SERIES_INFO_KEY])
                payload['猫耳:人工组']['dramaIds'].append('12345')
                fake.values[series.SERIES_INFO_KEY] = json.dumps(payload)
                return 0
            return original_call(command)
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()), \
             patch.object(series, 'assert_info_download_is_safe'), \
             patch.object(series, 'read_drama_records', return_value=fake.rows):
            result = series.update_series_info(upstash=call, path=Path(directory) / 'series.json',
                                               backup_root=Path(directory) / 'backups', backup_local=Mock())
        self.assertIn('12345', result['猫耳:人工组']['dramaIds'])

    def test_verification_failure_does_not_write_local(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, '校验失败'):
                self.run_update(FakeUpstash(verify_failure=True), directory)
            self.assertFalse((Path(directory) / 'series.json').exists())

    def test_local_failure_reports_published_state(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, '远端已更新、本地未同步'):
                self.run_update(FakeUpstash(), directory, write_local=Mock(side_effect=OSError('disk full')))

    def test_download_failure_and_invalid_payload_no_write(self):
        for value in [None, 'not-json', '{}']:
            with tempfile.TemporaryDirectory() as directory:
                fake = FakeUpstash()
                fake.values[series.SERIES_INFO_KEY] = value
                with self.assertRaises((ValueError, json.JSONDecodeError)):
                    self.run_update(fake, directory)
                self.assertEqual(list(Path(directory).iterdir()), [])
                self.assertFalse(fake.published)

    def test_failure_reading_platform_key_does_not_write(self):
        for key in (series.MANBO_INFO_KEY, series.MISSEVAN_INFO_KEY):
            with tempfile.TemporaryDirectory() as directory:
                fake = FakeUpstash()
                fake.values[key] = None
                with self.assertRaises(ValueError):
                    self.run_update(fake, directory)
                self.assertFalse(fake.published)
                self.assertEqual(list(Path(directory).iterdir()), [])

    def test_atomic_write_failure_preserves_original_and_removes_temp(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'series.json'
            path.write_text('original', encoding='utf-8')
            with patch.object(series.os, 'replace', side_effect=OSError('locked')):
                with self.assertRaises(OSError):
                    series.atomic_write(path, 'replacement')
            self.assertEqual(path.read_text(encoding='utf-8'), 'original')
            self.assertEqual(list(Path(directory).glob('*.tmp')), [])

    def test_main_failure_nonzero(self):
        with contextlib.redirect_stdout(io.StringIO()), patch.object(series, 'load_env_file'), \
             patch.object(series, 'configure_stdio'), patch.object(series, 'update_series_info', side_effect=RuntimeError('failed')):
            self.assertEqual(series.main(['--dry-run']), 1)


if __name__ == '__main__':
    unittest.main()
