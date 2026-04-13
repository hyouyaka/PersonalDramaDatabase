from __future__ import annotations

import argparse
import re
import sqlite3
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "DramasByCV.sqlite"
SQL_PATH = ROOT / "DramaByCV.rank.sql"
OUTPUT_DIR = ROOT / "output" / "xhs_rank_details"

FONT_REGULAR = Path(r"C:\Windows\Fonts\msyh.ttc")
FONT_BOLD = Path(r"C:\Windows\Fonts\msyhbd.ttc")
FONT_POSTER = Path(r"C:\Windows\Fonts\方正大黑_GBK.TTF")
FONT_EMOJI = Path(r"C:\Windows\Fonts\seguiemj.ttf")

WIDTH = 1700
MARGIN_X = 70
TOP = 120
SECTION_GAP = 34
TABLE_HEADER_H = 54
ROW_H = 54
FOOTER_H = 80

TARGET_PAGE_COUNT = 16


def prompt_required_text(prompt: str) -> str:
    while True:
        value = input(prompt).strip()
        if value:
            return value
        print("输入不能为空。")


def prompt_footer_text() -> str:
    missevan_date = prompt_required_text("请输入猫耳数据截至日期（如 2026/4/7）：")
    manbo_date = prompt_required_text("请输入漫播数据截至日期（如 2026/4/7）：")
    return f"by太殷雀翎**猫耳数据截至{missevan_date}，漫播数据截至{manbo_date}"


def build_footer_text(missevan_date: str, manbo_date: str) -> str:
    return f"by太殷雀翎**猫耳数据截至{missevan_date}，漫播数据截至{manbo_date}"


def load_rank_query() -> str:
    text = SQL_PATH.read_text(encoding="utf-8")
    block = text.split("--1. Ranking: both platforms, all works", 1)[1]
    query = block.strip().split(";", 1)[0]
    return query


def map_catalog_name(value: object) -> str:
    return str(value or "").strip()


def build_type_label(genre: object, catalog_name: object) -> str:
    genre_text = str(genre or "").strip()
    catalog_text = str(catalog_name or "").strip()
    if genre_text and catalog_text:
        return f"{genre_text}{catalog_text}"
    return genre_text or catalog_text


def platform_parts(value: object) -> tuple[str, str]:
    text = str(value or "").strip()
    if text == "猫耳":
        return "猫耳", "🐱"
    if text == "漫播":
        return "漫播", "🦊"
    return text, ""


def fmt_play_count(value: object) -> str:
    if value in (None, ""):
        return ""
    n = int(value)
    if n >= 100000000:
        return f"{n / 100000000:.2f}亿"
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    return str(n)


def fit_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> str:
    if draw.textlength(text, font=font) <= max_width:
        return text
    suffix = "..."
    low, high = 0, len(text)
    best = suffix
    while low <= high:
        mid = (low + high) // 2
        candidate = text[:mid].rstrip() + suffix
        if draw.textlength(candidate, font=font) <= max_width:
            best = candidate
            low = mid + 1
        else:
            high = mid - 1
    return best


def fit_font_full_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font_path: Path,
    base_size: int,
    min_size: int,
    max_width: int,
) -> ImageFont.FreeTypeFont:
    for size in range(base_size, min_size - 1, -1):
        font = ImageFont.truetype(str(font_path), size)
        if draw.textlength(text, font=font) <= max_width:
            return font
    return ImageFont.truetype(str(font_path), min_size)


def draw_platform_text(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    platform_value: str,
    text_font: ImageFont.FreeTypeFont,
    emoji_font: ImageFont.FreeTypeFont,
    fill=(84, 84, 84),
) -> None:
    text, emoji = platform_parts(platform_value)
    draw.text((x, y), text, font=text_font, fill=fill)
    if emoji:
        tx = x + int(draw.textlength(text, font=text_font)) + 2
        draw.text((tx, y - 2), emoji, font=emoji_font, embedded_color=True)


def draw_gradient_background(img: Image.Image, top_color, bottom_color):
    px = img.load()
    width, height = img.size
    for y in range(height):
        ratio = y / max(1, height - 1)
        color = tuple(int(top_color[i] * (1 - ratio) + bottom_color[i] * ratio) for i in range(3))
        for x in range(width):
            px[x, y] = color


def theme_for_index(idx: int):
    themes = [
        {"bg_top": (248, 231, 221), "bg_bottom": (253, 247, 241), "accent": (178, 79, 52), "panel": (255, 252, 249)},
        {"bg_top": (222, 237, 229), "bg_bottom": (245, 250, 247), "accent": (44, 111, 86), "panel": (252, 255, 253)},
        {"bg_top": (224, 232, 248), "bg_bottom": (246, 248, 253), "accent": (64, 92, 154), "panel": (251, 253, 255)},
        {"bg_top": (245, 227, 231), "bg_bottom": (252, 245, 247), "accent": (161, 63, 89), "panel": (255, 252, 253)},
    ]
    return themes[idx % len(themes)]


def fetch_top30_and_details():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rank_rows = conn.execute(load_rank_query() + " LIMIT 30").fetchall()
    top30 = []
    for row in rank_rows:
        rank = int(row["排名"])
        cv_name = row["CV名称"]
        works = conn.execute(
            """
            SELECT title, genre, catalog_name, role_names, total_play_count, create_month, platform
            FROM cv_works
            WHERE cv_name = ?
            ORDER BY COALESCE(total_play_count, 0) DESC, platform, title
            """,
            (cv_name,),
        ).fetchall()
        top30.append(
            {
                "rank": rank,
                "cv_name": cv_name,
                "total_play": row["总播放量"],
                "lead_count": int(row["主役总数"]),
                "works": [
                    {
                        "title": w["title"],
                        "catalog": map_catalog_name(w["catalog_name"]),
                        "type_label": build_type_label(w["genre"], w["catalog_name"]),
                        "role": w["role_names"] or "",
                        "play": fmt_play_count(w["total_play_count"]),
                        "create_month": w["create_month"] or "",
                        "platform": w["platform"],
                    }
                    for w in works
                ],
            }
        )
    conn.close()
    return top30


def make_cover(footer_text: str):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (1400, 1800), (255, 255, 255))
    draw_gradient_background(img, (245, 232, 224), (252, 247, 242))
    draw = ImageDraw.Draw(img)

    poster_font = ImageFont.truetype(str(FONT_POSTER if FONT_POSTER.exists() else FONT_BOLD), 118)
    sub_font = ImageFont.truetype(str(FONT_BOLD), 70)
    foot_font = ImageFont.truetype(str(FONT_REGULAR), 26)

    draw.rectangle((42, 42, 1358, 1758), fill=(255, 252, 249), outline=(227, 218, 211), width=3)
    draw.ellipse((1030, 70, 1330, 370), fill=(236, 214, 203))
    draw.ellipse((70, 1430, 350, 1710), fill=(241, 226, 217))

    lines = ["总榜Top30", "CV剧集明细"]
    for idx, text in enumerate(lines):
        bbox = draw.textbbox((0, 0), text, font=poster_font)
        x = (1400 - (bbox[2] - bbox[0])) / 2
        draw.text((x, 470 + idx * 170), text, font=poster_font, fill=(48, 40, 37))

    sub = "双平台总播放量排行"
    bbox = draw.textbbox((0, 0), sub, font=sub_font)
    draw.text(((1400 - (bbox[2] - bbox[0])) / 2, 900), sub, font=sub_font, fill=(178, 79, 52))
    draw.text((110, 1630), footer_text, font=foot_font, fill=(128, 118, 112))

    out = OUTPUT_DIR / "00_总榜Top30剧集明细_封面.png"
    img.save(out, quality=95)
    return out


def estimate_height(group_rows: list[dict]) -> int:
    total = TOP + 120
    for cv in group_rows:
        total += 92  # block title/meta
        total += TABLE_HEADER_H
        total += len(cv["works"]) * ROW_H
        total += SECTION_GAP
    total += FOOTER_H
    return total


def build_page_groups(ranked: list[dict], page_count: int = TARGET_PAGE_COUNT) -> list[tuple[int, int]]:
    # Keep rank #1 on a standalone page, then balance the remaining rows.
    if not ranked:
        return []
    if len(ranked) == 1:
        return [(ranked[0]["rank"], ranked[0]["rank"])]

    first_group = [(ranked[0]["rank"], ranked[0]["rank"])]
    remaining = ranked[1:]
    remaining_page_count = max(1, page_count - 1)

    # Use the same height model as rendering so pages stay visually balanced.
    n = len(remaining)
    weights = [
        92 + TABLE_HEADER_H + len(item["works"]) * ROW_H + SECTION_GAP
        for item in remaining
    ]
    prefix = [0]
    for w in weights:
        prefix.append(prefix[-1] + w)

    def seg_cost(i: int, j: int) -> int:
        return prefix[j] - prefix[i]

    inf = 10**18
    dp = [[inf] * (remaining_page_count + 1) for _ in range(n + 1)]
    prev = [[-1] * (remaining_page_count + 1) for _ in range(n + 1)]
    dp[0][0] = 0

    for i in range(1, n + 1):
        for k in range(1, min(remaining_page_count, i) + 1):
            for t in range(k - 1, i):
                worst = max(dp[t][k - 1], seg_cost(t, i))
                if worst < dp[i][k]:
                    dp[i][k] = worst
                    prev[i][k] = t

    groups_rev = []
    i, k = n, min(remaining_page_count, n)
    while k > 0 and i > 0:
        t = prev[i][k]
        if t < 0:
            break
        groups_rev.append((remaining[t]["rank"], remaining[i - 1]["rank"]))
        i, k = t, k - 1

    return first_group + list(reversed(groups_rev))


def render_page(page_idx: int, group_rows: list[dict], rank_range: tuple[int, int], footer_text: str) -> Path:
    theme = theme_for_index(page_idx - 1)
    height = estimate_height(group_rows)
    img = Image.new("RGB", (WIDTH, height), (255, 255, 255))
    draw_gradient_background(img, theme["bg_top"], theme["bg_bottom"])
    draw = ImageDraw.Draw(img)

    title_font = ImageFont.truetype(str(FONT_BOLD), 52)
    sub_font = ImageFont.truetype(str(FONT_REGULAR), 24)
    cv_font = ImageFont.truetype(str(FONT_BOLD), 38)
    meta_font = ImageFont.truetype(str(FONT_REGULAR), 24)
    th_font = ImageFont.truetype(str(FONT_BOLD), 26)
    td_font = ImageFont.truetype(str(FONT_REGULAR), 25)
    emoji_font = ImageFont.truetype(str(FONT_EMOJI if FONT_EMOJI.exists() else FONT_REGULAR), 24)
    foot_font = ImageFont.truetype(str(FONT_REGULAR), 22)

    draw.rectangle((44, 40, WIDTH - 44, height - 40), fill=(255, 255, 255), outline=(227, 218, 211), width=2)
    draw.ellipse((WIDTH - 250, 34, WIDTH - 34, 250), fill=theme["bg_top"])
    draw.text((MARGIN_X, TOP), "总榜Top30剧集明细", font=title_font, fill=(35, 35, 35))
    cv_list_text = "本页包含：" + "、".join(item["cv_name"] for item in group_rows)
    draw.text((MARGIN_X, TOP + 74), cv_list_text, font=sub_font, fill=theme["accent"])

    col_title = MARGIN_X + 18
    col_type = 640
    col_role = 870
    col_play = 1255
    col_time = 1420
    col_platform = 1560
    title_w = col_type - col_title - 24
    type_w = col_role - col_type - 24
    role_w = col_play - col_role - 24
    play_w = col_time - col_play - 24
    time_w = col_platform - col_time - 24

    y = TOP + 140
    for cv in group_rows:
        draw.rectangle((MARGIN_X, y, WIDTH - MARGIN_X, y + 82), fill=theme["panel"], outline=(230, 223, 218), width=2)
        draw.text((MARGIN_X + 18, y + 12), f"#{cv['rank']} {cv['cv_name']}", font=cv_font, fill=theme["accent"])
        meta_text = f"总播放量 {cv['total_play']}    主役总数 {cv['lead_count']}"
        draw.text((MARGIN_X + 360, y + 22), meta_text, font=meta_font, fill=(94, 94, 94))
        y += 92

        draw.rectangle((MARGIN_X, y, WIDTH - MARGIN_X, y + TABLE_HEADER_H), fill=(244, 240, 236))
        for text, x in [("作品", col_title), ("类型", col_type), ("角色", col_role), ("总播放量", col_play), ("上线时间", col_time), ("平台", col_platform)]:
            draw.text((x, y + 12), text, font=th_font, fill=(72, 72, 72))
        y += TABLE_HEADER_H

        for idx, work in enumerate(cv["works"]):
            row_fill = None
            if work["catalog"] in ("有声剧", "有声书"):
                row_fill = (245, 236, 221)
            elif work["catalog"] == "有声漫":
                row_fill = (227, 238, 245)
            elif idx % 2 == 1:
                row_fill = (250, 248, 246)
            if row_fill is not None:
                draw.rectangle((MARGIN_X, y, WIDTH - MARGIN_X, y + ROW_H), fill=row_fill)
            draw.text((col_title, y + 12), fit_text(draw, work["title"], td_font, title_w), font=td_font, fill=(35, 35, 35))
            draw.text((col_type, y + 12), fit_text(draw, work["type_label"], td_font, type_w), font=td_font, fill=(84, 84, 84))
            role_font = fit_font_full_text(draw, work["role"], FONT_REGULAR, 25, 15, role_w)
            draw.text((col_role, y + 12 + max(0, (25 - role_font.size) // 2)), work["role"], font=role_font, fill=(84, 84, 84))
            draw.text((col_play, y + 12), fit_text(draw, work["play"], td_font, play_w), font=td_font, fill=(84, 84, 84))
            draw.text((col_time, y + 12), fit_text(draw, work["create_month"], td_font, time_w), font=td_font, fill=(84, 84, 84))
            draw_platform_text(draw, col_platform, y + 12, work["platform"], td_font, emoji_font)
            y += ROW_H

        y += SECTION_GAP

    draw.text((MARGIN_X, height - 48), footer_text, font=foot_font, fill=(128, 118, 112))
    out = OUTPUT_DIR / f"{page_idx:02d}_总榜Top30剧集明细_{rank_range[0]}_{rank_range[1]}.png"
    img.save(out, quality=95)
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--missevan-date", help="猫耳数据截至日期，例如 2026/4/7")
    parser.add_argument("--manbo-date", help="漫播数据截至日期，例如 2026/4/7")
    args = parser.parse_args()

    if args.missevan_date and args.manbo_date:
        footer_text = build_footer_text(args.missevan_date.strip(), args.manbo_date.strip())
    else:
        footer_text = prompt_footer_text()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for old in OUTPUT_DIR.glob("*.png"):
        try:
            old.unlink()
        except PermissionError:
            pass

    ranked = fetch_top30_and_details()
    by_rank = {item["rank"]: item for item in ranked}
    page_groups = build_page_groups(ranked)

    outputs = [make_cover(footer_text)]
    for page_idx, (start_rank, end_rank) in enumerate(page_groups, start=1):
        group = [by_rank[r] for r in range(start_rank, end_rank + 1)]
        outputs.append(render_page(page_idx, group, (start_rank, end_rank), footer_text))

    print("Created:")
    for path in outputs:
        print(path)


if __name__ == "__main__":
    main()
