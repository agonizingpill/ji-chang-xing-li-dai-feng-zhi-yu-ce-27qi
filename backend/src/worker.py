import json, os, time
from pathlib import Path
import redis
from .db import connect, migrate, wait_for_db
from .algorithm import stl_rolling_forecast
from . import congestion_watch

REDIS_URL=os.environ['REDIS_URL']; QUEUE='airport:forecast'
r=redis.Redis.from_url(REDIS_URL, decode_responses=True)

def process(forecast_id):
    # 第一段事务：只负责抢占任务并置为 running，随后立即提交，
    # 避免长时间计算持有行锁，保证主管在计算期间可以跳过该申请。
    with connect() as conn:
        row=conn.execute('SELECT * FROM peak_forecasts WHERE id=%s FOR UPDATE',(forecast_id,)).fetchone()
        if not row or row['status'] not in ('queued','failed'): return
        conn.execute("UPDATE peak_forecasts SET status='running', attempts=attempts+1, updated_at=now() WHERE id=%s", (forecast_id,))
        wave_id=row['wave_id']; window_size=row['window_size']
        conn.commit()

    # 计算在事务外进行，不持有任何行锁
    try:
        with connect() as conn:
            points=conn.execute("SELECT counted_at,bag_count FROM belt_counts WHERE wave_id=%s AND status='accepted' ORDER BY counted_at", (wave_id,)).fetchall()
        result=stl_rolling_forecast(points, window_size)
    except Exception as exc:
        with connect() as conn:
            conn.execute("UPDATE peak_forecasts SET status='failed', failure_reason=%s, updated_at=now() WHERE id=%s AND status='running'", (str(exc), forecast_id))
            conn.commit()
        return

    # 第二段事务：重新取锁确认当前状态；若期间被主管跳过，迟到结果只留档
    with connect() as conn:
        latest=conn.execute('SELECT * FROM peak_forecasts WHERE id=%s FOR UPDATE',(forecast_id,)).fetchone()
        if not latest: return
        if latest['status']=='skipped':
            conn.execute("UPDATE peak_forecasts SET status='archived', peak_bags=%s, forecast_at=now(), seasonal_strength=%s, trend_per_hour=%s, snapshot_hash=%s, parameters=%s::jsonb, result=%s::jsonb, updated_at=now() WHERE id=%s", (result['peak_bags'],result['seasonal_strength'],result['trend_per_hour'],result['snapshot_hash'],json.dumps({'method':'STL decomposition + rolling linear regression','seed':1072,'period':result['period']}),json.dumps(result),forecast_id))
            congestion_watch.archive_late_forecast(conn, latest, result)
            conn.commit(); return
        if latest['status']!='running': return
        conn.execute("UPDATE peak_forecasts SET status='ready', peak_bags=%s, forecast_at=now(), seasonal_strength=%s, trend_per_hour=%s, snapshot_hash=%s, parameters=%s::jsonb, result=%s::jsonb, updated_at=now() WHERE id=%s", (result['peak_bags'],result['seasonal_strength'],result['trend_per_hour'],result['snapshot_hash'],json.dumps({'method':'STL decomposition + rolling linear regression','seed':1072,'period':result['period']}),json.dumps(result),forecast_id))
        conn.execute("UPDATE flight_waves SET status='forecasted' WHERE id=%s", (wave_id,))
        fresh=conn.execute('SELECT * FROM peak_forecasts WHERE id=%s',(forecast_id,)).fetchone()
        congestion_watch.evaluate_forecast(conn, fresh, result)
        conn.commit()

def main():
    wait_for_db(); migrate(); Path('/shared/worker-heartbeat').touch()
    while True:
        Path('/shared/worker-heartbeat').touch()
        item=r.blpop(QUEUE, timeout=3)
        if item:
            try: process(int(item[1]))
            except Exception: time.sleep(1)

if __name__=='__main__': main()
