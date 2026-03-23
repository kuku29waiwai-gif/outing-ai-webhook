#!/usr/bin/env python3
"""
おでかけAI - LINE Webhook サーバー
地名を送信すると当日の天気情報を返信する
"""

import os
import time
import requests
from datetime import datetime
from flask import Flask, request, abort
import pytz
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

app = Flask(__name__)

# 環境変数から認証情報を取得
CHANNEL_SECRET = os.environ.get("CHANNEL_SECRET", "")
CHANNEL_ACCESS_TOKEN = os.environ.get("CHANNEL_ACCESS_TOKEN", "")

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

TIMEZONE = "Asia/Tokyo"


# ──────────────────────────────────────────
# 天気取得ロジック
# ──────────────────────────────────────────

def _geocode_nominatim(place_name: str):
    """Nominatim APIで地名を検索する（日本語地名対応）"""
    url = "https://nominatim.openstreetmap.org/search"
    params = {
        "q": place_name,
        "format": "json",
        "limit": 1,
        "countrycodes": "jp",
        "accept-language": "ja",
    }
    headers = {"User-Agent": "OutingAI-WeatherBot/1.0"}
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=10)
            if r.status_code == 429:
                time.sleep(3 * (attempt + 1))  # 3秒 → 6秒 → 9秒
                continue
            if r.status_code != 200:
                return None
            results = r.json()
            if not results:
                return None
            result = results[0]
            return {
                "latitude": float(result["lat"]),
                "longitude": float(result["lon"]),
                "name": result.get("display_name", place_name).split(",")[0].strip(),
            }
        except Exception:
            return None
    return None  # リトライ上限


def _geocode_openmeteo(place_name: str):
    """Open-Meteo Geocoding APIで地名を検索する（英語・ローマ字対応）"""
    url = "https://geocoding-api.open-meteo.com/v1/search"
    params = {
        "name": place_name,
        "count": 5,
        "language": "ja",
        "format": "json",
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        if r.status_code != 200:
            return None
        data = r.json()
        results = data.get("results", [])
        # 日本のみに絞る
        jp_results = [res for res in results if res.get("country_code") == "JP"]
        if not jp_results:
            return None
        result = jp_results[0]
        name = result.get("name", place_name)
        admin1 = result.get("admin1", "")
        display = f"{name}（{admin1}）" if admin1 else name
        return {
            "latitude": float(result["latitude"]),
            "longitude": float(result["longitude"]),
            "name": display,
        }
    except Exception:
        return None


def geocode(place_name: str):
    """地名から緯度経度を取得する（Nominatim優先、失敗時はOpen-Meteoにフォールバック）"""
    # まずNominatimで試す（日本語地名に強い）
    result = _geocode_nominatim(place_name)
    if result:
        return result
    # フォールバック: Open-Meteo Geocoding API
    return _geocode_openmeteo(place_name)


def get_weather(lat: float, lon: float) -> dict:
    """Open-Meteo APIから当日の天気を取得する"""
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": ["temperature_2m", "precipitation"],
        "timezone": TIMEZONE,
        "forecast_days": 2,
    }
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def parse_weather(data: dict) -> dict:
    """当日8〜22時の気温・雨情報を抽出する"""
    jst = pytz.timezone(TIMEZONE)
    today = datetime.now(jst).date()

    times = data["hourly"]["time"]
    temps = data["hourly"]["temperature_2m"]
    precips = data["hourly"]["precipitation"]

    target_temps = []
    rain_hours = []

    for i, t_str in enumerate(times):
        dt = datetime.fromisoformat(t_str)
        if dt.date() != today:
            continue
        hour = dt.hour
        if 8 <= hour <= 22:
            target_temps.append(temps[i])
            if precips[i] > 0:
                rain_hours.append(hour)

    if not target_temps:
        return {}

    min_temp = min(target_temps)
    max_temp = max(target_temps)
    avg_temp = round(sum(target_temps) / len(target_temps), 1)

    # 連続する雨の時間帯をまとめる
    rain_ranges = []
    if rain_hours:
        rain_hours_sorted = sorted(set(rain_hours))
        start = end = rain_hours_sorted[0]
        for h in rain_hours_sorted[1:]:
            if h == end + 1:
                end = h
            else:
                rain_ranges.append((start, end))
                start = end = h
        rain_ranges.append((start, end))

    return {
        "date": today.strftime("%Y年%m月%d日"),
        "min_temp": min_temp,
        "max_temp": max_temp,
        "avg_temp": avg_temp,
        "rain_ranges": rain_ranges,
    }


def format_message(place_name: str, info: dict) -> str:
    """返信メッセージを生成する"""
    lines = []
    lines.append(f"📅 {info['date']} {place_name}の天気")

    if info["rain_ranges"]:
        rain_parts = []
        for start, end in info["rain_ranges"]:
            if start == end:
                rain_parts.append(f"{start}時頃")
            else:
                rain_parts.append(f"{start}〜{end + 1}時")
        lines.append("🌧 雨: " + "、".join(rain_parts))
        lines.append("☂️ 傘を持っていきましょう")
    else:
        lines.append("☀️ 雨の予報なし")

    lines.append(f"🌡 8〜22時: {info['min_temp']}°C / {info['max_temp']}°C（平均 {info['avg_temp']}°C）")
    return "\n".join(lines)


# ──────────────────────────────────────────
# Webhook エンドポイント
# ──────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    place_name = event.message.text.strip()

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)

        # 地名をジオコーディング
        location = geocode(place_name)
        if not location:
            reply_text = f"「{place_name}」の場所が見つかりませんでした。\n別の地名を試してみてください。"
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_text)],
                )
            )
            return

        # 天気取得
        weather_data = get_weather(location["latitude"], location["longitude"])
        info = parse_weather(weather_data)

        if not info:
            reply_text = "天気情報を取得できませんでした。しばらくしてからもう一度お試しください。"
        else:
            # 表示用の地名（APIが返す名前を優先）
            display_name = location.get("name", place_name)
            reply_text = format_message(display_name, info)

        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_text)],
            )
        )


@app.route("/", methods=["GET"])
def health():
    return "おでかけAI Webhook サーバー 稼働中", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
