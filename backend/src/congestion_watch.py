"""持续拥堵盯警：基于预测未来四档的开单、销警与留档规则。

规则口径
- 每波次只保留一张未销警单（one_open_alert_per_wave 部分唯一索引兜底）。
- 未来四档中存在相邻两档均超过阈值（默认 200 件）即判定为一次「突破」。
- 连续两笔已处理预测均突破，且当前无未销警单时开单。
- 连续两笔已处理预测的四档均不超过阈值时，销掉未销警单。
- 被主管跳过的申请，迟到结果只留档（archived），不参与连续计数、不重复开单。
"""

import json

import psycopg

from .db import audit

THRESHOLD = 200
REQUIRED_STREAK = 2


def _predicted_next_4(result):
    values = result.get('predicted_next_4') or []
    return [round(float(v), 2) for v in values[:4]]


def has_adjacent_breach(values, threshold=THRESHOLD):
    """相邻两档均超过阈值。"""
    return any(
        values[i] > threshold and values[i + 1] > threshold
        for i in range(len(values) - 1)
    )


def all_within_limit(values, threshold=THRESHOLD):
    return len(values) == 4 and all(v <= threshold for v in values)


def _last_evaluation(conn, wave_id):
    return conn.execute(
        'SELECT * FROM congestion_alert_evaluations WHERE wave_id=%s '
        "AND action <> 'archived' ORDER BY id DESC LIMIT 1",
        (wave_id,),
    ).fetchone()


def _open_alert(conn, wave_id):
    return conn.execute(
        "SELECT * FROM congestion_alerts WHERE wave_id=%s AND status='open' ORDER BY id DESC LIMIT 1",
        (wave_id,),
    ).fetchone()


def evaluate_forecast(conn, forecast, result, actor_id=None):
    """对一笔就绪预测执行盯警判定，返回评估记录。调用方负责提交事务。

    幂等：同一预测只评估一次；重试后的新结果会生成新预测记录，不重复开单。
    """
    forecast_id = forecast['id']
    wave_id = forecast['wave_id']
    existing = conn.execute(
        'SELECT id FROM congestion_alert_evaluations WHERE forecast_id=%s',
        (forecast_id,),
    ).fetchone()
    if existing:
        return conn.execute(
            'SELECT * FROM congestion_alert_evaluations WHERE forecast_id=%s',
            (forecast_id,),
        ).fetchone()

    values = _predicted_next_4(result)
    breach = has_adjacent_breach(values)
    prev = _last_evaluation(conn, wave_id)
    prev_breach_streak = prev['consecutive_breach'] if prev else 0
    prev_low_streak = prev['consecutive_low'] if prev else 0

    if breach:
        consecutive_breach = prev_breach_streak + 1
        consecutive_low = 0
    elif all_within_limit(values):
        consecutive_breach = 0
        consecutive_low = prev_low_streak + 1
    else:
        # 四档未全部达标但也没有相邻双突破：连续计数归零，不触发任何动作
        consecutive_breach = 0
        consecutive_low = 0

    action = 'none'
    alert_id = None
    open_alert = _open_alert(conn, wave_id)

    if breach and consecutive_breach >= REQUIRED_STREAK and not open_alert:
        basis = {
            'rule': '相邻两档超过 %d 件，且连续 %d 笔预测突破' % (THRESHOLD, REQUIRED_STREAK),
            'threshold': THRESHOLD,
            'forecast_ids': _recent_forecast_ids(conn, wave_id, forecast_id, REQUIRED_STREAK),
            'predicted_next_4': values,
        }
        # 部分唯一索引兜底并发：多 worker 同时命中时只允许一张未销警单
        try:
            with conn.transaction():
                alert = conn.execute(
                    'INSERT INTO congestion_alerts(wave_id,open_forecast_id,opened_by,open_basis) '
                    'VALUES (%s,%s,%s,%s::jsonb) RETURNING *',
                    (wave_id, forecast_id, actor_id, json.dumps(basis, ensure_ascii=False)),
                ).fetchone()
        except psycopg.errors.UniqueViolation:
            open_alert = _open_alert(conn, wave_id)
            alert_id = open_alert['id'] if open_alert else None
            action = 'none'
        else:
            alert_id = alert['id']
            action = 'opened'
            open_alert = alert
            audit(conn, actor_id, 'open_alert', 'congestion_alert', alert_id, basis)
    elif (not breach) and consecutive_low >= REQUIRED_STREAK and open_alert:
        basis = {
            'rule': '连续 %d 笔预测四档均在 %d 件以内' % (REQUIRED_STREAK, THRESHOLD),
            'threshold': THRESHOLD,
            'forecast_ids': _recent_forecast_ids(conn, wave_id, forecast_id, REQUIRED_STREAK),
            'predicted_next_4': values,
        }
        conn.execute(
            "UPDATE congestion_alerts SET status='cleared', cleared_by=%s, cleared_at=now(), "
            'clear_forecast_id=%s, clear_basis=%s::jsonb WHERE id=%s',
            (actor_id, forecast_id, json.dumps(basis, ensure_ascii=False), open_alert['id']),
        )
        alert_id = open_alert['id']
        action = 'cleared'
        audit(conn, actor_id, 'clear_alert', 'congestion_alert', alert_id, basis)
        open_alert = None

    evaluation = conn.execute(
        'INSERT INTO congestion_alert_evaluations'
        '(forecast_id,wave_id,predicted_next_4,threshold,breach,consecutive_breach,'
        'consecutive_low,action,alert_id,detail) VALUES (%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s::jsonb) '
        'RETURNING *',
        (
            forecast_id, wave_id, json.dumps(values), THRESHOLD, breach,
            consecutive_breach, consecutive_low, action, alert_id,
            json.dumps({'forecast_status': forecast['status']}, ensure_ascii=False),
        ),
    ).fetchone()
    return evaluation


def archive_late_forecast(conn, forecast, result):
    """被跳过申请的迟到结果：只留档，不参与开单/销警计数。"""
    forecast_id = forecast['id']
    existing = conn.execute(
        'SELECT * FROM congestion_alert_evaluations WHERE forecast_id=%s',
        (forecast_id,),
    ).fetchone()
    if existing:
        return existing
    values = _predicted_next_4(result)
    open_alert = _open_alert(conn, forecast['wave_id'])
    return conn.execute(
        'INSERT INTO congestion_alert_evaluations'
        '(forecast_id,wave_id,predicted_next_4,threshold,breach,consecutive_breach,'
        "consecutive_low,action,alert_id,detail) VALUES (%s,%s,%s::jsonb,%s,%s,0,0,'archived',%s,%s::jsonb) "
        'RETURNING *',
        (
            forecast_id, forecast['wave_id'], json.dumps(values), THRESHOLD,
            has_adjacent_breach(values), open_alert['id'] if open_alert else None,
            json.dumps(
                {'reason': '申请已被主管跳过，迟到预测数据只留档，不参与盯警判定'},
                ensure_ascii=False,
            ),
        ),
    ).fetchone()


def _recent_forecast_ids(conn, wave_id, current_id, limit):
    rows = conn.execute(
        'SELECT forecast_id FROM congestion_alert_evaluations WHERE wave_id=%s '
        "AND action <> 'archived' AND forecast_id <= %s ORDER BY id DESC LIMIT %s",
        (wave_id, current_id, limit),
    ).fetchall()
    ids = [row['forecast_id'] for row in rows]
    if current_id not in ids:
        ids = [current_id] + ids
    return list(reversed(ids))[:limit]


# ---------------------------------------------------------------- 值班班次

def get_active_shift(conn):
    return conn.execute(
        "SELECT * FROM duty_shifts WHERE status='active' ORDER BY id DESC LIMIT 1"
    ).fetchone()
