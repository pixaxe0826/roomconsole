import httpx
from .store import utcnow
async def fetch_weather(settings):
 if settings['latitude'] is None:return None
 params={'latitude':settings['latitude'],'longitude':settings['longitude'],'timezone':settings['timezone'],
 'current':'temperature_2m,relative_humidity_2m,apparent_temperature,is_day,precipitation,weather_code,wind_speed_10m',
 'daily':'weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max','forecast_days':7,'wind_speed_unit':'ms'}
 async with httpx.AsyncClient(timeout=18) as client:
  r=await client.get('https://api.open-meteo.com/v1/forecast',params=params);r.raise_for_status();payload=r.json()
 if not isinstance(payload.get('current'),dict) or not isinstance(payload.get('daily'),dict):raise ValueError('날씨 응답 형식 오류')
 return {'provider':'Open-Meteo','fetched_at':utcnow(),'location_name':settings['location_name'],'coordinates':[settings['latitude'],settings['longitude']],'current':payload['current'],'daily':payload['daily'],'error':None}
