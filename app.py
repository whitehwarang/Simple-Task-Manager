import streamlit as st
import pandas as pd
import sqlite3
import json
import hashlib
from pathlib import Path
from contextlib import contextmanager
import plotly.express as px
import plotly.graph_objects as go

# =========================================================
# 기본 설정
# =========================================================
st.set_page_config(
    page_title="AX 과제 관리 시스템",
    page_icon="📊",
    layout="wide",
)

DB_PATH = "ax_management.db"
CONFIG_PATH = "model_config.json"
ORG_PATH = "organization.json"

DEFAULT_MODEL_CONFIG = {
    "제도": ["부서(국)", "부서(과)", "과제명", "담당자", "진행상태", "관련법령"],
    "사업": ["부서(국)", "부서(과)", "과제명", "담당자", "진행상태", "사업비(백만원)"],
    "27년 예산안": ["부서(국)", "부서(과)", "과제명", "담당자", "진행상태", "예산액"],
    "심사": ["부서(국)", "부서(과)", "담당자", "진행상태", "활용시스템"],
}

STATUS_ORDER = ["아이디어", "기획 중", "개발 중", "적용 완료"]

# =========================================================
# 공통 유틸
# =========================================================
def hash_password(password: str) -> str:
    """SHA-256 기반 비밀번호 해시"""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


@contextmanager
def get_connection():
    """SQLite 커넥션 생성 (Lock 방지 timeout 포함)"""
    conn = sqlite3.connect(DB_PATH, timeout=20, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# =========================================================
# 설정 파일 처리
# =========================================================
def initialize_model_config():
    """최초 실행 시 기본 모델 설정 파일 생성"""
    config_file = Path(CONFIG_PATH)
    if not config_file.exists():
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_MODEL_CONFIG, f, ensure_ascii=False, indent=4)


@st.cache_data(show_spinner=False)
def load_model_config():
    """메타데이터 설정 로드"""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return DEFAULT_MODEL_CONFIG


def save_model_config(config_data: dict):
    """메타데이터 설정 저장"""
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config_data, f, ensure_ascii=False, indent=4)
    load_model_config.clear()


@st.cache_data(show_spinner=False)
def load_organization():
    """조직도 로드"""
    try:
        org_file = Path(ORG_PATH)
        if org_file.exists():
            with open(ORG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}
    except Exception as e:
        st.error(f"조직도 로드 오류: {e}")
        return {}

# =========================================================
# DB 초기화
# =========================================================
def initialize_database():
    """DB 및 테이블 생성"""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                bureau   TEXT NOT NULL, 
                team     TEXT NOT NULL
            )
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_tasks_bureau_team_category
            ON tasks(bureau, team, category)
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS task_attributes (
                task_id INTEGER,
                attribute_name TEXT,
                attribute_value TEXT,
                UNIQUE(task_id, attribute_name),
                FOREIGN KEY(task_id) REFERENCES tasks(id)
            )
            """
        )
        cursor.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_task_attributes_taskid
            ON task_attributes(task_id)
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                password_hash TEXT NOT NULL,
                bureau TEXT NOT NULL,
                team TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('admin', 'user'))
            )
            """
        )
        seed_users = [
            ("admin", hash_password("admin123"), "차장", "지식재산인공지능전환추진단", "admin"),
            ("kim", hash_password("kim123"), "디지털융합심사국", "인공지능빅데이터심사과", "user"),
            ("lee", hash_password("lee123"), "지식재산정보국", "지식재산정책과", "user"),
            ("park", hash_password("park123"), "지식재산보호협력국", "지식재산보호정책과", "user"),
        ]
        for user in seed_users:
            cursor.execute(
                """
                INSERT OR IGNORE INTO users
                (username, password_hash, bureau, team, role)
                VALUES (?, ?, ?, ?, ?)
                """,
                user,
            )


# =========================================================
# 인증 로직
# =========================================================
def authenticate_user(username: str, password: str):
    """사용자 인증"""
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT *
                FROM users
                WHERE username = ?
                AND password_hash = ?
                """,
                (username, hash_password(password)),
            )
            return cursor.fetchone()
    except Exception as e:
        st.error(f"로그인 오류: {e}")
        return None


# =========================================================
# 데이터 로드
# =========================================================
@st.cache_data(show_spinner=False, ttl=5)
def get_existing_bureaus():
    """현재 등록된 부서(국) 목록 조회"""
    try:
        with get_connection() as conn:
            query = """
                SELECT DISTINCT bureau
                FROM tasks
                WHERE bureau IS NOT NULL AND bureau != ''
                ORDER BY bureau
            """
            df = pd.read_sql_query(query, conn)
            return df["bureau"].tolist()
    except Exception:
        return []


@st.cache_data(show_spinner=False, ttl=5)
def load_all_tasks():
    """EAV 구조를 DataFrame 형태로 변환"""
    config = load_model_config()
    try:
        with get_connection() as conn:
            query = """
            SELECT
                t.id,
                t.category,
                t.bureau,
                t.team,
                ta.attribute_name,
                ta.attribute_value
            FROM tasks t
            LEFT JOIN task_attributes ta ON t.id = ta.task_id
            """
            raw_df = pd.read_sql_query(query, conn)

        if raw_df.empty:
            return pd.DataFrame()

        grouped_rows = []
        for task_id, group in raw_df.groupby("id"):
            category = group["category"].iloc[0]
            bureau = group["bureau"].iloc[0]
            team = group["team"].iloc[0]

            row_data = {
                "id": task_id,
                "카테고리": category,
                "부서(국)": bureau,
                "부서(과)": team,
            }
            for _, row in group.iterrows():
                attr_name = row["attribute_name"]
                attr_value = row["attribute_value"]
                if attr_name:
                    row_data[attr_name] = attr_value
            grouped_rows.append(row_data)

        final_df = pd.DataFrame(grouped_rows)
        ordered_columns = ["id", "카테고리", "부서(국)", "부서(과)"]

        for category, attrs in config.items():
            for attr in attrs:
                if attr not in ordered_columns:
                    ordered_columns.append(attr)

        existing_columns = [col for col in ordered_columns if col in final_df.columns]
        remaining_columns = [col for col in final_df.columns if col not in existing_columns]
        return final_df[existing_columns + remaining_columns]
    except Exception as e:
        st.error(f"데이터 로드 오류: {e}")
        return pd.DataFrame()


# =========================================================
# 저장 로직
# =========================================================
def save_department_tasks(bureau: str, team: str, category: str, dataframe: pd.DataFrame):
    """특정 부서(국/과) + 카테고리 데이터를 전체 교체 방식으로 저장"""
    config = load_model_config()
    attributes = config.get(category, [])
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id FROM tasks WHERE bureau = ? AND team = ? AND category = ?",
                (bureau, team, category),
            )
            existing_ids = [row[0] for row in cursor.fetchall()]

            if existing_ids:
                cursor.executemany("DELETE FROM task_attributes WHERE task_id = ?", [(t_id,) for t_id in existing_ids])
                cursor.executemany("DELETE FROM tasks WHERE id = ?", [(t_id,) for t_id in existing_ids])

            for _, row in dataframe.iterrows():
                cursor.execute("INSERT INTO tasks(category, bureau, team) VALUES (?, ?, ?)", (category, bureau, team))
                task_id = cursor.lastrowid

                for attr in attributes:
                    if attr == "부서(국)":
                        value = bureau
                    elif attr == "부서(과)":
                        value = team
                    else:
                        value = str(row.get(attr, "") or "")

                    cursor.execute(
                        "INSERT INTO task_attributes (task_id, attribute_name, attribute_value) VALUES (?, ?, ?)",
                        (task_id, attr, value),
                    )
        load_all_tasks.clear()
        get_existing_bureaus.clear()
        return True
    except Exception as e:
        st.error(f"저장 오류: {e}")
        return False


def insert_single_task(category: str, bureau: str, team: str, form_data: dict):
    """단일 과제 등록"""
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("INSERT INTO tasks(category, bureau, team) VALUES (?, ?, ?)", (category, bureau, team))
            task_id = cursor.lastrowid

            for key, value in form_data.items():
                cursor.execute(
                    "INSERT INTO task_attributes (task_id, attribute_name, attribute_value) VALUES (?, ?, ?)",
                    (task_id, key, str(value)),
                )
        load_all_tasks.clear()
        get_existing_bureaus.clear()
        return True
    except Exception as e:
        st.error(f"등록 오류: {e}")
        return False


# =========================================================
# 화면 컴포넌트 렌더링
# =========================================================
def render_login_page():
    """로그인 페이지"""
    st.title("🔐 AX 과제 관리 시스템")
    st.markdown("### 사내 AX(AI 부서 혁신) 통합 관리 플랫폼")
    col1, col2, col3 = st.columns([1, 1.2, 1])

    with col2:
        st.info("테스트 계정\n\n- admin / admin123\n- kim / kim123\n- lee / lee123")
        with st.form("login_form"):
            username = st.text_input("아이디")
            password = st.text_input("비밀번호", type="password")
            submitted = st.form_submit_button("로그인", use_container_width=True)
            if submitted:
                user = authenticate_user(username, password)
                if user:
                    st.session_state.logged_in = True
                    st.session_state.username = user["username"]
                    st.session_state.user_bureau = user["bureau"]
                    st.session_state.user_team = user["team"]
                    st.session_state.user_role = user["role"]
                    st.success("로그인 성공")
                    st.rerun()
                else:
                    st.error("아이디 또는 비밀번호가 올바르지 않습니다.")


def render_dashboard(df: pd.DataFrame):
    """종합 대시보드"""
    st.subheader("📊 전사 AX 종합 대시보드")
    if df.empty:
        st.warning("등록된 데이터가 없습니다.")
        return

    total_tasks = len(df)
    total_bureaus = df["부서(국)"].nunique() if "부서(국)" in df.columns else 0

    numeric_total = 0
    for col in df.columns:
        if "예산" in col or "사업비" in col:
            try:
                numeric_total += pd.to_numeric(df[col], errors="coerce").fillna(0).sum()
            except Exception:
                pass

    metric_col1, metric_col2, metric_col3 = st.columns(3)
    with metric_col1: st.metric("총 추진 과제", f"{total_tasks:,} 건")
    with metric_col2: st.metric("참여 국(부서) 수", f"{total_bureaus:,} 개")
    with metric_col3: st.metric("전사 예산/사업비 합계", f"{numeric_total:,.0f} 백만원")

    st.divider()

    status_col = next((col for col in df.columns if "진행상태" in col), None)
    chart_col1, chart_col2 = st.columns(2)

    with chart_col1:
        completed_ratio = 0
        if status_col:
            completed_count = df[status_col].fillna("").astype(str).str.contains("적용 완료").sum()
            completed_ratio = completed_count / total_tasks * 100 if total_tasks > 0 else 0
        gauge_fig = go.Figure(go.Indicator(
            mode="gauge+number", value=completed_ratio, title={"text": "적용 완료율 (%)"},
            gauge={"axis": {"range": [0, 100]}, "bar": {"thickness": 0.35},
                   "steps": [{"range": [0, 50], "color": "lightgray"}, {"range": [50, 80], "color": "gray"}, {"range": [80, 100], "color": "green"}]}
        ))
        gauge_fig.update_layout(height=350)
        st.plotly_chart(gauge_fig, use_container_width=True)

    with chart_col2:
        category_counts = df["카테고리"].value_counts().reset_index(name="개수")
        donut_fig = px.pie(category_counts, names="카테고리", values="개수", hole=0.5, title="카테고리별 AX 과제 비중")
        donut_fig.update_layout(height=350)
        st.plotly_chart(donut_fig, use_container_width=True)

    col3, col4 = st.columns(2)
    with col3:
        path_hierarchy = [c for c in ["부서(국)", "부서(과)"] if c in df.columns]
        tree_df = df.groupby(["카테고리"] + path_hierarchy).size().reset_index(name="과제수")
        tree_fig = px.treemap(tree_df, path=["카테고리"] + path_hierarchy, values="과제수", title="카테고리 및 부서별 과제 현황")
        tree_fig.update_layout(height=450)
        st.plotly_chart(tree_fig, use_container_width=True)

    with col4:
        if status_col:
            progress_df = df[status_col].fillna("미정").value_counts().reset_index(name="개수")
            progress_df["정렬"] = progress_df["진행상태"].apply(lambda x: STATUS_ORDER.index(x) if x in STATUS_ORDER else 999)
            progress_df = progress_df.sort_values("정렬")
            progress_fig = px.bar(progress_df, x="개수", y="진행상태", orientation="h", title="진행 단계별 과제 분포")
            progress_fig.update_layout(height=450)
            st.plotly_chart(progress_fig, use_container_width=True)


def render_readonly_view(df: pd.DataFrame):
    """전사 과제 조회"""
    st.subheader("👁️ 전사 과제 조회")
    if df.empty:
        st.warning("등록된 데이터가 없습니다.")
        return

    col1, col2, col3 = st.columns(3)
    with col1: category_filter = st.selectbox("카테고리 필터", ["전체"] + sorted(df["카테고리"].dropna().unique().tolist()))
    with col2: bureau_filter = st.selectbox("부서(국) 필터", ["전체"] + sorted(df["부서(국)"].dropna().unique().tolist()))
    with col3: team_filter = st.selectbox("부서(과) 필터", ["전체"] + sorted(df["부서(과)"].dropna().unique().tolist()))

    filtered_df = df.copy()
    if category_filter != "전체": filtered_df = filtered_df[filtered_df["카테고리"] == category_filter]
    if bureau_filter != "전체": filtered_df = filtered_df[filtered_df["부서(국)"] == bureau_filter]
    if team_filter != "전체": filtered_df = filtered_df[filtered_df["부서(과)"] == team_filter]

    st.dataframe(filtered_df, use_container_width=True, hide_index=True)


def render_department_management():
    """부서별 관리 화면"""
    st.subheader("📝 우리 부서 과제 관리 및 등록")
    config = load_model_config()
    all_df = load_all_tasks()

    user_bureau = st.session_state.user_bureau
    user_team = st.session_state.user_team

    st.info(f"현재 로그인 소속: {user_bureau} / {user_team}")
    selected_category = st.selectbox("관리할 카테고리 선택", list(config.keys()))
    category_attributes = config.get(selected_category, [])

    dept_df = pd.DataFrame()
    if not all_df.empty:
        dept_df = all_df[(all_df["부서(국)"] == user_bureau) & (all_df["부서(과)"] == user_team) & (all_df["카테고리"] == selected_category)].copy()

    editable_columns = [col for col in category_attributes if col not in ["부서(국)", "부서(과)"]]
    for col in editable_columns:
        if col not in dept_df.columns: dept_df[col] = ""

    editor_df = dept_df[editable_columns].copy() if not dept_df.empty else pd.DataFrame(columns=editable_columns)

    st.markdown("### ✏️ 기존 과제 수정")
    column_config = {col: st.column_config.NumberColumn(col, min_value=0, step=1, format="%d") for col in editable_columns if "예산" in col or "사업비" in col}

    edited_df = st.data_editor(editor_df, num_rows="dynamic", use_container_width=True, column_config=column_config, key="task_editor")

    if st.button("💾 수정사항 저장", use_container_width=True):
        save_df = edited_df.copy()
        save_df["부서(국)"] = user_bureau
        save_df["부서(과)"] = user_team
        if save_department_tasks(bureau=user_bureau, team=user_team, category=selected_category, dataframe=save_df):
            st.success("저장 완료")
            st.rerun()

    st.divider()

    st.markdown("### ➕ 신규 과제 등록")
    with st.form("new_task_form"):
        form_data = {}
        for attr in category_attributes:
            if attr == "부서(국)":
                st.text_input(attr, value=user_bureau, disabled=True)
                form_data[attr] = user_bureau
            elif attr == "부서(과)":
                st.text_input(attr, value=user_team, disabled=True)
                form_data[attr] = user_team
            elif attr == "진행상태":
                form_data[attr] = st.selectbox(attr, STATUS_ORDER)
            else:
                form_data[attr] = st.text_input(attr)

        if st.form_submit_button("신규 과제 등록", use_container_width=True):
            form_data["부서(국)"] = user_bureau
            form_data["부서(과)"] = user_team
            if insert_single_task(category=selected_category, bureau=user_bureau, team=user_team, form_data=form_data):
                st.success("신규 과제가 등록되었습니다.")
                st.rerun()


def render_admin_sidebar():
    """관리자 설정 메뉴"""
    if st.session_state.user_role != "admin": return
    config = load_model_config()

    with st.sidebar.expander("⚙️ 시스템 관리자 메뉴"):
        st.markdown("### 신규 카테고리 추가")
        new_category = st.text_input("카테고리명")
        new_attributes = st.text_area("관리 항목 입력 (쉼표 구분)", placeholder="부서(국), 부서(과), 과제명, 담당자, 진행상태")

        if st.button("카테고리 추가"):
            if new_category.strip() and new_attributes.strip():
                config[new_category] = [item.strip() for item in new_attributes.split(",") if item.strip()]
                save_model_config(config)
                st.success("카테고리 추가 완료")
                st.rerun()


# =========================================================
# 메인 앱
# =========================================================
def main():
    initialize_model_config()
    initialize_database()

    if "logged_in" not in st.session_state:
        st.session_state.logged_in = False

    if not st.session_state.logged_in:
        render_login_page()
        return

    # 조직도 로드 (Dropdown용 데이터 소스)
    org_chart = load_organization()

    # =====================================================
    # 사이드바 사용자 관리 부문 (조직도 Dropdown 반영)
    # =====================================================
    st.sidebar.title("🏢 사용자 정보")
    st.sidebar.write(f"사용자: {st.session_state.username} ({st.session_state.user_role})")

    st.sidebar.markdown("### ✏️ 나의 소속 부서")
    
    if st.session_state.user_role == "admin" and org_chart:
        # 조직도 리스트 추출
        bureau_options = list(org_chart.keys())
        
        # 기본값 인덱스 가공
        default_b_idx = bureau_options.index(st.session_state.user_bureau) if st.session_state.user_bureau in bureau_options else 0
        selected_bureau = st.sidebar.selectbox("부서(국) 선택", bureau_options, index=default_b_idx)
        
        team_options = org_chart.get(selected_bureau, [])
        default_t_idx = team_options.index(st.session_state.user_team) if st.session_state.user_team in team_options else 0
        selected_team = st.sidebar.selectbox("부서(과) 선택", team_options, index=default_t_idx)
        
        st.session_state.user_bureau = selected_bureau
        st.session_state.user_team = selected_team
    else:
        # 일반 사용자 혹은 조직도 파일이 비어있는 경우 Fallback(일반 텍스트 노출)
        st.sidebar.text_input("부서(국)", value=st.session_state.user_bureau, disabled=True)
        st.sidebar.text_input("부서(과)", value=st.session_state.user_team, disabled=True)

    st.sidebar.divider()
    if st.sidebar.button("로그아웃"):
        st.session_state.clear()
        st.rerun()

    render_admin_sidebar()

    # =====================================================
    # 신규 사용자 추가 컴포넌트 (조직도 Dropdown 반영)
    # =====================================================
    st.markdown("### 👤 사용자 추가")
    col1, col2 = st.columns(2)
    with col1:
        new_user = st.text_input("아이디", key="create_user_id")
        new_pw = st.text_input("비밀번호", type="password", key="create_user_pw")
        new_role = st.selectbox("권한", ["user", "admin"], key="create_user_role")
    with col2:
        if org_chart:
            # 국(bureau) 선택 시 하위 과(team) 목록 동적 업데이트
            reg_bureau_options = list(org_chart.keys())
            chosen_reg_bureau = st.selectbox("등록 부서(국)", reg_bureau_options, key="reg_user_bureau_select")
            
            reg_team_options = org_chart.get(chosen_reg_bureau, [])
            chosen_reg_team = st.selectbox("등록 부서(과)", reg_team_options, key="reg_user_team_select")
        else:
            # 조직도 없을 때 기본 텍스트 필드로 자동 전환
            chosen_reg_bureau = st.text_input("부서(국)", key="create_user_bureau_text")
            chosen_reg_team = st.text_input("부서(과)", key="create_user_team_text")

    if st.button("사용자 생성"):
        if new_user.strip() and new_pw.strip() and chosen_reg_bureau and chosen_reg_team:
            with get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    INSERT INTO users (username, password_hash, bureau, team, role)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (new_user, hash_password(new_pw), chosen_reg_bureau, chosen_reg_team, new_role),
                )
            st.success(f"사용자 '{new_user}' 생성 완료 ({chosen_reg_bureau} / {chosen_reg_team})")
        else:
            st.error("모든 필드를 정확하게 입력 또는 선택해주세요.")

    # =====================================================
    # 하단 메인 탭 뷰 영역
    # =====================================================
    all_df = load_all_tasks()
    tab1, tab2, tab3 = st.tabs(["📊 종합 대시보드", "👁️ 전사 과제 조회", "📝 우리 부서 과제 관리"])

    with tab1: render_dashboard(all_df)
    with tab2: render_readonly_view(all_df)
    with tab3: render_department_management()


if __name__ == "__main__":
    main()