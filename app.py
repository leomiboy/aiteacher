# -*- coding: utf-8 -*-
"""
數學解題助教 - Streamlit 版
部署目標：Streamlit Cloud
"""

import time
import tempfile
import os
import re
import streamlit as st
import streamlit.components.v1 as components
from google import genai
from google.genai import types
from google.oauth2.service_account import Credentials
import gspread


# ============================================================
# 常數
# ============================================================
SS_ID = '1G0hBHKoGRP9009a1ga601ToEoMhn8X8VqXAzcMA6K9Y'

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ============================================================
# Google 服務初始化（從 Streamlit secrets 取憑證）
# ============================================================

@st.cache_resource
def get_gspread_client():
    creds = Credentials.from_service_account_info(
        st.secrets["gcp_service_account"], scopes=SCOPES
    )
    return gspread.authorize(creds)


@st.cache_resource
def get_spreadsheet():
    gc = get_gspread_client()
    return gc.open_by_key(SS_ID)

# ============================================================
# Sheets 操作
# ============================================================

def login_user(account: str, password: str):
    """驗證帳號密碼，回傳帳號資訊 dict 或 None"""
    sh = get_spreadsheet()
    ws = sh.worksheet("accounts")
    rows = ws.get_all_values()
    for row in rows[1:]:
        if len(row) >= 5 and str(row[0]) == account and str(row[1]) == password:
            return {
                "account":   row[0],
                "tutorName": row[2],
                "keyName":   row[3],
                "modelName": row[4],
            }
    return None

def get_api_key(key_name: str) -> str | None:
    sh = get_spreadsheet()
    ws = sh.worksheet("api_keys")
    rows = ws.get_all_values()
    for row in rows[1:]:
        if len(row) >= 2 and str(row[0]) == key_name:
            return str(row[1])
    return None

def write_record(account, tutor_name, key_name, model_name,
                 answer, has_answer, ai_result, image_url):
    sh = get_spreadsheet()
    ws = sh.worksheet("records")
    from datetime import datetime
    import pytz
    now = datetime.now(pytz.timezone("Asia/Taipei")).strftime("%Y-%m-%d %H:%M:%S")
    ws.append_row([
        now, account, tutor_name, key_name, model_name,
        answer.strip() if has_answer else "",
        "FALSE" if has_answer else "TRUE",
        ai_result.get("scaffold1", ""),
        ai_result.get("scaffold2", ""),
        ai_result.get("scaffold3", ""),
        ai_result.get("explanation", ""),
        image_url,
    ])


# ============================================================
# AI Prompts
# ============================================================

def build_explanation_prompt(has_answer: bool, answer: str) -> str:
    answer_line = (
        f"本題正確答案為：{answer.strip()}，請以此答案為基礎撰寫，不需要自行推導。"
        if has_answer else "請自行推導本題答案。"
    )
    return f"""你是一位資深的國中數學老師，請針對這道數學題目提供完整的解析。
你必須使用繁體中文回答。

【已知條件】
- 題目圖片：（附圖）
- {answer_line}

## 解析格式
請依照以下結構撰寫：

📌 **題目考點**：（這題考的知識點是什麼）
💡 **解題思路**：（用什麼方法來解題）
📝 **解題過程**：（具體的計算步驟，要算出最終答案）
✅ **答案**：（最終答案）

## 數學符號
- 所有數學符號、公式一律使用 LaTeX 語法
- 行內公式用 $...$ 包裹，獨立公式用 $$...$$ 包裹
- 分式必須用 $\\frac{{分子}}{{分母}}$，不得用 / 代替

## 重要限制
- 解析控制在 300 字以內
- 只能使用國中課程範圍內的數學知識"""


def build_scaffold_prompt(has_answer: bool, answer: str) -> str:
    answer_line = (
        f"本題正確答案為：{answer.strip()}，請以此答案為基礎撰寫鷹架，不需要自行推導答案。"
        if has_answer else "請自行推導本題答案作為鷹架依據。"
    )
    sc3_rule = (
        "最後一個步驟以「請你完成計算：$算式 = ?$」的方式呈現，不得寫出最終數字答案或選項。"
        if has_answer else "按步驟完整列出，最後直接寫出推導的答案。"
    )
    return f"""你是一位資深的國中數學老師，請針對這道數學題目產出三個層次的學習鷹架說明。
你必須使用繁體中文回答。

【已知條件】
- 題目圖片：（附圖）
- {answer_line}

## 輸出格式（嚴格遵守，不得有任何額外文字）

【第1層鷹架】
（從題目文字或圖形指出需要用到哪些知識點，直接列出名稱，不使用提問句。）

【第2層鷹架】
（針對第1層知識點，說明公式中每個符號對應題目中哪個已知條件，只做對應說明，不代入計算。）

【第3層鷹架】
（按步驟列出計算過程，包含代入數值後的每一步算式。{sc3_rule}）

## 數學符號
- 所有數學符號、公式一律使用 LaTeX 語法
- 行內公式用 $...$ 包裹，獨立公式用 $$...$$ 包裹
- 分式必須用 $\\frac{{分子}}{{分母}}$，不得用 / 代替
- 不要用純文字寫數學符號

## 重要限制
- 每層鷹架不超過 200 字
- 只能使用國中課程範圍內的數學知識
- 嚴格遵守輸出格式"""

# ============================================================
# AI 呼叫（對應 math_scaffold_batch 的 call_ai_with_retry）
# ============================================================

def call_with_retry(client, model_name: str, prompt: str, image_part, max_retries: int = 3) -> str:
    last_error: Exception = RuntimeError("未知錯誤")
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=[prompt, image_part],
            )
            return response.text
        except Exception as e:
            last_error = e
            if attempt < max_retries - 1:
                time.sleep(3 * (attempt + 1))
    raise last_error


def parse_scaffold(text: str) -> dict:
    def get(pattern):
        m = re.search(pattern, text, re.DOTALL)
        return m.group(1).strip() if m else "（解析失敗，請重試）"
    return {
        "scaffold1": get(r"【第1層鷹架】(.*?)(?=【第2層鷹架】)"),
        "scaffold2": get(r"【第2層鷹架】(.*?)(?=【第3層鷹架】)"),
        "scaffold3": get(r"【第3層鷹架】(.*)$"),
    }



# ============================================================
# UI 工具
# ============================================================

def render_math(text: str):
    """在 Streamlit 裡渲染含 LaTeX 的 markdown"""
    st.markdown(text)


def show_live_timer(label: str = "⏱️ 計時中"):
    """在頁面上嵌入一個 JS 碼表，按下按鈕後即時跳動。
    回傳一個 placeholder，供完成後替換為最終時間。"""
    timer_placeholder = st.empty()
    timer_placeholder.components_html = components.html(
        f"""
        <div id="timer-box" style="
            font-family: 'Courier New', monospace;
            font-size: 1.1rem;
            font-weight: bold;
            color: #ff6b35;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            border: 2px solid #ff6b35;
            border-radius: 8px;
            padding: 6px 16px;
            display: flex;
            flex-direction: row;
            align-items: center;
            gap: 8px;
            width: fit-content;
            box-shadow: 0 0 12px rgba(255,107,53,0.35);
            animation: pulse-border 1.5s ease-in-out infinite;
        ">
            <span>⏱️</span>
            <span>{label}：</span>
            <span id="elapsed" style="min-width:4ch;">0.0 秒</span>
        </div>
        <style>
            body {{ margin: 0; padding: 0; }}
            @keyframes pulse-border {{
                0%, 100% {{ box-shadow: 0 0 12px rgba(255,107,53,0.35); }}
                50%        {{ box-shadow: 0 0 24px rgba(255,107,53,0.70); }}
            }}
        </style>
        <script>
            (function() {{
                var start = Date.now();
                var el = document.getElementById('elapsed');
                var iv = setInterval(function() {{
                    if (!el) {{ clearInterval(iv); return; }}
                    var sec = ((Date.now() - start) / 1000).toFixed(1);
                    el.textContent = sec + ' 秒';
                }}, 100);
            }})();
        </script>
        """,
        height=52,
    )
    return timer_placeholder


def show_final_time(placeholder, label: str, elapsed: float):
    """把碼表位置替換成最終耗時顯示"""
    placeholder.metric(
        label=label,
        value=f"{elapsed:.1f} 秒",
    )


# ============================================================
# 頁面：登入
# ============================================================

def page_login():
    st.title("🧮 數學解題助教")
    st.subheader("請登入")
    with st.form("login_form"):
        account  = st.text_input("帳號")
        password = st.text_input("密碼", type="password")
        submitted = st.form_submit_button("登入")

    if submitted:
        with st.spinner("驗證中..."):
            user = login_user(account, password)
        if user:
            st.session_state.user = user
            st.session_state.page = "main"
            st.rerun()
        else:
            st.error("帳號或密碼錯誤，請重試。")

# ============================================================
# 頁面：主功能
# ============================================================

def page_main():
    user = st.session_state.user

    # ── Sidebar ─────────────────────────────────────
    with st.sidebar:
        st.markdown(f"### 👤 {user['tutorName']}")
        st.caption(f"模型：`{user['modelName']}`")
        st.divider()
        if st.button("🚪 登出"):
            for k in list(st.session_state.keys()):
                del st.session_state[k]
            st.rerun()

    st.title("🧮 數學解題助教")

    # ── 圖片上傳 ─────────────────────────────────────
    st.subheader("📷 上傳題目圖片")
    upload_img = st.file_uploader(
        "請用相機拍照後，匯入圖片檔案（JPG / PNG）",
        type=["jpg", "jpeg", "png"],
    )

    img_bytes = None
    if upload_img:
        img_bytes = upload_img.getvalue()
        # 換圖偵測：hash 不同就清空舊結果
        import hashlib
        img_hash = hashlib.md5(img_bytes).hexdigest()
        if st.session_state.get("img_hash") != img_hash:
            st.session_state["img_hash"] = img_hash
            st.session_state.pop("explanation", None)
            st.session_state.pop("scaffold", None)

    if img_bytes:
        st.image(img_bytes, caption="題目圖片", use_container_width=True)

    # ── 答案輸入 ─────────────────────────────────────
    st.subheader("✏️ 已知答案（選填）")
    answer_input = st.text_input(
        "若已知正確答案，請填入（留空表示讓 AI 自行推導）",
        placeholder="例：B 或 12"
    )
    has_answer = bool(answer_input and answer_input.strip())

    # ── 分析按鈕 ─────────────────────────────────────
    col_btn1, col_btn2 = st.columns(2)
    with col_btn1:
        if st.button("📖 產出解析", type="primary", disabled=not img_bytes, use_container_width=True):
            _do_explanation(user, img_bytes, has_answer, answer_input)
    with col_btn2:
        if st.button("🏗️ 產出鷹架", type="secondary", disabled=not img_bytes, use_container_width=True):
            _do_scaffold(user, img_bytes, has_answer, answer_input)

    # ── 顯示保留的結果 ─────────────────────────────────────
    if "explanation" in st.session_state:
        st.divider()
        st.subheader("📖 解析")
        render_math(st.session_state["explanation"])

    if "scaffold" in st.session_state:
        sc = st.session_state["scaffold"]
        st.divider()
        st.subheader("🏗️ 學習鷹架")
        st.caption("建議先試著自己解題，需要提示再展開各層鷹架。")
        with st.expander("💡 第1層鷹架（知識點辨識）"):
            render_math(sc.get("scaffold1", ""))
        with st.expander("🔧 第2層鷹架（概念橋接）"):
            render_math(sc.get("scaffold2", ""))
        with st.expander("🧮 第3層鷹架（操作執行）"):
            render_math(sc.get("scaffold3", ""))


def _get_api_key_or_error(user: dict) -> str | None:
    api_key = get_api_key(user["keyName"])
    if not api_key:
        st.error("找不到 API Key，請確認 api_keys 分頁設定。")
    return api_key


def _do_explanation(user: dict, img_bytes: bytes, has_answer: bool, answer: str):
    """只產出解析"""
    api_key = _get_api_key_or_error(user)
    if not api_key:
        return

    client = genai.Client(api_key=api_key)

    # ── 顯示即時碼表 ──
    timer_ph = show_live_timer("📖 解析計時")

    with st.status("⏳ 產出解析中...", expanded=True) as status:
        t0 = time.time()
        try:
            st.write("📂 [1/4] 建立暫存檔案...")
            with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
                tmp.write(img_bytes)
                tmp_path = tmp.name
            st.write(f"✔️ [1/4] 暫存檔建立完成（{tmp_path}\uff09")
            try:
                st.write("☁️ [2/4] 上傳圖片至 Gemini Files API...")
                uploaded = client.files.upload(file=tmp_path)
                st.write(f"✔️ [2/4] 上傳完成（uri: {uploaded.uri}\uff09")
                st.write("⏳ [3/4] 等待 File API 就緒（1 秒）...")
                time.sleep(1)
                image_part = types.Part.from_uri(
                    file_uri=uploaded.uri, mime_type=uploaded.mime_type
                )
                st.write(f"✔️ [3/4] image_part 建立完成（mime: {uploaded.mime_type}\uff09")
                st.write(f"🤖 [4/4] 呼叫 generate_content（模型: {user['modelName']}\uff09...")
                text = call_with_retry(
                    client, user["modelName"],
                    build_explanation_prompt(has_answer, answer),
                    image_part,
                )
                st.write(f"✔️ [4/4] 模型回定，回覆圖字數: {len(text) if text else 0}")
            finally:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)

            elapsed = time.time() - t0
            st.session_state["explanation"] = text.strip() if text else "（解析失敗，請重試）"
            st.session_state["explanation_time"] = elapsed
            st.write(f"✅ 解析完成（耗時 {elapsed:.1f} 秒）")
            status.update(label=f"✅ 解析完成！（{elapsed:.1f} 秒）", state="complete")
        except Exception as e:
            status.update(label="❌ 解析失敗", state="error")
            st.error(f"AI 呼叫失敗：{e}")
            return

    # ── 碼表停止，改為最終時間 ──
    show_final_time(timer_ph, "📖 解析耗時", elapsed)
    st.session_state["explanation_time"] = elapsed

    # 寫入紀錄（只有解析欄位）
    try:
        ai_result = {"scaffold1": "", "scaffold2": "", "scaffold3": "",
                     "explanation": st.session_state["explanation"]}
        write_record(user["account"], user["tutorName"], user["keyName"],
                     user["modelName"], answer, has_answer, ai_result, "")
    except Exception as e:
        st.warning(f"⚠️ 紀錄寫入失敗：{e}")

    st.rerun()


def _do_scaffold(user: dict, img_bytes: bytes, has_answer: bool, answer: str):
    """只產出鷹架"""
    api_key = _get_api_key_or_error(user)
    if not api_key:
        return

    client = genai.Client(api_key=api_key)

    # ── 顯示即時碼表 ──
    timer_ph = show_live_timer("🏗️ 鷹架計時")

    with st.status("⏳ 產出鷹架中...", expanded=True) as status:
        t0 = time.time()
        try:
            st.write("📂 [1/4] 建立暫存檔案...")
            with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
                tmp.write(img_bytes)
                tmp_path = tmp.name
            st.write(f"✔️ [1/4] 暫存檔建立完成（{tmp_path}\uff09")
            try:
                st.write("☁️ [2/4] 上傳圖片至 Gemini Files API...")
                uploaded = client.files.upload(file=tmp_path)
                st.write(f"✔️ [2/4] 上傳完成（uri: {uploaded.uri}\uff09")
                st.write("⏳ [3/4] 等待 File API 就緒（1 秒）...")
                time.sleep(1)
                image_part = types.Part.from_uri(
                    file_uri=uploaded.uri, mime_type=uploaded.mime_type
                )
                st.write(f"✔️ [3/4] image_part 建立完成（mime: {uploaded.mime_type}\uff09")
                st.write(f"🤖 [4/4] 呼叫 generate_content（模型: {user['modelName']}\uff09...")
                text = call_with_retry(
                    client, user["modelName"],
                    build_scaffold_prompt(has_answer, answer),
                    image_part,
                )
                st.write(f"✔️ [4/4] 模型回定，回覆圖字數: {len(text) if text else 0}")
            finally:
                if os.path.exists(tmp_path):
                    os.unlink(tmp_path)

            elapsed = time.time() - t0
            st.session_state["scaffold"] = parse_scaffold(text)
            st.session_state["scaffold_time"] = elapsed
            st.write(f"✅ 鷹架完成（耗時 {elapsed:.1f} 秒）")
            status.update(label=f"✅ 鷹架完成！（{elapsed:.1f} 秒）", state="complete")
        except Exception as e:
            status.update(label="❌ 鷹架失敗", state="error")
            st.error(f"AI 呼叫失敗：{e}")
            return

    # ── 碼表停止，改為最終時間 ──
    show_final_time(timer_ph, "🏗️ 鷹架耗時", elapsed)
    st.session_state["scaffold_time"] = elapsed

    # 寫入紀錄（只有鷹架欄位）
    try:
        sc = st.session_state["scaffold"]
        ai_result = {**sc, "explanation": ""}
        write_record(user["account"], user["tutorName"], user["keyName"],
                     user["modelName"], answer, has_answer, ai_result, "")
    except Exception as e:
        st.warning(f"⚠️ 紀錄寫入失敗：{e}")

    st.rerun()

# ============================================================
# 主流程
# ============================================================

def main():
    st.set_page_config(
        page_title="數學解題助教",
        page_icon="🧮",
        layout="centered"
    )

    # 初始化 session state
    if "page" not in st.session_state:
        st.session_state.page = "login"
    if "user" not in st.session_state:
        st.session_state.user = None

    if st.session_state.page == "login" or st.session_state.user is None:
        page_login()
    else:
        page_main()


if __name__ == "__main__":
    main()
