import re
import unicodedata
import streamlit as st
import easyocr
import numpy as np
from PIL import Image, ImageOps
import gspread
from google.oauth2.service_account import Credentials
from datetime import date

# ---------------- 基本設定 ----------------
st.set_page_config(page_title="家電回収・ラベル読取（カメラ・複数撮影対応）", page_icon="📷")
st.title("家電回収管理アプリ")

# ---------------- セッション状態初期化 ----------------
if "shots" not in st.session_state:
    st.session_state["shots"] = []   # [{"img": PIL.Image, "ocr": "..."}]
if "cam_key" not in st.session_state:
    st.session_state["cam_key"] = 0  # camera_input を再マウントするためのキー
if "autofill_enabled" not in st.session_state:
    st.session_state["autofill_enabled"] = True  # OCR自動反映のON/OFF

# 入力欄の初期値
for k, v in {
    "pickup_date": date.today(),
    "warehouse_choice": "本社倉庫（東京）",
    "warehouse_other": "",
    "maker": "",
    "model": "",
    "serial": "",
    "year": "",
    "note": "",
}.items():
    st.session_state.setdefault(k, v)

DEFAULTS = {
    "pickup_date": date.today(),
    "warehouse_choice": "本社倉庫（東京）",
    "warehouse_other": "",
    "maker": "",
    "model": "",
    "serial": "",
    "year": "",
    "note": "",
}

def _apply_defaults():
    # 入力欄のデフォルトを適用
    for k, v in DEFAULTS.items():
        st.session_state[k] = v
    # 撮影データもリセット
    st.session_state["shots"] = []
    st.session_state["cam_key"] += 1

# 「次の実行でリセット」フラグが立っていたら、ここで初期化してフラグを下ろす
if st.session_state.get("_reset_pending", False):
    _apply_defaults()
    st.session_state["_reset_pending"] = False

# ---------------- Google シート接続 ----------------
@st.cache_resource
def get_sheet():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    if "gcp_service_account" not in st.secrets:
        raise RuntimeError("Streamlit Secrets に [gcp_service_account] がありません。")
    sa_raw = st.secrets["gcp_service_account"]
    sa_info = {k: (str(v) if v is not None else "") for k, v in sa_raw.items()}
    creds = Credentials.from_service_account_info(sa_info, scopes=scopes)
    gc = gspread.authorize(creds)

    sheet_id = st.secrets.get("SHEET_ID", "").strip()
    if sheet_id:
        sh = gc.open_by_key(sheet_id)
    else:
        sheet_title = st.secrets.get("SHEET_TITLE", "家電回収管理").strip()
        sh = gc.open(sheet_title)
    return sh.sheet1

sheet = None
try:
    sheet = get_sheet()
except Exception as e:
    st.warning("スプレッドシートに接続できませんでした。設定後に再読み込みしてください。")
    st.caption(f"詳細: {e}")

# ---------------- OCR Reader（キャッシュ） ----------------
@st.cache_resource
def get_reader():
    return easyocr.Reader(["ja", "en"], gpu=False)

reader = get_reader()

# ---------------- ユーティリティ：OCRテキスト正規化＆抽出 ----------------
def _normalize(s: str) -> str:
    # 全角→半角、結合文字の統一
    return unicodedata.normalize("NFKC", s)

def _split_lines(text: str):
    return [ln.strip() for ln in _normalize(text).splitlines() if ln.strip()]

# ---- OCRゆらぎ補正（文脈依存の置換は控えめに）----
def _fix_confusions_for_model(tok: str) -> str:
    # モデルは英数混在が前提：数字周辺のO→0、I→1、Z→2、S→5、B→8 を軽めに
    t = tok
    t = re.sub(r'(?<=\d)O|O(?=\d)', '0', t)
    t = re.sub(r'(?<=\d)I|I(?=\d)|l(?=\d)|(?<=\d)l', '1', t)
    t = re.sub(r'(?<=\d)Z|Z(?=\d)', '2', t)
    t = re.sub(r'(?<=\d)S|S(?=\d)', '5', t)
    t = re.sub(r'(?<=\d)B|B(?=\d)', '8', t)
    return t

def _fix_confusions_for_serial(tok: str) -> str:
    # 連続数字・ハイフン比率が高い場合のみ強めに補正
    digits_ratio = sum(c.isdigit() for c in tok) / max(1, len(tok))
    if digits_ratio >= 0.4:
        t = tok
        t = t.replace('O', '0').replace('o', '0')
        t = t.replace('I', '1').replace('l', '1')
        t = t.replace('Z', '2')
        t = t.replace('S', '5')
        t = t.replace('B', '8')
        return t
    return tok

# ---- 年式：和暦/西暦/書式ゆらぎ対応 ----
def _to_western_year(era: str, y: int) -> int:
    base = {"令和": 2018, "平成": 1988, "昭和": 1925,
            "R": 2018, "H": 1988, "S": 1925}.get(era, None)
    return base + y if base else 0

def _parse_year(text: str) -> str:
    norm = _normalize(text)

    # 1) 「製造年/製造年月/年式」優先
    m = re.search(r'(製造年(?:月)?|年式)\s*[:：]?\s*(\d{4})\s*年?', norm)
    if m:
        return m.group(2)

    # 2) 和暦（令和/平成/昭和 + 年）
    m = re.search(r'(令和|平成|昭和)\s*([元\d]{1,2})\s*年', norm)
    if m:
        y = 1 if m.group(2) == "元" else int(m.group(2))
        return str(_to_western_year(m.group(1), y))

    # 3) 省略記号（R/H/S + 数字）
    m = re.search(r'\b([RHS])\s*([1-3]?\d)\b', norm)
    if m:
        return str(_to_western_year(m.group(1), int(m.group(2))))

    # 4) 4桁西暦（2010〜来年）
    this_year = date.today().year
    for y in re.findall(r'\b(19\d{2}|20[0-3]\d)\b', norm):
        yi = int(y)
        if 2010 <= yi <= this_year + 1:
            return y

    # 5) 「2015/07」「2015-07」などから年だけ拾う
    m = re.search(r'\b(19\d{2}|20[0-3]\d)[/\-\.](0?[1-9]|1[0-2])\b', norm)
    if m:
        return m.group(1)

    return ""

# ---- メーカー：別名辞書拡充 ----
_MAKER_TABLE = [
    ("Panasonic", ["PANASONIC", "パナソニック"]),
    ("Hitachi", ["HITACHI", "日立"]),
    ("Toshiba", ["TOSHIBA", "東芝"]),
    ("Sharp", ["SHARP", "シャープ"]),
    ("Sony", ["SONY", "ソニー"]),
    ("Mitsubishi", ["MITSUBISHI", "三菱"]),
    ("Hisense", ["HISENSE", "ハイセンス"]),
    ("Haier", ["HAIER", "ハイアール"]),
    ("LG", ["LG", "エルジー"]),
    ("Samsung", ["SAMSUNG", "サムスン"]),
    ("SANYO", ["SANYO", "三洋"]),
    ("BALMUDA", ["BALMUDA", "バルミューダ"]),
    ("IRIS OHYAMA", ["IRIS OHYAMA", "IRISOHYAMA", "アイリスオーヤマ"]),
    ("DAIKIN", ["DAIKIN", "ダイキン"]),
    ("AQUA", ["AQUA", "アクア"]),
    ("Panasonic National", ["NATIONAL", "ナショナル"]),  # 旧名
]

def _parse_maker(up_text: str) -> str:
    for canon, aliases in _MAKER_TABLE:
        if any(a in up_text for a in [al.upper() for al in aliases]):
            return canon
    return ""

# ---- 製造番号：パターン拡張 ----
_SERIAL_LABEL_PATTERNS = [
    r'(?:S\/?\s*N|SER(?:IAL)?\s*NO\.?|SER\s*NO\.?|SNO\.?)',
    r'(?:製造番号|製番|製造NO\.?|製造No\.?|製造№|製品番号)',
    r'(?:管理番号|本体番号|本体No\.?)',
    r'(?:NO\.|No\.)',
]
def _parse_serial(lines):
    up_lines = [ln.upper() for ln in lines]
    # ラベル付き：行ごとに見る（誤跨ぎ防止）
    for ln in up_lines:
        if any(re.search(p, ln) for p in _SERIAL_LABEL_PATTERNS):
            m = re.search(r'(?:' + "|".join(_SERIAL_LABEL_PATTERNS) + r')\s*[:：\-]?\s*([A-Z0-9\- ]{4,})', ln)
            if m:
                cand = re.sub(r'\s+', '', m.group(1)).strip('-')
                cand = _fix_confusions_for_serial(cand)
                # 連続記号だらけ等を除外
                if 6 <= len(cand) <= 24 and re.search(r'[A-Z]', cand) and re.search(r'\d', cand):
                    return cand
                if 8 <= len(cand) <= 20 and cand.isdigit():
                    return cand
    # フォールバック：ラベル近傍で英数長め
    for ln in up_lines:
        if any(k in ln for k in ["SN", "SERIAL", "NO", "製造", "製番"]):
            toks = re.findall(r'\b[A-Z0-9][A-Z0-9\-]{5,24}\b', ln)
            toks = [t.strip('-') for t in toks]
            for t in toks:
                tt = _fix_confusions_for_serial(t)
                if any(c.isalpha() for c in tt) and any(c.isdigit() for c in tt):
                    return tt
    return ""

# ---- 型番：ラベル優先＋スコアリング ----
# NG語（型番として出たら除外）
_BAD_TOKENS = {
    "WARNING","CAUTION","JAPAN","WASHER","DRYER","REFRIGERATOR","INVERTER",
    "AC","DC","VOLT","HERTZ","HZ","W","KW","MM","CM","KG",
    "PANASONIC","HITACHI","TOSHIBA","SHARP","SONY","MITSUBISHI","HISENSE","HAIER","SAMSUNG","AQUA","SANYO","DAIKIN","IRIS","OHYAMA",
}
def _score_model_token(t: str) -> int:
    s = 0
    if '-' in t: s += 3
    if any(c.isalpha() for c in t) and any(c.isdigit() for c in t): s += 3
    if 3 <= len(t) <= 18: s += 2
    if re.search(r'^\w+-\w+', t): s += 2  # 例：NA-VX、BD-S 等
    return s

def _parse_model(lines, serial: str):
    up_all = " ".join([ln.upper() for ln in lines])

    # 1) ラベル付き抽出（強）
    m = re.search(r'(?:型番|型式|形式|形名|MODEL(?:\s*NO\.?)?|MOD\.?|品番|製品型番|形式名)\s*[:：\-]?\s*([A-Z0-9][A-Z0-9\-_\/\.]{2,24})', up_all)
    if m:
        tok = m.group(1)
        tok = _fix_confusions_for_model(tok)
        if tok not in _BAD_TOKENS:
            return tok

    # 2) 行単位で候補収集→スコア最大を採用
    cands = []
    for ln in [ln.upper() for ln in lines]:
        for t in re.findall(r'\b[A-Z][A-Z0-9\-_\/\.]{2,24}\b', ln):
            if t in _BAD_TOKENS: 
                continue
            if serial and t == serial:
                continue
            if re.fullmatch(r'\d{3,}', t):  # 数字だけ
                continue
            t_fixed = _fix_confusions_for_model(t)
            score = _score_model_token(t_fixed)
            if score >= 4:
                cands.append((score, t_fixed))
    if cands:
        cands.sort(reverse=True)
        return cands[0][1]
    return ""

# ---- まとめ関数：OCRテキスト → フィールド ----
def extract_fields_from_text(text: str) -> dict:
    if not text:
        return {"maker":"", "model":"", "serial":"", "year":""}
    lines = _split_lines(text)
    up_text = _normalize(text).upper()

    maker  = _parse_maker(up_text)
    serial = _parse_serial(lines)
    model  = _parse_model(lines, serial)
    year   = _parse_year(text)

    return {"maker": maker, "model": model, "serial": serial, "year": year}

# ---------------- ① スマホのカメラから複数回撮影 ----------------
st.subheader("① ラベル撮影（必要なだけ複数回）")

img_file = st.camera_input(
    "別面・別ラベルを撮ったら『この写真を追加』を押してください。",
    key=f"cam_{st.session_state['cam_key']}"
)

c1, c2, c3 = st.columns(3)
with c1:
    add_clicked = st.button("この写真を追加", disabled=(img_file is None))
with c2:
    remove_last = st.button("最後の写真を削除", disabled=(len(st.session_state["shots"]) == 0))
with c3:
    clear_all = st.button("全消去", disabled=(len(st.session_state["shots"]) == 0))

if add_clicked and img_file is not None:
    img = Image.open(img_file)
    img = ImageOps.exif_transpose(img)  # スマホの回転補正
    np_img = np.array(img.convert("RGB"))
    with st.spinner("OCR中...（初回はモデル取得で少し時間がかかります）"):
        res = reader.readtext(np_img)  # [(box, text, conf), ...]
    ocr_text = "\n".join([r[1] for r in res])
    st.session_state["shots"].append({"img": img, "ocr": ocr_text})

if remove_last and st.session_state["shots"]:
    st.session_state["shots"].pop()
    st.session_state["cam_key"] += 1
    st.rerun()

if clear_all and st.session_state["shots"]:
    st.session_state["shots"].clear()
    st.session_state["cam_key"] += 1
    st.rerun()

st.write(f"現在の撮影枚数：**{len(st.session_state['shots'])}** 枚")
if st.session_state["shots"]:
    for idx, shot in enumerate(st.session_state["shots"], start=1):
        st.image(shot["img"], caption=f"撮影 {idx}", use_container_width=True)
        st.text_area(f"OCR結果 {idx}", shot["ocr"], height=100, key=f"ocr_{idx}")

# ---------------- ②-0 OCR自動抽出（入力欄の前に実行 = 重要） ----------------
st.subheader("②-0 OCR 自動抽出（任意）")
st.checkbox("OCR 自動入力を有効にする（空欄のみ自動反映）", key="autofill_enabled")

aggregated_text = "\n---\n".join([s["ocr"] for s in st.session_state["shots"]]) if st.session_state["shots"] else ""
suggest = extract_fields_from_text(aggregated_text) if aggregated_text else {"maker": "", "model": "", "serial": "", "year": ""}

# 空欄のみ自動反映（ウィジェット描画前に state を更新する）
if st.session_state["autofill_enabled"] and aggregated_text:
    for field in ("maker", "model", "serial", "year"):
        if not st.session_state[field] and suggest.get(field):
            st.session_state[field] = suggest[field]

with st.expander("OCR統合テキスト＆抽出候補の確認", expanded=False):
    st.text_area("統合OCRテキスト", aggregated_text, height=180)
    st.write("抽出候補（上書きしたい場合は下のボタンで反映できます）")
    st.json(suggest)
    if st.button("候補で入力欄を上書きする"):
        for field in ("maker", "model", "serial", "year"):
            st.session_state[field] = suggest.get(field, "")
        st.success("候補で入力欄を更新しました。")
        st.rerun()

# ---------------- ② 回収情報の入力（手入力） ----------------
st.subheader("② 回収情報の入力（手入力）")

# 回収日（値は st.session_state["pickup_date"] に自動で入る）
st.date_input("回収日", key="pickup_date")


WAREHOUSES = [
    "本社倉庫（東京）", "東日本センター（仙台）",
    "西日本センター（大阪）", "九州センター（福岡）",
    "その他（手入力）",
]
st.selectbox(
    "倉庫を選択",
    options=WAREHOUSES,
    index=WAREHOUSES.index(st.session_state["warehouse_choice"]) if st.session_state["warehouse_choice"] in WAREHOUSES else 0,
    key="warehouse_choice",
)
if st.session_state["warehouse_choice"] == "その他（手入力）":
    st.text_input("倉庫名を入力", key="warehouse_other")

warehouse = (
    st.session_state["warehouse_other"]
    if st.session_state["warehouse_choice"] == "その他（手入力）"
    else st.session_state["warehouse_choice"]
)

# ここに自動反映済みの値が入ってくる（必要に応じて手修正が可能）
st.text_input("メーカー", key="maker")
st.text_input("型番", key="model")
st.text_input("製造番号", key="serial")
st.text_input("年式", key="year")
st.text_area("備考", key="note")

# ---------------- ③ スプレッドシート保存（A1:G1固定追記） ----------------
st.subheader("③ スプレッドシート保存")
can_save = sheet is not None and st.session_state["pickup_date"] and (warehouse or st.session_state["maker"] or st.session_state["model"])
if st.button("スプレッドシートに保存", disabled=not can_save):
    try:
        payload = [
            str(st.session_state["pickup_date"]),  # A: 回収日
            warehouse,                              # B: 回収倉庫
            st.session_state["maker"],              # C: メーカー
            st.session_state["model"],              # D: 型番
            st.session_state["serial"],             # E: 製造番号
            st.session_state["year"],               # F: 年式
            st.session_state["note"],               # G: 備考
        ]
        sheet.append_row(
            payload,
            value_input_option="USER_ENTERED",
            table_range="A1:G1",
        )
        st.success("スプレッドシートに反映しました！")

        # ★ここを置き換え：次の実行サイクルで初期化するフラグだけ立てる
        st.session_state["_reset_pending"] = True
        st.rerun()
    except Exception as e:
        st.error(f"スプレッドシートに接続/書き込みできませんでした。詳細: {e}")

# ---------------- 撮影データだけリセット ----------------
if st.button("撮影データだけリセット"):
    st.session_state["shots"].clear()
    st.session_state["cam_key"] += 1
    st.success("撮影データをリセットしました。")