需要的数据传输格式
用户需求
nullable_string = {"type": ["string", "null"]}
nullable_integer = {"type": ["integer", "null"]}

requirement_schema = {
        "destination": "Beijing",     #我们暂时将颗粒度定为城市，后面加上区域.
      "start_date": "YYYY-MM-DD",
        "days": 2,
        "visitor_number":1,
        "constraints": {
           
                "budget": {**nullable_integer, "title": "总预算（元）", "minimum": 0},
                "must_visit": {"type": "array", "items": {"type": "string"}},
                "required_tags": {"type": "array", "items": {"type": "string"}},
                "dismissed_tags": {"type": "array", "items":{"type": "string"}},
                "walking_time": {**nullable_integer, "title": "单日最大步行时长（分钟）", "minimum": 0},
                "queue_time": {**nullable_integer, "title": "单次最大排队时长（分钟）", "minimum": 0},
            "required": ["budget", "must_visit", "required_tags", "dismissed_tags"],
        },
        "preferences": {
                "preferred_tags": {"type": "array", "items": {"type": "string"}},
                "avoid_tags": {"type": "array", "items": {"type": "string"}},
            "required": ["preferred_tags", "avoid_tags"],
        },
    },
    "required": ["destination", "start_date", "days", "visitor_number", "constraints", "preferences"],
}

LLM由用户的自然语言输入整理而成，发送给筛选景点的python程序
景点信息
{
        "id": "BJ_001",
        "name": "故宫博物院",
        "alias": ["故宫", "紫禁城", "皇宫"],
        "city": "北京",
        "location": {
            "lat": 39.916,
            "lng": 116.397
        },
        "price": 60,
        "duration": 180,
        "opening_time": "08:30",
        "closing_time": "17:00",
        "content_tags": ["历史文化", "古建筑", "皇家文化", "博物馆"],
        "plan_tags": ["顶流热门", "强制预约", "中度1-3h", "低价亲民", "排队时间长", "日间游览"],
        "experience_tags": ["恢弘大气", "历史爱好者", "摄影观景", "震撼出片"],
        "reservation_required": true
    },
通过API获得，储存在知识库中。由筛选景点的python程序调用
路线
{
“id”: “route_001”,
“spots”: [
“spot_001”,
“spot_002”,
“spot_003”
],
“segments”: [
{
“from”: “spot_001”,
“to”: “spot_002”,
“transport”: “walk”,
“distance”: 1.2,
“duration”: 15
},
{
“from”: “spot_002”,
“to”: “spot_003”,
“transport”: “subway”,
“duration”: 20,
“cost”: 3
}
],
“total_duration”: 240,
“total_transport_cost”: 3
}
python接受需求后生成备选路线，发送给LLM比较，LLM输出最优
计划
{
“id”: “plan_001”,
“days”: [
{
“date”: “2026-08-10”,

  "activities": [
    {
      "spot_id": "spot_001",
      "start": "09:00",
      "end": "12:00"
    },
    {
      "spot_id": "spot_002",
      "start": "13:00",
      "end": "14:30"
    }
  ]
}

],
“total_cost”: 780,
“walking_distance”: 4.2
}
LLM组合最优路线得到计划，输出给python程序检查是否存在时间冲突、预算超支等硬伤。检查不通过重新组合，通过则输出到前端
事件变化
{
“type”: “QUEUE_CHANGE”,
“timestamp”: “2026-08-10T10:20:00”,
“spot_id”: “spot_001”,
“data”: {
“old_value”: 20,
“new_value”: 120
}
}
python首先判断是否有必要发送LLM（硬约束不满足无须发送存在，直接重新规划；影响太小无须发送，可忽略），再执行
决定
{
“event_id”: “event_001”,
“need_replan”: true,
“impact”: 0.91,
“reason”: “排队时间显著增加，与用户避免长时间排队的偏好冲突”,
“affected_spots”: [
“spot_001”
]
}
LLM判断是否需要重新规划，是则从候选路线中选择组合
行为
{
“id”: “action_001”,
“type”: “BOOK_TICKET”,
“target”: “spot_001”,
“date”: “2026-08-10”,
“quantity”: 2,
“requires_confirmation”: true,
“status”: “WAITING_CONFIRMATION”
}
python发送给前端等待用户确认，确认后发送给负责booking等行动的任务
用户画像（优先级低）
{
“user_id”: “user_001”,
“preferences”: {
“walking”: “low”,
“crowd”: “low”,
“interests”: [
“历史”,
“摄影”
]
}
}
从需求总结，储存在知识库。

