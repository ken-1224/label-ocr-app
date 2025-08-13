import streamlit as st
import easyocr
from PIL import Image
import gspread
from google.oauth2.service_account import Credentials
from datetime import date

# ---------------- 基本設定 ----------------
st.set_page_config(page_title="家電回収・ラベル読取（複数撮影対応）", page_icon="📷")
st.title("📷 家電回収管理アプリ")

# セッション状態（撮影データを保持）
if "images" not in st.session_state:
    st.session_state["images"] = []

# ---------------- Google Sheets 接続 ----------------
# Secrets から認証情報取得
service_account_info = st.secrets["gcp_service_account"]
creds = Credentials.from_service_account_info(
    service_account_info,
    scopes=["https://www.googleapis.com/auth/spreadsheets"]
)
client = gspread.authorize(creds)

# スプレッドシートの指定
SHEET_NAME = "家電回収データ"
sheet = client.open(SHEET_NAME).sheet1

# ---------------- 画像アップロード ----------------
uploaded_files = st.file_uploader("ラベル写真をアップロード（複数可）", type=["jpg", "jpeg", "png"], accept_multiple_files=True)

if uploaded_files:
    for uploaded_file in uploaded_files:
        image = Image.open(uploaded_file)
        st.session_state["images"].append(image)
    st.success(f"{len(uploaded_files)}件の画像を追加しました。")

# ---------------- OCR 処理 ----------------
if st.session_state["images"]:
    reader = easyocr.Reader(["ja", "en"])

    for idx, img in enumerate(st.session_state["images"], start=1):
        st.image(img, caption=f"画像 {idx}", use_column_width=True)
        result = reader.readtext(img)
        text = "\n".join([res[1] for res in result])
        st.text_area(f"OCR結果 {idx}", text, height=100)

# ---------------- 入力フォーム ----------------
st.subheader("回収情報の入力")

回収日 = st.date_input("回収日", value=date.today())
顧客名 = st.text_input("顧客名")
住所 = st.text_input("住所")
電話番号 = st.text_input("電話番号")
家電種類 = st.text_input("家電種類")
メーカー = st.text_input("メーカー")
型番 = st.text_input("型番")
状態 = st.text_input("状態")
備考 = st.text_area("備考")

# ---------------- スプレッドシート保存 ----------------
if st.button("スプレッドシートに保存"):
    try:
        sheet.append_row(
            [
                str(回収日),
                顧客名,
                住所,
                電話番号,
                家電種類,
                メーカー,
                型番,
                状態,
                備考
            ],
            table_range="A1:I1"  # 常にA列〜I列に追記
        )
        st.success("スプレッドシートに反映しました！")
    except Exception as e:
        st.error(f"スプレッドシートに接続できませんでした。詳細: {e}")

# ---------------- 画像リセット ----------------
if st.button("画像をリセット"):
    st.session_state["images"] = []
    st.success("画像をリセットしました。")