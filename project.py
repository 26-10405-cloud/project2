"""걷기 산책 코스 추천 - Streamlit 앱 (기분 + 경치 좋은 장소 + 실제 위치)

실행:  pip install -r requirements.txt  →  streamlit run app.py

API 키 없이 동작하도록 무료 공개 서비스를 사용합니다.
  - 장소 검색: OpenStreetMap Overpass API
  - 도보 경로: routing.openstreetmap.de (OSRM foot)
  - 주소 검색: Nominatim
선택(키가 있으면 사용): ANTHROPIC_API_KEY → 자유 입력 기분 문장을 분석
"""
import math
import os
import random
import time

import folium
import requests
import streamlit as st
from streamlit_folium import st_folium
from streamlit_geolocation import streamlit_geolocation

st.set_page_config(page_title="오늘의 산책", page_icon="🚶", layout="wide")

UA = {"User-Agent": "walk-course-app/0.1 (streamlit demo)"}

# 기분 → 어울리는 지형지물 (OSM 태그)
MOODS = {
    "😊 기분 좋아요": {
        "why": "활기를 더해줄 탁 트인 공원과 전망 좋은 곳",
        "tags": ['["leisure"="park"]', '["tourism"="viewpoint"]', '["leisure"="garden"]'],
    },
    "😔 울적해요": {
        "why": "마음이 차분해지는 물가와 정원",
        "tags": ['["natural"="water"]', '["waterway"="river"]', '["leisure"="garden"]'],
    },
    "😤 스트레스 받아요": {
        "why": "머리를 식혀줄 숲과 나무가 많은 공원",
        "tags": ['["natural"="wood"]', '["leisure"="park"]', '["landuse"="forest"]'],
    },
    "🥰 설레요": {
        "why": "분위기 좋은 전망대와 꽃이 있는 정원",
        "tags": ['["tourism"="viewpoint"]', '["leisure"="garden"]', '["natural"="water"]'],
    },
    "😴 지치고 멍해요": {
        "why": "가볍게 걷기 좋은 조용한 공원과 호수",
        "tags": ['["leisure"="park"]', '["natural"="water"]'],
    },
}


def classify_mood_with_claude(text: str):
    """자유 입력 문장 → MOODS 키 하나로 분류 (ANTHROPIC_API_KEY 있을 때만)."""
    try:
        key = os.getenv("ANTHROPIC_API_KEY") or st.secrets.get("ANTHROPIC_API_KEY", None)
    except Exception:
        key = None
    if not key:
        return None
    options = list(MOODS.keys())
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={
                "model": "claude-sonnet-4-6",
                "max_tokens": 50,
                "messages": [{"role": "user", "content":
                    f"다음 문장에 가장 가까운 기분을 보기 중 하나만 정확히 그대로 답해줘.\n"
                    f"보기: {options}\n문장: {text}"}],
            },
            timeout=10,
        )
        answer = r.json()["content"][0]["text"].strip()
        return next((o for o in options if o in answer), None)
    except Exception:
        return None


def geocode(address: str):
    r = requests.get("https://nominatim.openstreetmap.org/search",
                     params={"q": address, "format": "json", "limit": 1},
                     headers=UA, timeout=15)
    data = r.json()
    return (float(data[0]["lat"]), float(data[0]["lon"])) if data else None


def haversine(a, b):
    R = 6371000
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def spot_kind(t: dict) -> str:
    """OSM 태그 → 사람이 읽는 장소 종류."""
    if t.get("tourism") == "viewpoint":
        return "전망 명소"
    if t.get("leisure") == "garden":
        return "정원"
    if t.get("leisure") == "park":
        return "공원"
    if t.get("natural") == "water":
        return "호수·물가"
    if t.get("waterway"):
        return "하천"
    if t.get("natural") == "wood" or t.get("landuse") == "forest":
        return "숲"
    return "경치 좋은 곳"


OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://z.overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
FETCH_RADIUS = 3500  # 항상 같은 반경으로 한 번에 검색 → 시간을 바꿔도 캐시 재사용


def overpass_query(q: str):
    """여러 Overpass 서버를 돌아가며 시도. 빈 응답/오류면 다음 서버로."""
    fails = []
    for rnd in range(2):  # 전체 서버를 최대 2바퀴
        for url in OVERPASS_URLS:
            host = url.split("/")[2]
            try:
                r = requests.post(url, data={"data": q}, headers=UA, timeout=45)
                if r.status_code == 200:
                    return r.json()
                fails.append(f"{host}: HTTP {r.status_code}")
            except Exception as e:
                fails.append(f"{host}: {type(e).__name__}")
        time.sleep(2 * (rnd + 1))
    raise RuntimeError("장소 검색 서버가 모두 응답하지 않아요. 1~2분 뒤 다시 눌러주세요. ("
                       + ", ".join(fails[-5:]) + ")")


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_spots(lat, lon, tags):
    """고정 반경으로 장소를 한 번에 가져와 캐시한다."""
    parts = "".join(f"nwr{t}(around:{FETCH_RADIUS},{lat},{lon});" for t in tags)
    q = f"[out:json][timeout:25];({parts});out center 200;"
    data = overpass_query(q)
    spots, seen = [], set()
    for el in data.get("elements", []):
        name = el.get("tags", {}).get("name")
        c = (el.get("lat"), el.get("lon")) if "lat" in el else (
            el.get("center", {}).get("lat"), el.get("center", {}).get("lon"))
        if not name or name in seen or c[0] is None:
            continue
        seen.add(name)
        spots.append({"name": name, "lat": c[0], "lon": c[1],
                      "kind": spot_kind(el.get("tags", {}))})
    return spots


def find_spots(lat, lon, radius, tags):
    """걷는 시간에 맞는 반경 안의 장소만 골라 돌려준다."""
    # 좌표를 소수 3자리(약 100m)로 맞춰 같은 동네는 같은 캐시를 쓰게 한다
    allspots = fetch_spots(round(lat, 3), round(lon, 3), tags)
    here = (lat, lon)
    for s in allspots:
        s["dist"] = haversine(here, (s["lat"], s["lon"]))
    near = [s for s in allspots if s["dist"] <= radius]
    if not near:  # 반경 안에 없으면 가장 가까운 몇 곳이라도 사용
        near = sorted(allspots, key=lambda s: s["dist"])[:5]
    return near


@st.cache_data(ttl=600, show_spinner=False)
def walking_route(points):
    coords = ";".join(f"{lon},{lat}" for lat, lon in points)
    url = f"https://routing.openstreetmap.de/routed-foot/route/v1/foot/{coords}"
    r = requests.get(url, params={"overview": "full", "geometries": "geojson"},
                     headers=UA, timeout=30)
    route = r.json()["routes"][0]
    line = [(lat, lon) for lon, lat in route["geometry"]["coordinates"]]
    return line, route["distance"], route["duration"]


def build_course(start, spots, n_stops):
    """가까운 곳부터 이어 붙여 출발지로 돌아오는 순환 코스."""
    chosen = random.sample(spots, min(n_stops, len(spots)))
    ordered, cur = [], start
    while chosen:
        nxt = min(chosen, key=lambda s: haversine(cur, (s["lat"], s["lon"])))
        ordered.append(nxt)
        chosen.remove(nxt)
        cur = (nxt["lat"], nxt["lon"])
    return ordered


KIND_WHY = {
    "공원": "넓은 잔디와 나무가 있어 걷는 동안 마음이 트여요.",
    "정원": "꽃과 나무를 가까이서 볼 수 있어 눈이 편안해져요.",
    "전망 명소": "탁 트인 풍경을 한눈에 볼 수 있어 기분 전환에 좋아요.",
    "호수·물가": "물결과 물가의 분위기가 마음을 차분하게 가라앉혀 줘요.",
    "하천": "흐르는 물을 따라 걸으면 생각이 정리되고 편안해져요.",
    "숲": "나무 그늘과 맑은 공기가 머리를 식혀줘요.",
    "경치 좋은 곳": "주변 풍경을 즐기며 쉬어가기 좋아요.",
}


def explain_course(res):
    """이 코스를 왜 추천했는지 문장 목록으로 만든다."""
    lines = [f"**기분 반영**: '{res['mood']}'인 오늘은 {res['why']}이(가) "
             f"잘 어울려서 이런 장소들을 골랐어요."]
    prev = res["start"]
    for i, s in enumerate(res["stops"], 1):
        pos = (s["lat"], s["lon"])
        d = haversine(prev, pos)
        lines.append(
            f"**{i}. {s['name']}** ({s['kind']}) — {KIND_WHY[s['kind']]} "
            f"{'출발지' if i == 1 else '앞 장소'}에서 직선으로 약 {d:.0f}m, "
            f"걸어서 {max(1, round(d / 80))}분 거리예요.")
        prev = pos
    lines.append(
        f"**코스 구성**: 가까운 장소부터 차례로 이어서 불필요하게 돌아가지 않게 했고, "
        f"마지막에는 출발지로 돌아오는 순환 코스라 다시 이동할 필요가 없어요.")
    diff = res["dur"] / 60 - res["minutes"]
    if abs(diff) <= 10:
        fit = "원하신 시간과 거의 맞아요."
    elif diff > 0:
        fit = "원하신 시간보다 조금 길어요. 장소 수를 줄이면 더 짧아져요."
    else:
        fit = "원하신 시간보다 짧아요. 시간이나 장소 수를 늘려보세요."
    lines.append(f"**시간**: 예상 {res['dur']/60:.0f}분으로, 목표 {res['minutes']}분 대비 {fit}")
    return lines


# ---------------- UI ----------------
st.title("🚶 오늘의 걷기 산책")
st.caption("지금 기분과 내 위치를 바탕으로, 경치 좋은 곳을 이어 산책 코스를 추천해요.")

# 1) 현재 위치
st.subheader("1. 내 위치")
loc = streamlit_geolocation()  # 브라우저 위치 권한 허용 필요
start = None
if loc and loc.get("latitude"):
    start = (loc["latitude"], loc["longitude"])
    st.success("현재 위치를 가져왔어요.")
else:
    st.info("위 버튼으로 위치 권한을 허용하거나, 아래에 주소를 입력하세요.")
    addr = st.text_input("주소 / 장소명으로 대신 찾기")
    if addr:
        start = geocode(addr)
        if not start:
            st.error("주소를 찾지 못했어요.")

# 2) 기분
st.subheader("2. 오늘 기분")
mood = st.radio("기분을 골라주세요", list(MOODS.keys()), horizontal=True)
free = st.text_input("또는 한 줄로 적어보세요 (선택)", placeholder="예: 일이 너무 많아서 머리가 복잡해")
if free:
    guessed = classify_mood_with_claude(free)
    if guessed:
        mood = guessed
        st.write(f"→ 이렇게 느껴져요: **{mood}**")
    else:
        st.caption("ANTHROPIC_API_KEY가 없어서 위에서 고른 기분으로 추천할게요.")

# 3) 옵션
c1, c2 = st.columns(2)
minutes = c1.slider("걷고 싶은 시간(분)", 20, 90, 40, step=10)
n_stops = c2.slider("들를 장소 수", 1, 3, 2)

if st.button("산책 코스 추천받기", type="primary", disabled=start is None):
    radius = int(minutes * 80 / 2.2)  # 보행속도 약 80m/분, 왕복 고려
    cfg = MOODS[mood]
    with st.spinner("근처 예쁜 장소를 찾는 중..."):
        try:
            spots = find_spots(start[0], start[1], radius, tuple(cfg["tags"]))
        except Exception as e:
            st.error(f"장소 검색 실패: {e}")
            st.stop()
    if not spots:
        st.warning("근처에서 장소를 못 찾았어요. 시간을 늘려보세요.")
        st.stop()

    stops = build_course(start, spots, n_stops)
    pts = [start] + [(s["lat"], s["lon"]) for s in stops] + [start]
    with st.spinner("도보 경로 계산 중..."):
        try:
            line, dist, dur = walking_route(tuple(pts))
        except Exception as e:
            st.error(f"경로 계산 실패: {e}")
            st.stop()
    st.session_state["result"] = dict(start=start, stops=stops, line=line,
                                      dist=dist, dur=dur, mood=mood, why=cfg["why"],
                                      minutes=minutes)

res = st.session_state.get("result")
if res:
    st.subheader("추천 코스")
    st.write(f"**{res['mood']}** 인 오늘, {res['why']}을(를) 이어봤어요.")
    m1, m2, m3 = st.columns(3)
    m1.metric("총 거리", f"{res['dist']/1000:.1f} km")
    m2.metric("예상 시간", f"{res['dur']/60:.0f} 분")
    m3.metric("들르는 곳", f"{len(res['stops'])} 곳")
    st.write(" → ".join(["출발"] + [s["name"] for s in res["stops"]] + ["도착"]))

    fmap = folium.Map(location=res["start"], zoom_start=15)
    folium.Marker(res["start"], tooltip="출발/도착", icon=folium.Icon(color="green")).add_to(fmap)
    for i, s in enumerate(res["stops"], 1):
        folium.Marker((s["lat"], s["lon"]), tooltip=f"{i}. {s['name']}",
                      icon=folium.Icon(color="blue")).add_to(fmap)
    folium.PolyLine(res["line"], weight=5, color="#2E86DE").add_to(fmap)
    st_folium(fmap, height=500, use_container_width=True, returned_objects=[])

    st.subheader("💡 이 코스를 추천한 이유")
    for line in explain_course(res):
        st.markdown(f"- {line}")
