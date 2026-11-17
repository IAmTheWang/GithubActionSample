# 安装依赖 pip3 install requests schedule
import os
import requests
import json
import time

# 所有外部请求统一的超时(秒)和重试次数，避免第三方接口卡住导致整个任务失败/挂起
REQUEST_TIMEOUT = 10
REQUEST_RETRIES = 3


def _request(method, url, **kwargs):
    last_err = None
    for attempt in range(1, REQUEST_RETRIES + 1):
        try:
            resp = requests.request(method, url, timeout=REQUEST_TIMEOUT, **kwargs)
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            last_err = e
            print(f"Request failed ({attempt}/{REQUEST_RETRIES}): {url.split('?')[0]} -> {e}")
            if attempt < REQUEST_RETRIES:
                time.sleep(2 * attempt)
    raise last_err


# 从测试号信息获取
appID = os.environ.get("APP_ID")
appSecret = os.environ.get("APP_SECRET")
# 收信人ID即 用户列表中的微信号
openId = os.environ.get("OPEN_ID")
# 天气预报模板ID
weather_template_id = os.environ.get("TEMPLATE_ID")

# 气象厅(JMA)预报接口是按都道府県分区的，这里维护 城市 -> 所属地域/代表观测点 的映射
# 城市所属地域(region_name)决定天气/风向，代表观测点(temp_station)决定气温，lat/lon用于查询紫外线指数
# region_name/temp_station 必须是JMA接口返回的原始日语地名，用于匹配；display_name是推送消息里展示的英文名
CITY_AREA_MAP = {
    "市川市": {"area_code": "120000", "region_name": "北西部", "temp_station": "千葉", "lat": 35.7217, "lon": 139.9310, "display_name": "Ichikawa"},
}

# JMA天气/风向原文是日语，按长词优先的顺序转换成英语单词
WEATHER_TERM_TRANSLATIONS = {
    "晴れ": "Sunny",
    "くもり": "Cloudy",
    "曇り": "Cloudy",
    "曇": "Cloudy",
    "晴": "Sunny",
    "雨": "Rain",
    "雪": "Snow",
    "時々": " Occasionally ",
    "一時": " Temporarily ",
    "のち": " Then ",
    "後": " Then ",
    "夜のはじめ頃": " Early Evening ",
    "夜遅く": " Late Night ",
    "未明": " Before Dawn ",
    "昼過ぎ": " Afternoon ",
    "昼前": " Before Noon ",
    "夕方": " Evening ",
    "朝": " Morning ",
    "夜": " At Night ",
}
WIND_TERM_TRANSLATIONS = {
    "北東": "Northeast",
    "北西": "Northwest",
    "南東": "Southeast",
    "南西": "Southwest",
    "北": "North",
    "南": "South",
    "東": "East",
    "西": "West",
    "やや強く": " Somewhat Strong",
    "強く": " Strong",
    "弱く": " Weak",
    "風": " Wind",
    "の": " ",
}


def _translate(text, term_map):
    for jp, en in term_map.items():
        text = text.replace(jp, en)
    return " ".join(text.split())


def get_weather(my_city):
    city_info = CITY_AREA_MAP.get(my_city)
    if city_info is None:
        raise ValueError(f"Unsupported city: {my_city}, please add a JMA region mapping in CITY_AREA_MAP first")

    url = f"https://www.jma.go.jp/bosai/forecast/data/forecast/{city_info['area_code']}.json"
    forecast = _request("GET", url).json()[0]

    region_series = forecast["timeSeries"][0]
    region = next(a for a in region_series["areas"] if a["area"]["name"] == city_info["region_name"])
    weather_typ = _translate(region["weathers"][0].replace("　", ""), WEATHER_TERM_TRANSLATIONS)
    wind = _translate(region["winds"][0].replace("　", ""), WIND_TERM_TRANSLATIONS)

    temp_series = forecast["timeSeries"][2]
    station = next(a for a in temp_series["areas"] if a["area"]["name"] == city_info["temp_station"])
    temps = sorted(int(t) for t in station["temps"] if t)
    if not temps:
        temp = "Temperature data unavailable"
    elif len(temps) >= 2:
        temp = f"{temps[0]}-{temps[-1]}C"
    else:
        temp = f"{temps[0]}C"

    return city_info["display_name"], temp, weather_typ, wind


def get_uv_index(my_city):
    city_info = CITY_AREA_MAP.get(my_city)
    if city_info is None:
        raise ValueError(f"Unsupported city: {my_city}, please add lat/lon to CITY_AREA_MAP first")

    url = (f"https://api.open-meteo.com/v1/forecast?latitude={city_info['lat']}&longitude={city_info['lon']}"
           "&hourly=uv_index&timezone=Asia%2FTokyo&forecast_days=1")
    hourly = _request("GET", url).json()["hourly"]

    # 只展示白天时段(6:00-18:00)的逐时紫外线指数，夜间恒为0没有展示意义
    parts = []
    for time_str, uv in zip(hourly["time"], hourly["uv_index"]):
        hour = int(time_str.split("T")[1].split(":")[0])
        if 6 <= hour <= 18:
            parts.append(f"{hour:02d}:00 UV{round(uv)}")
    return " ".join(parts)


def get_access_token():
    # 获取access token的url
    url = 'https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential&appid={}&secret={}' \
        .format(appID.strip(), appSecret.strip())
    response = _request("GET", url).json()
    print(response)
    access_token = response.get('access_token')
    return access_token


def get_daily_quote():
    # 每日一句英文名言
    url = "https://zenquotes.io/api/random"
    try:
        quote = _request("GET", url).json()[0]
        return f"{quote['q']} — {quote['a']}"
    except Exception as e:
        # 每日一句只是装饰信息，获取失败不应影响天气推送
        print(f"Failed to get daily quote: {e}")
        return "N/A"


def send_weather(access_token, weather, uv_index):
    # touser 就是 openID
    # template_id 就是模板ID
    # url 就是点击模板跳转的url
    # data就按这种格式写，time和text就是之前{{time.DATA}}中的那个time，value就是你要替换DATA的值

    import datetime
    today = datetime.date.today()
    today_str = today.strftime("%Y-%m-%d")

    body = {
        "touser": openId.strip(),
        "template_id": weather_template_id.strip(),
        "url": "https://weixin.qq.com",
        "data": {
            "date": {
                "value": today_str
            },
            "region": {
                "value": weather[0]
            },
            "weather": {
                "value": weather[2]
            },
            "temp": {
                "value": weather[1]
            },
            "wind_dir": {
                "value": weather[3]
            },
            "uv_index": {
                "value": uv_index
            },
            "today_note": {
                "value": get_daily_quote()
            }
        }
    }
    url = 'https://api.weixin.qq.com/cgi-bin/message/template/send?access_token={}'.format(access_token)
    print(_request("POST", url, data=json.dumps(body)).text)



def weather_report(this_city):
    # 1.获取access_token
    access_token = get_access_token()
    # 2. 获取天气
    weather = get_weather(this_city)
    try:
        uv_index = get_uv_index(this_city)
    except Exception as e:
        # 紫外线指数只是附加信息，获取失败时仍然推送天气
        print(f"Failed to get UV index: {e}")
        uv_index = "N/A"
    print(f"Weather info: {weather}, UV index: {uv_index}")
    # 3. 发送消息
    send_weather(access_token, weather, uv_index)



if __name__ == '__main__':
    weather_report("市川市")