"""餐厅选择器：给一顿饭（锚定在某个景点）挑一家餐厅。

规则：
- 用户有饮食偏好（food_preferences）时，优先匹配菜系/招牌，再按交通时间就近；
- 没有饮食偏好时，直接选距离锚定景点最近的餐厅。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

try:
    from data_transmission.city_graph import DEFAULT_GRAPH_DIR
    from data_transmission.restaurant import Restaurant, load_restaurants
except ModuleNotFoundError:
    from ..data_transmission.city_graph import DEFAULT_GRAPH_DIR
    from ..data_transmission.restaurant import Restaurant, load_restaurants

from .providers import JsonTravelTimeProvider, TravelEdge, TravelTimeProvider


class RestaurantResolver:
    """为路线时间轴挑选餐厅，并提供餐厅与景点之间的交通边。"""

    def __init__(
        self,
        city: str,
        food_preferences: Sequence[str] = (),
        travel_time_provider: Optional[TravelTimeProvider] = None,
        data_dir: Path = DEFAULT_GRAPH_DIR,
        restaurant_provider: Optional[Callable[[str], List[Restaurant]]] = None,
    ):
        """餐厅选择器。

        ``travel_time_provider``：缺省 ``JsonTravelTimeProvider``（spots_graph 假边）；
        真源模式下传入 ``LiveTravelTimeProvider``（batch_route 矩阵）→ 餐厅↔景点通勤
        由矩阵提供（8.28：真源餐厅坐标须并入矩阵的 ``name_to_coord``）。
        ``restaurant_provider``：``fn(city) -> List[Restaurant]``，缺省 ``load_restaurants``
        （假池）；真实数据接入时由 ``data_transmission.live_data.make_live_restaurants_provider`` 注入。
        """
        self.city = city
        if restaurant_provider is not None:
            self.restaurants = list(restaurant_provider(city))
        else:
            self.restaurants = load_restaurants(city, data_dir)
        self.food_preferences = [
            str(item).strip() for item in food_preferences if str(item).strip()
        ]
        self.provider = travel_time_provider or JsonTravelTimeProvider(city, data_dir)
        self._by_id = {restaurant.id: restaurant for restaurant in self.restaurants}

    def is_restaurant(self, node_id: str) -> bool:
        return str(node_id) in self._by_id

    def name_of(self, node_id: str) -> str:
        restaurant = self._by_id.get(str(node_id))
        return restaurant.name if restaurant is not None else str(node_id)

    def travel_edge(self, origin_id: str, destination_id: str) -> Optional[TravelEdge]:
        origin_id, destination_id = str(origin_id), str(destination_id)
        if origin_id == destination_id:
            return None
        try:
            return self.provider.get_edge(origin_id, destination_id)
        except (ValueError, KeyError):
            return None

    def travel_minutes(self, origin_id: str, destination_id: str) -> int:
        edge = self.travel_edge(origin_id, destination_id)
        return edge.transport_minutes if edge is not None else 0

    def select(self, anchor_spot_id: str) -> Optional[Restaurant]:
        """为锚定在 ``anchor_spot_id`` 的一顿饭选餐厅。"""
        if not self.restaurants:
            return None
        anchor = str(anchor_spot_id)

        def rank(restaurant: Restaurant) -> Tuple:
            minutes = self.travel_minutes(anchor, restaurant.id)
            if self.food_preferences:
                score = self._food_score(restaurant)
                return (-score, minutes, restaurant.id)
            return (minutes, restaurant.id)

        return min(self.restaurants, key=rank)

    def _food_score(self, restaurant: Restaurant) -> int:
        tags = [tag.lower() for tag in (*restaurant.cuisine_tags, *restaurant.signature_tags)]
        preferences = [preference.lower() for preference in self.food_preferences]
        return sum(
            1
            for preference in preferences
            for tag in tags
            if preference in tag or tag in preference
        )
