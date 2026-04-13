from __future__ import annotations

import argparse
import re
import sqlite3
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "DramasByCV.sqlite"
SQL_PATH = ROOT / "DramaByCV.rank.sql"
OUTPUT_DIR = ROOT / "output" / "xhs_rank_images"

FONT_REGULAR = Path(r"C:\Windows\Fonts\msyh.ttc")
FONT_BOLD = Path(r"C:\Windows\Fonts\msyhbd.ttc")

BASE_WIDTH = 1700
BASE_HEIGHT = 2800
MARGIN_X = 70
TOP = 150
TABLE_TOP = TOP + 180
FOOTER_H = 90
ROW_H = 84


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


def load_queries() -> list[tuple[str, str]]:
    text = SQL_PATH.read_text(encoding="utf-8")
    queries: list[tuple[str, str]] = []
    parts = re.split(r"(?m)^--(?=\d+\.)", text)
    for part in parts:
        block = part.strip()
        if not block:
            continue
        lines = ("--" + block).splitlines()
        title = lines[0].lstrip("-").strip()
        sql = "\n".join(lines[1:]).strip()
        if sql.endswith(";"):
            sql = sql[:-1]
        queries.append((title, sql))
    return queries


def fetch_top30(sql: str) -> list[tuple]:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(f"SELECT * FROM ({sql}) LIMIT 30").fetchall()
    conn.close()
    return rows


def fit_text(draw, text, font, max_width):
    text = str(text or "")
    if not text:
        return ""
    if draw.textlength(text, font=font) <= max_width:
        return text
    suffix = "..."
    low, high = 0, len(text)
    best = ""
    while low <= high:
        mid = (low + high) // 2
        candidate = text[:mid].rstrip() + suffix
        if draw.textlength(candidate, font=font) <= max_width:
            best = candidate
            low = mid + 1
        else:
            high = mid - 1
    return best or suffix


def draw_gradient_background(img: Image.Image, top_color, bottom_color):
    width, height = img.size
    px = img.load()
    for y in range(height):
        ratio = y / max(1, height - 1)
        color = tuple(int(top_color[i] * (1 - ratio) + bottom_color[i] * ratio) for i in range(3))
        for x in range(width):
            px[x, y] = color


def theme_for_index(idx: int):
    themes = [
        {"bg_top": (248, 231, 221), "bg_bottom": (253, 247, 241), "accent": (178, 79, 52), "card": (255, 252, 249)},
        {"bg_top": (222, 237, 229), "bg_bottom": (245, 250, 247), "accent": (44, 111, 86), "card": (252, 255, 253)},
        {"bg_top": (224, 232, 248), "bg_bottom": (246, 248, 253), "accent": (64, 92, 154), "card": (251, 253, 255)},
        {"bg_top": (245, 227, 231), "bg_bottom": (252, 245, 247), "accent": (161, 63, 89), "card": (255, 252, 253)},
    ]
    return themes[idx % len(themes)]


def render_one(title: str, rows: list[tuple], index: int, footer_text: str) -> Path:
    theme = theme_for_index(index)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    title_font = ImageFont.truetype(str(FONT_BOLD), 56)
    sub_font = ImageFont.truetype(str(FONT_REGULAR), 24)
    head_font = ImageFont.truetype(str(FONT_BOLD), 30)
    body_font = ImageFont.truetype(str(FONT_REGULAR), 29)
    body_bold = ImageFont.truetype(str(FONT_BOLD), 30)
    rank_font = ImageFont.truetype(str(FONT_BOLD), 34)
    foot_font = ImageFont.truetype(str(FONT_REGULAR), 22)

    dummy = Image.new("RGB", (10, 10), (255, 255, 255))
    measure = ImageDraw.Draw(dummy)

    pretty_titles = [
        "双平台汇总 Top 30",
        "双平台纯爱ONLY Top 30",
        "猫耳纯爱ONLY Top 30",
        "漫播纯爱ONLY Top 30",
        "双平台仅广播剧 Top 30",
        "双平台纯爱仅广播剧 Top 30",
        "猫耳纯爱仅广播剧 Top 30",
        "漫播纯爱仅广播剧 Top 30",
    ]
    display_title = pretty_titles[index] if index < len(pretty_titles) else title

    rank_w = 90
    name_w = 185
    play_w = 165
    count_w = 80
    top3_w = max(
        720,
        int(max((measure.textlength(str(row[4] or ""), font=body_font) for row in rows), default=720)) + 10,
    )

    table_width = 28 + rank_w + 28 + name_w + 35 + play_w + 35 + count_w + 35 + top3_w + 28
    canvas_width = max(BASE_WIDTH, MARGIN_X * 2 + table_width)
    canvas_height = max(BASE_HEIGHT, TABLE_TOP + 90 + len(rows) * ROW_H + FOOTER_H + 100)

    img = Image.new("RGB", (canvas_width, canvas_height), (255, 255, 255))
    draw_gradient_background(img, theme["bg_top"], theme["bg_bottom"])
    draw = ImageDraw.Draw(img)

    draw.rectangle((46, 40, canvas_width - 46, canvas_height - 40), fill=(255, 255, 255), outline=(228, 220, 214), width=2)
    draw.ellipse((canvas_width - 260, 30, canvas_width - 30, 260), fill=theme["bg_top"])
    draw.ellipse((30, canvas_height - 260, 250, canvas_height - 30), fill=theme["bg_top"])

    draw.text((MARGIN_X, TOP), display_title, font=title_font, fill=(36, 36, 36))
    draw.text((MARGIN_X, TOP + 80), "包含会员剧，不含免费剧/100红豆剧", font=sub_font, fill=theme["accent"])

    table_box = (MARGIN_X, TABLE_TOP, canvas_width - MARGIN_X, canvas_height - FOOTER_H - 40)
    draw.rectangle(table_box, fill=theme["card"], outline=(232, 224, 220), width=2)

    col_rank = MARGIN_X + 28
    col_name = col_rank + rank_w + 28
    col_play = col_name + name_w + 35
    col_count = col_play + play_w + 35
    col_top3 = col_count + count_w + 35

    header_y = TABLE_TOP + 20
    for text, x in [("排名", col_rank), ("CV名称", col_name), ("总播放量", col_play), ("主役总数", col_count)]:
        draw.text((x, header_y), text, font=head_font, fill=(70, 70, 70))
    top3_header_bbox = draw.textbbox((0, 0), "TOP3", font=head_font)
    top3_header_w = top3_header_bbox[2] - top3_header_bbox[0]
    top3_header_x = col_top3 + max(0, (top3_w - top3_header_w) / 2)
    draw.text((top3_header_x, header_y), "TOP3", font=head_font, fill=(70, 70, 70))

    start_y = header_y + 56
    name_max_w = name_w
    play_max_w = play_w
    count_max_w = count_w
    top3_max_w = top3_w

    for idx, row in enumerate(rows):
        y = start_y + idx * ROW_H
        rank, cv_name, total_play, work_count, top3 = row

        if idx < 3:
            highlight = [(255, 244, 232), (242, 246, 255), (244, 241, 255)][idx]
            draw.rectangle((MARGIN_X + 16, y - 4, canvas_width - MARGIN_X - 16, y + ROW_H - 8), fill=highlight)
        elif idx % 2 == 1:
            draw.rectangle((MARGIN_X + 16, y - 4, canvas_width - MARGIN_X - 16, y + ROW_H - 8), fill=(250, 248, 246))

        rank_num = int(rank)
        rank_color = theme["accent"] if rank_num > 3 else [(203, 135, 57), (94, 111, 145), (160, 111, 92)][rank_num - 1]
        draw.text((col_rank, y + 8), str(rank), font=rank_font, fill=rank_color)
        draw.text((col_name, y + 8), fit_text(draw, cv_name, body_bold, name_max_w), font=body_bold, fill=(32, 32, 32))
        draw.text((col_play, y + 8), fit_text(draw, total_play, body_font, play_max_w), font=body_font, fill=(54, 54, 54))
        draw.text((col_count, y + 8), fit_text(draw, str(work_count), body_font, count_max_w), font=body_font, fill=(54, 54, 54))
        draw.text((col_top3, y + 8), fit_text(draw, str(top3 or ""), body_font, top3_max_w), font=body_font, fill=(72, 72, 72))

    draw.text((MARGIN_X, canvas_height - 95), footer_text, font=foot_font, fill=(120, 120, 120))

    safe_name = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "_", display_title).strip("_")
    out = OUTPUT_DIR / f"{index+1:02d}_{safe_name}.png"
    img.save(out, quality=95)
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--missevan-date", help="猫耳数据截至日期，例如 2026/4/7")
    parser.add_argument("--manbo-date", help="漫播数据截至日期，例如 2026/4/7")
    args = parser.parse_args()

    outputs = []
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

    for idx, (title, sql) in enumerate(load_queries()):
        rows = fetch_top30(sql)
        outputs.append(render_one(title, rows[:30], idx, footer_text))
    print("Created:")
    for path in outputs:
        print(path)


if __name__ == "__main__":
    main()
