"""JSON Schema for structured travel requirements."""

nullable_string = {"type": ["string", "null"]}
nullable_integer = {"type": ["integer", "null"]}
nullable_boolean = {"type": ["boolean", "null"]}

requirement_schema = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "旅行规划需求",
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "destination": {**nullable_string, "title": "目的地", "description": "目的地城市"},     #我们暂时将颗粒度定为城市，后面加上区域的划分
        "origin": {
            **nullable_string,
            "title": "出发地",
            "description": "用户出发的城市；为空表示已在目的地，无需来去程",
        },
        "free_text_requirement": {
            **nullable_string,
            "title": "自由文本备注",
            "description": (
                "C 端备注框的原始文本（可空）。B 入口检测到非空时先经 LLM "
                "解析（parse_requirement_input）把语义并入 tags/constraints/"
                "food_preferences 等结构化字段；解析失败按无备注规划，不阻断。"
                "规划层不消费本字段本身。"
            ),
        },
        "start_date": {**nullable_string, "title": "出发日期", "description": "YYYY-MM-DD"},
        "days": {**nullable_integer, "title": "旅行天数", "minimum": 1},
        "visitor_number": {**nullable_integer, "title": "出行人数", "minimum": 1},
        "travel_schedule": {
            "type": ["object", "null"],
            "additionalProperties": False,
            "properties": {
                "departure_date": {
                    **nullable_string,
                    "title": "去程日期",
                    "description": "标准日期 YYYY-MM-DD，如 2026-08-21；只接受标准日期",
                },
                "departure_time": {
                    **nullable_string,
                    "title": "去程时间",
                    "description": "24 小时制 HH:MM，如 20:00",
                },
                "return_date": {
                    **nullable_string,
                    "title": "返程日期",
                    "description": "标准日期 YYYY-MM-DD，如 2026-08-23",
                },
                "return_time": {
                    **nullable_string,
                    "title": "返程时间",
                    "description": "24 小时制 HH:MM",
                },
            },
            "title": "出行时段",
            "description": "可空；城际来去程的标准日期与时刻（精确到日期和时刻，不接受星期）",
        },
        "hotel_preferences": {
            "type": ["object", "null"],
            "additionalProperties": False,
            "properties": {
                "price_level": {
                    **nullable_string,
                    "title": "价位段",
                    "description": "经济 / 舒适 / 豪华（或同义用户词）",
                },
                "location_preferences": {
                    "type": "array",
                    "items": {"type": "string"},
                    "title": "位置偏好",
                    "description": "如「近地铁」「胡同」「商圈」；没有则为空数组",
                },
                "min_star": {
                    **nullable_integer,
                    "title": "最低星级",
                    "description": "如 4（4 星及以上）",
                    "minimum": 1,
                    "maximum": 5,
                },
            },
            "title": "酒店偏好",
            "description": "可空；选酒店的价位段 / 位置 / 星级偏好",
        },
        "constraints": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "budget": {
                    **nullable_integer,
                    "title": "总预算（元）",
                    "minimum": 0,
                    "description": (
                        "必填。整趟行程的人均总预算（含城际交通/住宿/餐饮/门票）；"
                        "null 或缺失视为未提供，由入口层要求补全（预算参与路线"
                        "选择与费用核算，不能缺省）"
                    ),
                },
                "must_visit": {"type": "array", "items": {"type": "string"}},
                "required_tags": {"type": "array", "items": {"type": "string"}},
                "dismissed_tags": {"type": "array", "items":{"type": "string"}},
                "daily_travel_time": {
                    **nullable_integer,
                    "title": "每日出游时长（分钟）",
                    "description": "每天允许计入时长额度的最大分钟数",
                    "minimum": 1,
                },
                "include_meal_time_in_daily_limit": {
                    **nullable_boolean,
                    "title": "用餐时间是否计入每日出游时长",
                    "description": (
                        "true 表示用餐时间占用 daily_travel_time 额度；"
                        "false 表示用餐仍出现在时间轴中，但不占用该额度"
                    ),
                },
                "walking_time": {**nullable_integer, "title": "单日最大步行时长（分钟）", "minimum": 0},
                "queue_time": {**nullable_integer, "title": "单次最大排队时长（分钟）", "minimum": 0},
            },
            "required": [
                "budget",
                "must_visit",
                "required_tags",
                "dismissed_tags",
                "daily_travel_time",
                "include_meal_time_in_daily_limit",
            ],
        },
        "preferences": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "preferred_tags": {"type": "array", "items": {"type": "string"}},
                "avoid_tags": {"type": "array", "items": {"type": "string"}},
                "food_preferences": {
                    "type": "array",
                    "items": {"type": "string"},
                    "title": "饮食偏好（菜系/忌口）",
                    "description": "可选；用户提到的菜系、菜品或忌口，没有则为空数组",
                },
                "travel_priority": {
                    "type": ["string", "null"],
                    "enum": ["rail", "air", "speed", "earliest", "cost"],
                    "title": "城际交通偏好",
                    "description": "可选；用户对城际来去程的交通偏好（C 端四维：高铁优先/飞机优先/速度最快/最早到达）："
                    "rail=高铁优先（有高铁就用高铁，链式命中 train→air→driving）、"
                    "air=飞机优先（air→train→driving）、"
                    "speed=速度最快（总耗时最短，飞机按净时长+值机缓冲公平比）、"
                    "earliest=最早到达（到达时刻最早；估算表无班次时与 speed 等价，真源班次化后按到达时刻选）、"
                    "cost=越省钱越好（人均费用最低，C 端可选暴露）；没有交通偏好则省略该字段",
                },
            },
            "required": ["preferred_tags", "avoid_tags"],
        },
    },
    "required": ["destination", "start_date", "days", "visitor_number", "constraints", "preferences"],
}
