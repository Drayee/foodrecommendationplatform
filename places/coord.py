"""坐标转换与几何计算。

关键事实
- 浏览器 `navigator.geolocation` 返回 **WGS-84**；
- 高德（Amap）全系使用 **GCJ-02**（火星坐标）；
- 直接拿 WGS-84 去查周边，城区会偏移 100~700 米，"最近的店"可能指向隔壁街区。

因此：前端上报 WGS-84 → 服务端转 GCJ-02 再查高德；返回给前端展示时统一用 GCJ-02
（地图与 POI 同一坐标系才不会错位）。
"""
from __future__ import annotations

import math

# Krasovsky 1940 椭球参数
A = 6378245.0
EE = 0.00669342162296594323
EARTH_RADIUS_M = 6371008.8


def out_of_china(lng: float, lat: float) -> bool:
    """粗略判断是否在中国境外（境外不做 GCJ-02 偏移）。"""
    return not (72.004 <= lng <= 137.8347 and 0.8293 <= lat <= 55.8271)


def _transform_lat(x: float, y: float) -> float:
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lng(x: float, y: float) -> float:
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def wgs84_to_gcj02(lng: float, lat: float) -> tuple[float, float]:
    """WGS-84 → GCJ-02（浏览器定位 → 高德）。"""
    if out_of_china(lng, lat):
        return lng, lat
    dlat = _transform_lat(lng - 105.0, lat - 35.0)
    dlng = _transform_lng(lng - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = math.sin(radlat)
    magic = 1 - EE * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((A * (1 - EE)) / (magic * sqrtmagic) * math.pi)
    dlng = (dlng * 180.0) / (A / sqrtmagic * math.cos(radlat) * math.pi)
    return round(lng + dlng, 6), round(lat + dlat, 6)


def gcj02_to_wgs84(lng: float, lat: float) -> tuple[float, float]:
    """GCJ-02 → WGS-84（近似逆解，误差 < 1m，足够展示用）。"""
    if out_of_china(lng, lat):
        return lng, lat
    # 用正向偏移做一次反推
    wlng, wlat = lng, lat
    for _ in range(3):
        glng, glat = wgs84_to_gcj02(wlng, wlat)
        wlng += lng - glng
        wlat += lat - glat
    return round(wlng, 6), round(wlat, 6)


def haversine_m(lng1: float, lat1: float, lng2: float, lat2: float) -> float:
    """两点球面距离（米）。"""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def grid_key(lng: float, lat: float, precision: int = 3) -> str:
    """把坐标量化成网格串用于缓存键（precision=3 约 100m 一格）。"""
    return f"{round(lng, precision)}:{round(lat, precision)}"


def format_distance(meters: float) -> str:
    """距离的人类可读格式。"""
    try:
        value = float(meters)
    except (TypeError, ValueError):
        return ""
    if value < 1000:
        return f"{int(round(value))}m"
    return f"{value / 1000:.1f}km"
