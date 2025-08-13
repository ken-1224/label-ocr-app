import re
import json
import numpy as np
import streamlit as st
from PIL import Image, ImageOps
import easyocr
import gspread
import cv2
from google.oauth2.service_account import Credentials
from datetime import date

# ---------------- 基本設定 ----------------
st.set_page_config(page_title="家電回収・ラベル読取（複数撮影対応）", page_icon="📷")
st.title("📷 家電回収管理アプリ")

# セッション状態（撮影データを保持）
if "shots" not in st.session_state:
    st.session_state.shots = []
if "agg_text" not in st.session_state:
    st.session_state.agg_text = ""
if "cam_key" not in st.session_state:          # ← 追加
    st.session_state.cam_key = 0

# ---------------- OCR 初期化 ----------------
@st.cache_resource
def get_reader():
    # 日本語・英語モデル（初回は自動DLで少し時間がかかる）
    return easyocr.Reader(['ja', 'en'], gpu=False)

reader = get_reader()

# ---------------- Google シート接続 ----------------
@st.cache_resource
def get_sheet():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"  # ← 追加
    ]

    creds = None
    if "gcp_service_account" in st.secrets:
        sa_raw = st.secrets["gcp_service_account"]
        sa_info = {k: (str(v) if v is not None else "") for k, v in sa_raw.items()}
        creds = Credentials.from_service_account_info(sa_info, scopes=scopes)
    else:
        creds = Credentials.from_service_account_file("service_account.json", scopes=scopes)

    gc = gspread.authorize(creds)
    sh = gc.open("家電回収管理")  # ← タイトルで開く
    return sh.sheet1

sheet = None
try:
    sheet = get_sheet()
except Exception as e:
    st.warning("スプレッドシートに接続できませんでした。設定後に再読み込みしてください。")
    st.caption(f"詳細: {e}")
    # 共有ヒント：サービスアカウントのメールをシートに「編集者」で共有
    try:
        if "gcp_service_account" in st.secrets and "client_email" in st.secrets["gcp_service_account"]:
            sa_mail = st.secrets["gcp_service_account"]["client_email"]
            st.caption(f"ヒント: スプレッドシートを次のメールに共有してください → {sa_mail}")
    except Exception:
        pass

# 任意：簡易パスワード（CloudでURL共有時に推奨）
PWD = st.secrets["APP_PASSWORD"] if "APP_PASSWORD" in st.secrets else ""
if PWD:
    input_pwd = st.text_input("Password", type="password")
    if input_pwd != PWD:
        st.stop()

# ---------------- 前処理（必要になれば強化） ----------------
def preprocess(img_pil: Image.Image) -> Image.Image:
    img = np.array(img_pil)
    # リサイズ（最長辺1600px）
    h, w = img.shape[:2]
    max_side = max(h, w)
    if max_side > 1600:
        scale = 1600 / max_side
        img = cv2.resize(img, (int(w*scale), int(h*scale)), interpolation=cv2.INTER_AREA)

    g = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    g = cv2.GaussianBlur(g, (3,3), 0)
    g = cv2.equalizeHist(g)
    _, bw = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return Image.fromarray(bw)

# ---------------- 粗抽出（統合テキストから） ----------------
def rough_extract(text: str) -> dict:
    # 年式（ざっくり4桁の年）
    m_year = re.search(r"\b(19\d{2}|20[0-3]\d)\b", text)

    # 製造番号（S/N, Serial, 製造番号 の後ろ）
    m_serial = re.search(r"(?:S\/N|Serial|製造番号)\s*[:：\-]?\s*([A-Z0-9\-]+)", text, re.IGNORECASE)

    # 型番（英数記号のまとまりをざっくり）
    m_model = re.search(r"\b([A-Z0-9]{2,}(?:-?[A-Z0-9]{2,}){1,})\b", text, re.IGNORECASE)

    makers_en = ["Panasonic","Hitachi","Toshiba","Sharp","Sony","Mitsubishi","Hisense","Haier","LG","Samsung","SANYO","BALMUDA","IRIS OHYAMA","DAIKIN","AQUA"]
    makers_ja = ["パナソニック","日立","東芝","シャープ","ソニー","三菱","ハイセンス","ハイアール","エルジー","サムスン","三洋","バルミューダ","アイリスオーヤマ","ダイキン","アクア"]

    maker = ""
    # 英語は単語境界、和文は部分一致
    for mk in makers_en:
        if re.search(rf"\b{re.escape(mk)}\b", text, re.IGNORECASE):
            maker = mk
            break
    if not maker:
        for mk in makers_ja:
            if mk in text:
                maker = mk
                break

    return {
        "model":  m_model.group(1)  if m_model  else "",
        "serial": m_serial.group(1) if m_serial else "",
        "year":   m_year.group(1)   if m_year   else "",
        "maker":  maker,
    }

# ---------------- ① 回収日時 ----------------
st.subheader("① 回収日")
pickup_date = st.date_input("回収日", value=date.today())

# ---------------- ② 回収倉庫（営業所） ----------------
st.subheader("② 回収倉庫（営業所）")
WAREHOUSES = [
    "本社倉庫（東京）", "東日本センター（仙台）",
    "西日本センター（大阪）", "九州センター（福岡）",
    "その他（手入力）",
]
wh_choice = st.selectbox("倉庫を選択", options=WAREHOUSES, index=0)
warehouse = wh_choice if wh_choice != "その他（手入力）" else st.text_input("倉庫名を入力", value="")

# ---------------- ③ 複数回撮影して追加 ----------------
st.subheader("③ ラベル撮影（必要なだけ複数回）")
img_file = st.camera_input(
    "別面・別シールを撮影 → 『この写真を追加』を押してください。",
    key=f"cam_{st.session_state.cam_key}"      # ← 追加・重要
)

col_b1, col_b2, col_b3 = st.columns(3)
with col_b1:
    add_clicked = st.button("この写真を追加", disabled=(img_file is None))
with col_b2:
    remove_last = st.button("最後の写真を削除", disabled=(len(st.session_state.shots) == 0))
with col_b3:
    clear_all = st.button("全消去", type="secondary", disabled=(len(st.session_state.shots) == 0))

# 追加
if add_clicked and img_file is not None:
    img = Image.open(img_file)
    img = ImageOps.exif_transpose(img)
    img = preprocess(img)
    # easyocr は ndarray(RGB) を推奨
    np_img = np.array(img.convert("RGB"))
    with st.spinner("OCR中...（初回はモデルDLで少し待ちます）"):
        lines = reader.readtext(np_img, detail=0)
    ocr_text = "\n".join(lines)
    st.session_state.shots.append({"img_bytes": img_file.getvalue(), "ocr_text": ocr_text})

# 削除/全消去
if remove_last and st.session_state.shots:
    st.session_state.shots.pop()
    st.session_state.cam_key += 1              # ← 追加（ウィジェット再マウント）
    st.rerun()                                  # ← 追加

if clear_all and st.session_state.shots:
    st.session_state.shots.clear()
    st.session_state.agg_text = ""
    st.session_state.cam_key += 1              # ← 追加
    st.rerun()                                  # ← 追加

# 統合テキストを再構築
if st.session_state.shots:
    parts = [f"[SHOT {i}]\n{s['ocr_text']}" for i, s in enumerate(st.session_state.shots, start=1)]
    st.session_state.agg_text = "\n---\n".join(parts)
else:
    st.session_state.agg_text = ""

# 現在の撮影状況
st.write(f"現在の撮影枚数：**{len(st.session_state.shots)}** 枚")
if st.session_state.agg_text:
    with st.expander("統合OCRテキスト（確認用）", expanded=False):
        txt = st.session_state.agg_text
        st.text(txt[:2000] + ("..." if len(txt) > 2000 else ""))

# 進捗（どれだけ埋まったか）
def mark(v: str) -> str: return "✅" if v.strip() else "✗"
pre_fields = rough_extract(st.session_state.agg_text) if st.session_state.agg_text else {"model":"", "serial":"", "year":"", "maker":""}
st.caption(f"抽出進捗：型番 {mark(pre_fields['model'])} / 製造番号 {mark(pre_fields['serial'])} / 年式 {mark(pre_fields['year'])} / メーカー {mark(pre_fields['maker'])}")

# ---------------- ④ 統合OCR → 最終修正 → 保存 ----------------
if st.session_state.agg_text:
    st.subheader("④ 抽出候補（必要なら修正してください）")
    c3, c4 = st.columns(2)
    with c3:
        model  = st.text_input("型番 Model", value=pre_fields.get("model", ""))
        serial = st.text_input("製造番号 Serial", value=pre_fields.get("serial", ""))
    with c4:
        year   = st.text_input("年式 Year", value=pre_fields.get("year", ""))
        maker  = st.text_input("メーカー Maker", value=pre_fields.get("maker", ""))

    note = st.text_area("補足（読みにくい箇所、現場メモなど）", value="")
    src  = st.text_input("Source（任意：現場/倉庫/担当者名など）", value="")

    ready_date = bool(pickup_date)
    ready_wh = bool(warehouse.strip())
    ready_item = bool(model or serial or maker)  # いずれか1つ以上
    ready_sheet = sheet is not None
    can_save = ready_date and ready_wh and ready_item and ready_sheet

    st.caption("※保存条件：回収日・倉庫が選択済み ＋ （型番 or 製造番号 or メーカーのいずれか）")

    raw_text_save = ""   # RawTextを空で保存

    if st.button("スプレッドシートに登録", disabled=not can_save):
        if not ready_date:
            st.error("回収日を選択してください。")
        elif not ready_wh:
            st.error("回収倉庫（営業所）を入力してください。")
        elif not ready_item:
            st.error("型番・製造番号・メーカーのどれか1つ以上を入力してください。")
        elif sheet is None:
            st.error("スプレッドシート接続エラー。設定を見直してください。")
        else:
            try:
                # 保存列：PickupDateTime, Warehouse, Model, Serial, Year, Maker, Note, RawText, Source
                sheet.append_row([
                    pickup_date.isoformat(),
                    warehouse, model, serial, year, maker, note, raw_text_save, src
                ], value_input_option="USER_ENTERED")
                st.success("登録しました！")
                # 次の製品に備えてクリア
                st.session_state.shots.clear()
                st.session_state.agg_text = ""
                st.session_state.cam_key += 1
                st.rerun()
            except Exception as e:
                st.error(f"登録に失敗しました: {e}")
else:
    st.info("まずは写真を1枚以上『追加』してください。")

st.caption("Tips: 明るい場所で、ラベルを画面いっぱい＆真正面で撮影すると認識率が上がります。HTTPSのURL（Streamlit Cloud）ならスマホでもカメラが動きます。")