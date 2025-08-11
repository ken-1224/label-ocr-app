import re
import json
import numpy as np
import streamlit as st
from PIL import Image
import easyocr
import gspread
from google.oauth2.service_account import Credentials
from datetime import date, time

# ---------------- 基本設定 ----------------
st.set_page_config(page_title="家電回収・ラベル読取（複数撮影対応）", page_icon="📷")
st.title("📷 家電回収管理アプリ")

# セッション状態（撮影データを保持）
if "shots" not in st.session_state:
    st.session_state.shots = []   # [{"img_bytes": bytes, "ocr_text": "..."}]
if "agg_text" not in st.session_state:
    st.session_state.agg_text = ""

# ---------------- OCR 初期化 ----------------
@st.cache_resource
def get_reader():
    # 日本語・英語モデル（初回は自動DLで少し時間がかかる）
    return easyocr.Reader(['ja', 'en'], gpu=False)

reader = get_reader()

# ---------------- Google シート接続 ----------------
@st.cache_resource
def get_sheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]

    # A) Streamlit Cloud では Secrets から読む
    if "gcp_service_account" in st.secrets:
        sa_info = st.secrets["gcp_service_account"]
        creds = Credentials.from_service_account_info(sa_info, scopes=scopes)
    else:
        # B) ローカルでは service_account.json を使う
        creds = Credentials.from_service_account_file("service_account.json", scopes=scopes)

    gc = gspread.authorize(creds)
    # ←あなたのシート名に合わせてください（既定：家電回収管理）
    sh = gc.open("家電回収管理")
    return sh.sheet1

sheet = None
try:
    sheet = get_sheet()
except Exception as e:
    st.warning("スプレッドシートに接続できませんでした。設定後に再読み込みしてください。")
    st.caption(f"詳細: {e}")

# 任意：簡易パスワード（CloudでURL共有時に推奨）
PWD = st.secrets.get("APP_PASSWORD", "")
if PWD:
    input_pwd = st.text_input("Password", type="password")
    if input_pwd != PWD:
        st.stop()

# ---------------- 前処理（必要になれば強化） ----------------
def preprocess(img_pil: Image.Image) -> Image.Image:
    # 読みにくい場合は次行を有効化（グレースケール）
    # return img_pil.convert("L")
    return img_pil

# ---------------- 粗抽出（統合テキストから） ----------------
def rough_extract(text: str) -> dict:
    # 年式（ざっくり4桁の年）
    m_year = re.search(r"\b(19\d{2}|20[0-3]\d)\b", text)

    # 製造番号（S/N, Serial, 製造番号 の後ろ）
    m_serial = re.search(r"(?:S\/N|Serial|製造番号)\s*[:：\-]?\s*([A-Z0-9\-]+)", text, re.IGNORECASE)

    # 型番（英数記号のまとまりをざっくり）
    m_model = re.search(r"\b([A-Z0-9]{2,}(?:-?[A-Z0-9]{2,}){1,})\b", text, re.IGNORECASE)

    makers = [
        "Panasonic","パナソニック","Hitachi","日立","Toshiba","東芝","Sharp","シャープ",
        "Sony","ソニー","Mitsubishi","三菱","Hisense","ハイセンス","Haier","ハイアール",
        "LG","エルジー","Samsung","サムスン","SANYO","三洋","BALMUDA","バルミューダ",
        "IRIS OHYAMA","アイリスオーヤマ","DAIKIN","ダイキン","AQUA","アクア",
    ]
    maker = ""
    for mk in makers:
        if re.search(rf"\b{re.escape(mk)}\b", text, re.IGNORECASE):
            maker = mk
            break

    return {
        "model":  m_model.group(1)  if m_model  else "",
        "serial": m_serial.group(1) if m_serial else "",
        "year":   m_year.group(1)   if m_year   else "",
        "maker":  maker,
    }

# ---------------- ① 回収日時 ----------------
st.subheader("① 回収日時")
c1, c2 = st.columns(2)
with c1:
    pickup_date = st.date_input("回収日", value=date.today())
with c2:
    pickup_time = st.time_input("回収時刻", value=time(10, 0))
pickup_dt_str = f"{pickup_date.isoformat()} {pickup_time.strftime('%H:%M')}"

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
img_file = st.camera_input("別面・別シールを撮影 → 『この写真を追加』を押してください。")

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
    img = preprocess(img)
    with st.spinner("OCR中...（初回はモデルDLで少し待ちます）"):
        lines = reader.readtext(np.array(img), detail=0)
    ocr_text = "\n".join(lines)
    st.session_state.shots.append({"img_bytes": img_file.getvalue(), "ocr_text": ocr_text})

# 削除/全消去
if remove_last and st.session_state.shots:
    st.session_state.shots.pop()
if clear_all and st.session_state.shots:
    st.session_state.shots.clear()

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

    ready_datetime = bool(pickup_date and pickup_time)
    ready_wh = bool(warehouse.strip())
    ready_item = bool(model or serial or maker)  # いずれか1つ以上
    ready_sheet = sheet is not None
    can_save = ready_datetime and ready_wh and ready_item and ready_sheet

    st.caption("※保存条件：回収日時・倉庫が選択済み ＋ （型番 or 製造番号 or メーカーのいずれか）")

    raw_text_save = st.session_state.agg_text

    if st.button("スプレッドシートに登録", disabled=not can_save):
        if not ready_datetime:
            st.error("回収日時を選択してください。")
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
                    f"{pickup_date.isoformat()} {pickup_time.strftime('%H:%M')}",
                    warehouse, model, serial, year, maker, note, raw_text_save, src
                ])
                st.success("登録しました！")
                # 次の製品に備えてクリア
                st.session_state.shots.clear()
                st.session_state.agg_text = ""
            except Exception as e:
                st.error(f"登録に失敗しました: {e}")
else:
    st.info("まずは写真を1枚以上『追加』してください。")

st.caption("Tips: 明るい場所で、ラベルを画面いっぱい＆真正面で撮影すると認識率が上がります。HTTPSのURL（Streamlit Cloud）ならスマホでもカメラが動きます。")
