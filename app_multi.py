import streamlit as st
import easyocr
import numpy as np
from PIL import Image, ImageOps
import gspread
from google.oauth2.service_account import Credentials
from datetime import date

# ---------------- 基本設定 ----------------
st.set_page_config(page_title="家電回収・ラベル読取（カメラ・複数撮影対応）", page_icon="📷")
st.title("📷 家電回収管理アプリ（カメラ対応）")

# ---------------- セッション状態初期化 ----------------
if "shots" not in st.session_state:
    st.session_state["shots"] = []   # [{"img": PIL.Image, "ocr": "..."}]
if "cam_key" not in st.session_state:
    st.session_state["cam_key"] = 0  # camera_input を再マウントするためのキー

# 入力欄の状態（保存後にここを空にしてから rerun すれば全部リセットできる）
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
        st.image(shot["img"], caption=f"撮影 {idx}", use_container_width=True)  # ← 警告回避
        st.text_area(f"OCR結果 {idx}", shot["ocr"], height=100, key=f"ocr_{idx}")

st.caption("Tips: HTTPSの本番URL（Streamlit Cloud）でカメラが動作します。明るく、ラベルを画面いっぱい・真正面で撮影すると精度UP。")

# ---------------- ② 回収情報の入力（手入力） ----------------
st.subheader("② 回収情報の入力（手入力）")
st.session_state["pickup_date"] = st.date_input("回収日", value=st.session_state["pickup_date"], key="pickup_date")

st.subheader("② 回収倉庫（営業所）")
WAREHOUSES = [
    "本社倉庫（東京）", "東日本センター（仙台）",
    "西日本センター（大阪）", "九州センター（福岡）",
    "その他（手入力）",
]
st.selectbox("倉庫を選択", options=WAREHOUSES, index=WAREHOUSES.index(st.session_state["warehouse_choice"]) if st.session_state["warehouse_choice"] in WAREHOUSES else 0, key="warehouse_choice")
if st.session_state["warehouse_choice"] == "その他（手入力）":
    st.text_input("倉庫名を入力", key="warehouse_other")
warehouse = st.session_state["warehouse_other"] if st.session_state["warehouse_choice"] == "その他（手入力）" else st.session_state["warehouse_choice"]

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

        # ---- ここがポイント：入力欄と撮影データをリセットして次の製品へ ----
        st.session_state["shots"].clear()
        st.session_state["cam_key"] += 1

        # 入力欄の値を初期化
        st.session_state["pickup_date"] = date.today()
        st.session_state["warehouse_choice"] = "本社倉庫（東京）"
        st.session_state["warehouse_other"] = ""
        st.session_state["maker"] = ""
        st.session_state["model"] = ""
        st.session_state["serial"] = ""
        st.session_state["year"] = ""
        st.session_state["note"] = ""

        st.rerun()
    except Exception as e:
        st.error(f"スプレッドシートに接続/書き込みできませんでした。詳細: {e}")

# ---------------- 撮影データだけリセット ----------------
if st.button("撮影データだけリセット"):
    st.session_state["shots"].clear()
    st.session_state["cam_key"] += 1
    st.success("撮影データをリセットしました。")