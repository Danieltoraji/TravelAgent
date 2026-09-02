import json
import math
import sys
from pathlib import Path
from typing import Callable, Optional
from rapidfuzz import fuzz

current_file=Path(__file__)
project_root=current_file.parent.parent
sys.path.insert(0,str(project_root))

from data_transmission.requirement import requirement_schema
from data_transmission.city_graph import match_city_spots, normalize_city_name
from algorithoms.spot_assignment import _haversine_km, _spot_coord

# 9.2 十一节层二：rating 质量评分分档（真源 POI 评分；假池无 rating 字段 → 0 → 零回归）
_RATING_TIERS = (
    (4.8, 6),
    (4.5, 4),
    (4.0, 2),
)
_RATING_LOW_BAD = 3.5   # 0 < rating < 3.5 主动压掉
_RATING_LOW_PENALTY = 5
# 9.2 十一节层三：距池质心距离惩罚（仅真源池启用）——>25km 每超 10km 减 2 分
_DISTANCE_PENALTY_THRESHOLD_KM = 25.0
_DISTANCE_PENALTY_PER_10KM = 2

def load_spot_json(city: str | None = None, file_path=None) -> list:
    """Load attractions for one city from its city data directory."""
    if file_path is None:
        if city is None:
            raise ValueError("city 和 file_path 至少需要提供一个")
        file = match_city_spots(city)
    else:
        file = Path(file_path)
    if not file.exists():
        raise FileNotFoundError(f"文件不存在：{file}")
    else:
        with open(file, "r", encoding="utf-8-sig") as f:
            spot_data = json.load(f)  # 直接转 Python list/dict
        if not isinstance(spot_data, list):
            raise ValueError(f"景点数据必须是 JSON 数组：{file}")
        return spot_data

def match_name(spot,name):
    if(spot["name"]==name):
        return 1        #1代表相等
    if(name in set(spot["alias"])):     #我们先认为这一层过滤掉所有简称，下一层用来处理长城与八达岭长城这样的包含关系
        return 1
    if(fuzz.partial_ratio(name,spot["name"])==100 and fuzz.ratio(name,spot["name"])<=60):
        return 2        #2代表包含
    elif(fuzz.ratio(name,spot["name"])>75):
        return 1
    # 8.30 demo1 复探：分段命中——「山丹马场」vs「山丹皇家马场景区」ratio 66.7
    # 差一点不到 75，但 must 名按 2 字以上切点拆两段后两段都连续出现在池名里
    # （山丹 + 马场），语义上明确是同一景点。仅对长度 ≥4 的 must 名启用，
    # 两段各 ≥2 字防碎片误命中。
    if len(name) >= 4:
        pool_name = spot["name"]
        for cut in range(2, len(name) - 1):
            head, tail = name[:cut], name[cut:]
            if len(tail) >= 2 and head in pool_name and tail in pool_name:
                return 2
    return 0

def _confirm_conflict_spot(
    spot,
    conflicting_tags,
    user_input_fn: Callable[[str], str],
):
    risks = [f"命中您明确排除的标签：{'、'.join(sorted(conflicting_tags))}"]
    if spot.get("reservation_required"):
        risks.append("需要提前预约，可能存在无票或预约失败风险")
    if "排队时间长" in set(spot.get("plan_tags", [])):
        risks.append("可能需要较长时间排队")

    prompt = (
        f"必去景点“{spot['name']}”可能存在以下风险："
        + "；".join(risks)
        + "。是否仍然保留该景点？[y/N]："
    )
    answer = user_input_fn(prompt).strip().lower()
    return answer in {"y", "yes", "是", "保留", "仍然保留"}


def select_spots(
    requirement,
    min_threshold=0,
    ask_user_on_conflict=True,
    user_input_fn: Optional[Callable[[str], str]] = None,
    spots_provider: Optional[Callable[[str], list]] = None,
):
    """挑选景点（must / conflict / scored 三类）。

    ``spots_provider``：``fn(city) -> List[spot dict]``（与 ``load_spot_json`` 同契约，
    即原始候选数组）；缺省读 ``fake_spots/`` 假数据。真实数据接入时由
    ``data_transmission.live_data.LiveSpotsSource`` 注入（USE_LIVE_DATA=1）。
    """
    user_input_fn = user_input_fn or input
    target_city=requirement["content"]["destination"]
    normalized_target_city = normalize_city_name(target_city)
    if spots_provider is not None:
        spots = spots_provider(target_city)
        if not isinstance(spots, list):
            raise ValueError(
                f"spots_provider 必须返回景点数组（list），实际 {type(spots).__name__}"
            )
    else:
        spots = load_spot_json(city=target_city)
    preferred_tags=set(requirement["content"]["preferences"]["preferred_tags"])
    avoid_tags=set(requirement["content"]["preferences"]["avoid_tags"])
    required_tags=set(requirement["content"]["constraints"]["required_tags"])
    dismissed_tags=set(requirement["content"]["constraints"]["dismissed_tags"])
    must_visits=requirement["content"]["constraints"]["must_visit"]

    # 9.2 十一节层三：距池质心距离惩罚——仅真源池启用（池内存在 rating 字段；
    # 假池 spots.json 无 rating → 距离惩罚零回归）。锚点取池内全部景点质心
    # （零额外请求，不调高德）；与打分循环一致只统计目标城市景点。
    _live_pool = any("rating" in spot for spot in spots)
    _pool_centroid = None
    if _live_pool:
        coords = [
            c
            for s in spots
            if normalize_city_name(s.get("city", "")) == normalized_target_city
            and (c := _spot_coord(s)) is not None
        ]
        if coords:
            _pool_centroid = (
                sum(c[0] for c in coords) / len(coords),
                sum(c[1] for c in coords) / len(coords),
            )

    def spot_tag_union(spot):
        return set(spot["content_tags"])|set(spot["plan_tags"])|set(spot["experience_tags"])

    scored_spots=[]
    must_spots=[]
    conflict_spots=[]
    must_keys=set()

    # required 冲突预检：若某 required 标签的「所有命中景点」都被 dismissed 排除，
    # 记下代表景点（命中 required 标签数多的优先），主循环里进入冲突询问；
    # 其余情况（部分被排除）丢弃被排除的、保留未被排除的进替代组。
    required_conflict_reps = {}
    for tag in required_tags:
        kept_hits = []
        dismissed_hits = []
        for spot in spots:
            if normalize_city_name(spot["city"]) != normalized_target_city:
                continue
            if tag in spot_tag_union(spot):
                if spot_tag_union(spot) & dismissed_tags:
                    dismissed_hits.append(spot)
                else:
                    kept_hits.append(spot)
        if not kept_hits and dismissed_hits:
            required_conflict_reps[tag] = max(
                dismissed_hits,
                key=lambda spot: (
                    len(spot_tag_union(spot) & required_tags),
                    str(spot.get("id") or spot.get("name")),
                ),
            )

    for spot in spots:
        if(normalize_city_name(spot["city"]) != normalized_target_city):
            continue
        spot_tags = spot_tag_union(spot)
        spot_key = str(spot.get("id") or spot.get("name"))

        #判断景点是不是must_visit
        for name in requirement["content"]["constraints"]["must_visit"]:
            score=match_name(spot=spot,name=name)
            if(score==1):
                candidate_spot=spot.copy()
                candidate_spot["dependency"]=False
                candidate_spot["dependency_group"] = None
                candidate_spot["must_visit_source"] = name
                if(spot_tag_union(candidate_spot)&dismissed_tags):
                    conflict_spots.append(candidate_spot)
                else:
                    must_spots.append(candidate_spot)
                    must_keys.add(spot_key)
                continue
            elif(score==2):
                candidate_spot=spot.copy()
                candidate_spot["dependency"]=True
                # Several concrete attractions may satisfy one fuzzy must-visit
                # request (for example “长城”). They are alternatives, not
                # separate mandatory visits.
                candidate_spot["dependency_group"] = name
                candidate_spot["must_visit_source"] = name
                if(spot_tag_union(candidate_spot)&dismissed_tags):
                    conflict_spots.append(candidate_spot)
                else:
                    must_spots.append(candidate_spot)
                    must_keys.add(spot_key)
                continue

        # required 标签：命中且未被排除 → 作为「必去类别」的替代组进 must
        # （组内所有命中者都进 must，beam 分配时选一个代表进日程——硬约束）
        hit_required = spot_tags & required_tags
        if hit_required and not (spot_tags & dismissed_tags):
            if spot_key not in must_keys:
                candidate_spot = spot.copy()
                candidate_spot["dependency"] = True
                candidate_spot["dependency_group"] = "required:" + sorted(hit_required)[0]
                candidate_spot["required_tag_source"] = sorted(hit_required)[0]
                must_spots.append(candidate_spot)
                must_keys.add(spot_key)
            continue

        if hit_required and (spot_tags & dismissed_tags):
            # 命中但被排除：仅当该标签「全部命中者都被排除」时询问代表；其余丢弃
            for tag in sorted(hit_required):
                rep = required_conflict_reps.get(tag)
                if rep is not None and str(rep.get("id") or rep.get("name")) == spot_key:
                    candidate_spot = spot.copy()
                    candidate_spot["dependency"] = True
                    candidate_spot["dependency_group"] = "required:" + tag
                    candidate_spot["required_tag_source"] = tag
                    conflict_spots.append(candidate_spot)
                    break
            continue

        #判断景点该不该被排除
        if(spot_tags&dismissed_tags):
            continue

        #可以打分的景点
        score = 0
        # 1. required标签加分：每个匹配+10
        match_required = spot_tags & required_tags
        score += len(match_required) * 10

        # 2. preferred标签加分：每个匹配+3
        match_preferred = spot_tags & preferred_tags
        score += len(match_preferred) * 3

        # 3. avoid标签扣分：每个匹配-8
        match_avoid = spot_tags & avoid_tags
        score -= len(match_avoid) * 8

        # 4. rating 质量评分（9.2 十一节层二）：真源 POI 评分入分——
        #    ≥4.8 → +6；≥4.5 → +4；≥4.0 → +2；0<rating<3.5 → −5；
        #    无评分（rating=0，含假池无该字段）不加不罚 → 假数据零回归。
        rating = float(spot.get("rating", 0) or 0)
        if rating >= _RATING_TIERS[0][0]:
            score += _RATING_TIERS[0][1]
        elif rating >= _RATING_TIERS[1][0]:
            score += _RATING_TIERS[1][1]
        elif rating >= _RATING_TIERS[2][0]:
            score += _RATING_TIERS[2][1]
        elif 0 < rating < _RATING_LOW_BAD:
            score -= _RATING_LOW_PENALTY

        # 5. 距池质心距离惩罚（9.2 十一节层三，仅真源池）：>25km 每超 10km 减 2 分；
        #    must_visit 硬约束在 must 分支已被拦下（走 must_spots），此处天然豁免。
        if _live_pool and _pool_centroid is not None:
            coord = _spot_coord(spot)
            if coord is not None:
                dist_km = _haversine_km(coord, _pool_centroid)
                if dist_km > _DISTANCE_PENALTY_THRESHOLD_KM:
                    score -= _DISTANCE_PENALTY_PER_10KM * math.ceil(
                        (dist_km - _DISTANCE_PENALTY_THRESHOLD_KM) / 10
                    )

        # 6. 分数达标才保留，存入分数
        if score >= min_threshold:
            spot_with_score = spot.copy()  # 复制原字典，不修改原始数据
            spot_with_score["match_score"] = score
            scored_spots.append(spot_with_score)

    # 按分数从高到低排序，同分可按价格/名称二次排序
    scored_spots.sort(key=lambda x: x["match_score"], reverse=True)
    # A must-visit attraction can conflict with the user's hard exclusions.
    # Ask once per attraction whether it should override those exclusions.
    unique_conflicts = {
        str(spot.get("id") or spot.get("name")): spot for spot in conflict_spots
    }
    unresolved_conflicts = []
    must_keys = {str(spot.get("id") or spot.get("name")) for spot in must_spots}
    for key, spot in unique_conflicts.items():
        conflicting_tags = spot_tag_union(spot) & dismissed_tags
        keep = False
        if ask_user_on_conflict:
            keep = _confirm_conflict_spot(spot, conflicting_tags, user_input_fn)
        if keep:
            if key not in must_keys:
                must_spots.append(spot)
                must_keys.add(key)
        else:
            conflict_spot = spot.copy()
            conflict_spot["conflicting_tags"] = sorted(conflicting_tags)
            unresolved_conflicts.append(conflict_spot)

    return [must_spots,unresolved_conflicts,scored_spots]   
