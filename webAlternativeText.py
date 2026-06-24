import os
import re
import time
from urllib.parse import urljoin
import streamlit as st
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from openai import OpenAI

# 스트림릿 웹 페이지 설정
st.set_page_config(page_title="웹접근성 alt 대체 텍스트 관리", page_icon="🛠️", layout="wide")
st.title("🛠️ 웹접근성 alt 대체 텍스트 관리")

# 세션 상태(Session State) 초기화
if "article_list" not in st.session_state:
    st.session_state.article_list = []
if "log_messages" not in st.session_state:
    st.session_state.log_messages = []
if "article_images" not in st.session_state:
    st.session_state.article_images = {}  # { url: [ {idx, src, alt}, ... ] }

def add_log(msg: str):
    print(msg)
    st.session_state.log_messages.append(msg)

# ============================================================
# [웹 UI 영역] 사이드바 설정 정보 입력
# ============================================================
st.sidebar.header("🔑 기본 설정 정보")
OPENAI_API_KEY = st.sidebar.text_input("OpenAI API Key", type="password", help="sk-... 형태의 API 키를 입력하세요.")
LOGIN_URL = st.sidebar.text_input("로그인 URL (LOGIN_URL)", value="https://")
LIST_URL = st.sidebar.text_input("게시글 목록 URL (LIST_URL)", value="https://")
LOGIN_ID = st.sidebar.text_input("로그인 ID")
LOGIN_PASSWORD = st.sidebar.text_input("로그인 패스워드", type="password")

# ============================================================
# [핵심 로직 영역] GPT & 플레이라이트
# ============================================================
SYSTEM_PROMPT = (
    "당신은 한국어 웹 접근성 전문가입니다. 주어진 이미지를 시각장애인 사용자의 "
    "스크린리더가 읽어줄 대체 텍스트(alt)로 작성합니다. 이미지 안에 보이는 텍스트는 "
    "맞춤법, 띄어쓰기, 숫자, 날짜, 기호를 임의로 수정하거나 요약하지 않고 가능한 원문 "
    "순서대로 포함합니다. '이미지', '사진', '그림'으로 시작하지 않고, 따옴표나 부연 설명 "
    "없이 최종 대체 텍스트만 출력합니다."
)

def generate_alt_text(client, image_url: str) -> str | None:
    try:
        if "localhost" in image_url or "127.0.0.1" in image_url or image_url.startswith("/"):
            add_log(f"  ⚠️ [경고] 이미지 주소가 로컬/내부망 경로 같습니다. 외부 접근이 불가능하면 AI 생성이 실패합니다. ({image_url})")

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "이 이미지의 대체 텍스트를 작성해 주세요."},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                },
            ],
            max_tokens=500,
        )
        alt = response.choices[0].message.content.strip()
        alt = re.sub(r'^["\'\u201c\u201d]|["\'\u201c\u201d]$', "", alt)
        return alt
    except Exception as e:
        add_log(f"  ❌ [AI 생성 실패] 이미지 분석 오류 발생: {str(e)}")
        return None

# 🌟 [오류 해결의 핵심] 버튼 클릭 시점에 세션 값을 안전하게 변경하는 콜백 함수 선언
def run_ai_alt_callback(target_key: str, img_src: str):
    if not OPENAI_API_KEY:
        st.error("왼쪽 사이드바에 OpenAI API Key를 먼저 입력해주세요.")
        return
        
    client = OpenAI(api_key=OPENAI_API_KEY)
    generated = generate_alt_text(client, img_src)
    if generated:
        # 렌더링 주기 밖인 on_click 시점에 변경하므로 StreamlitAPIException 에러가 절대 나지 않습니다.
        st.session_state[target_key] = generated
        add_log(f"  ✨ [AI 반영 성공] 키({target_key})의 텍스트 영역 데이터 갱신 완료")

def login(page):
    page.goto(LOGIN_URL)
    page.wait_for_load_state("networkidle")
    page.fill("#userId", LOGIN_ID)
    page.fill("#userPassword", LOGIN_PASSWORD)
    page.click("button[type=submit]")
    page.wait_for_load_state("networkidle")

def fetch_links_and_images():
    if not LOGIN_URL or not LIST_URL or not LOGIN_ID or not LOGIN_PASSWORD:
        st.error("사이드바의 로그인 및 목록 정보를 모두 입력해주세요.")
        return

    st.session_state.article_list = []
    st.session_state.article_images = {}

    with st.spinner("🔄 게시판 목록 분석 및 각 글의 이미지 상태를 일괄 수집 중..."):
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            try:
                login(page)
                page.goto(LIST_URL)
                page.wait_for_load_state("networkidle")
                
                links = page.eval_on_selector_all(
                    'a[href*="artclView.do"]',
                    "els => els.map(el => el.href)",
                )
                fetched_links = list(dict.fromkeys(links))
                st.session_state.article_list = fetched_links
                
                for idx, url in enumerate(fetched_links):
                    add_log(f"[{idx+1}/{len(fetched_links)}] 이미지 상태 스캔 중: {url}")
                    page.goto(url)
                    page.wait_for_load_state("networkidle")
                    
                    edit_btn = page.locator("input[value='수정']").first
                    if not edit_btn.is_visible(timeout=2000):
                        add_log(f"  ⚠️ {url} 글의 수정 권한이 없거나 수정 버튼을 찾지 못했습니다.")
                        st.session_state.article_images[url] = []
                        continue
                        
                    edit_btn.click()
                    page.wait_for_load_state("networkidle")
                    page.wait_for_timeout(1200)
                    
                    editor_frame = None
                    for frame in page.frames:
                        if "NamoSE_editorframe_editor" in frame.name or "NamoSE_editorframe_editor" in frame.url:
                            editor_frame = frame
                            break
                            
                    if editor_frame:
                        imgs = editor_frame.eval_on_selector_all(
                            "img",
                            "els => els.map((el, idx) => ({idx, src: el.getAttribute('src'), alt: el.getAttribute('alt')}))",
                        )
                        for img in imgs:
                            if not img["src"].startswith("http"):
                                img["src"] = urljoin(url, img["src"])
                            if not img["alt"] or img["alt"].strip() == "":
                                img["alt"] = "alt값 미존재"
                                
                            widget_key = f"widget_{idx}_{img['idx']}"
                            st.session_state[widget_key] = img["alt"]
                            
                        st.session_state.article_images[url] = imgs
                    else:
                        add_log(f"  ❌ {url} 글에서 나모 에디터 프레임을 발견하지 못했습니다.")
                        st.session_state.article_images[url] = []
                        
                st.success(f"🎉 총 {len(st.session_state.article_list)}개의 게시글 및 내부 이미지 연동이 완료되었습니다!")
            except Exception as e:
                st.error(f"동기화 수집 중 오류 발생: {e}")
            finally:
                browser.close()

def save_alt_to_web(url: str, img_data_list: list, article_idx: int):
    with st.spinner("🚀 실제 게시판에 저장 반영 중입니다..."):
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, slow_mo=300)
            page = browser.new_page()
            try:
                add_log(f"\n[서버 반영 저장 시작] {url}")
                login(page)
                page.goto(url)
                page.wait_for_load_state("networkidle")
                
                page.locator("input[value='수정']").first.click()
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(1500)
                
                editor_frame = None
                for frame in page.frames:
                    if "NamoSE_editorframe_editor" in frame.name or "NamoSE_editorframe_editor" in frame.url:
                        editor_frame = frame
                        break
                
                if editor_frame:
                    for img in img_data_list:
                        widget_key = f"widget_{article_idx}_{img['idx']}"
                        user_edited_alt = st.session_state.get(widget_key, img["alt"])
                        
                        final_alt = "" if user_edited_alt == "alt값 미존재" else user_edited_alt
                        
                        editor_frame.evaluate(
                            """({idx, altText}) => {
                                const imgEls = document.querySelectorAll('img');
                                if (imgEls[idx]) imgEls[idx].setAttribute('alt', altText);
                            }""",
                            {"idx": img["idx"], "altText": final_alt},
                        )
                        add_log(f"  -> 이미지 [{img['idx']}]번에 alt='{final_alt}' 주입")
                    
                    save_candidates = ["input[value='수정']"]
                    save_btn = None
                    for sel in save_candidates:
                        if page.locator(sel).first.is_visible(timeout=1000):
                            save_btn = page.locator(sel).first
                            break
                    
                    if save_btn:
                        save_btn.click()
                        page.wait_for_load_state("networkidle")
                        add_log("  ✅ [성공] 나모 에디터 반영 및 수정 완료!")
                        st.success("게시글 수정 저장에 성공했습니다!")
                    else:
                        add_log("  ⚠️ [경고] 수정 버튼을 찾지 못했습니다.")
            except Exception as e:
                add_log(f"  ❌ [오류] 반영 실패: {e}")
            finally:
                browser.close()


# ============================================================
# [화면 레이아웃 구성]
# ============================================================
col1, col2 = st.columns([6, 4])

with col1:
    st.subheader("📋 게시글 목록 및 이미지 제어")
    
    if st.button("🔄 전체 게시판 글 & 이미지 일괄 동기화", type="primary"):
        fetch_links_and_images()
        
    if st.session_state.article_list:
        for idx, url in enumerate(st.session_state.article_list):
            display_title = url.split('?')[-1] if '?' in url else url
            
            with st.expander(f"📝 글 [{idx+1}] : {display_title}", expanded=True):
                st.link_button("🌐 실제 게시글 새창으로 열기", url, use_container_width=False)
                st.markdown(" ")
                
                saved_imgs = st.session_state.article_images.get(url, [])
                if saved_imgs:
                    for img_idx, img in enumerate(saved_imgs):
                        img_col, text_col, ai_col = st.columns([2, 6, 2])
                        
                        img_col.image(img["src"], width=100)
                        
                        widget_key = f"widget_{idx}_{img['idx']}"
                        if widget_key not in st.session_state:
                            st.session_state[widget_key] = img["alt"]
                        
                        # 🌟 오직 'key' 매개변수만 사용하여 세션 데이터와 컴포넌트를 1:1 결합합니다.
                        text_col.text_area(
                            f"이미지 [{img['idx']}] 소스: ..{img['src'][-20:]}", 
                            key=widget_key,
                            height=150
                        )
                        
                        # 🌟 [오류 해결의 핵심] AI 생성 버튼에 on_click 콜백을 연결하고 인자를 전달합니다.
                        ai_col.button(
                            "✨ AI alt 생성", 
                            key=f"ai_{idx}_{img_idx}",
                            on_click=run_ai_alt_callback,
                            args=(widget_key, img["src"])
                        )
                    
                    st.markdown("---")
                    if st.button("💾 이 게시글 최종 변경사항 저장(서버반영)", key=f"save_all_{idx}"):
                        save_alt_to_web(url, saved_imgs, idx)
                else:
                    st.info("본문에 등록된 이미지가 없거나 글 수정 접근 권한이 없습니다.")
    else:
        st.info("사이드바 정보를 확인하신 후 위의 '동기화' 버튼을 눌러 목록과 이미지를 불러오세요.")

with col2:
    st.subheader("🖥️ 데이터 동기화 및 저장 실시간 로그")
    if st.button("🗑️ 로그 초기화"):
        st.session_state.log_messages = []
        st.rerun()
        
    st.text_area(
        "초기 로딩 단계와 서버 저장 프로세스 로그가 여기에 기록됩니다.", 
        value="\n".join(st.session_state.log_messages), 
        height=650
    )
