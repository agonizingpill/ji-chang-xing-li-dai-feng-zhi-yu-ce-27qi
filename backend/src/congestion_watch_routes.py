"""值班室持续拥堵盯警路由：班次、交接、签收、预测申请队列与跳过/重试。"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import congestion_watch as watch
from .auth import current_user, require
from .db import audit, connect

router = APIRouter(prefix='/congestion-watch', tags=['值班盯警'])


class ShiftIn(BaseModel):
    shift_code: str = Field(min_length=2, max_length=40)
    note: str = Field(default='', max_length=500)


class HandoverIn(BaseModel):
    to_user_id: int
    note: str = Field(default='', max_length=500)


class SignoffIn(BaseModel):
    note: str = Field(default='', max_length=500)


class SkipIn(BaseModel):
    reason: str = Field(min_length=3, max_length=500)


def _user(conn, user_id):
    return conn.execute('SELECT id,username,display_name,role,active FROM users WHERE id=%s', (user_id,)).fetchone()


# ------------------------------------------------------------------ 班次

@router.get('/shifts')
def list_shifts(user=Depends(current_user)):
    with connect() as conn:
        rows = conn.execute(
            'SELECT s.*,o.display_name AS owner_name,o.username AS owner_username,'
            'h.display_name AS handover_name FROM duty_shifts s '
            'JOIN users o ON o.id=s.owner_id LEFT JOIN users h ON h.id=s.handed_over_to '
            'ORDER BY s.id DESC'
        ).fetchall()
        return [dict(row) for row in rows]


@router.get('/handover-candidates')
def handover_candidates(user=Depends(current_user)):
    with connect() as conn:
        rows = conn.execute(
            "SELECT id,username,display_name,role FROM users "
            "WHERE active AND role <> 'viewer' ORDER BY id"
        ).fetchall()
        return [dict(row) for row in rows]


@router.post('/shifts', status_code=201)
def open_shift(data: ShiftIn, user=Depends(require('operator', 'supervisor'))):
    with connect() as conn:
        active = watch.get_active_shift(conn)
        if active:
            raise HTTPException(409, '已有当班班次，请先完成交接再开新班')
        try:
            row = conn.execute(
                'INSERT INTO duty_shifts(shift_code,owner_id) VALUES (%s,%s) RETURNING *',
                (data.shift_code, user['id']),
            ).fetchone()
        except Exception as exc:
            raise HTTPException(409, '班次编号已存在') from exc
        audit(conn, user['id'], 'open_shift', 'duty_shift', row['id'],
              {'shift_code': data.shift_code, 'note': data.note})
        conn.commit()
        return dict(row)


@router.post('/shifts/handover')
def handover(data: HandoverIn, user=Depends(require('operator', 'supervisor'))):
    with connect() as conn:
        # 锁定当班班次行，交接与签收并发时串行化
        shift = conn.execute("SELECT * FROM duty_shifts WHERE status='active' ORDER BY id DESC LIMIT 1 FOR UPDATE").fetchone()
        if not shift:
            raise HTTPException(409, '当前没有可交接的当班班次')
        if shift['owner_id'] != user['id']:
            raise HTTPException(403, '只有当班人可以发起交接')
        target = _user(conn, data.to_user_id)
        if not target or not target['active'] or target['role'] == 'viewer':
            raise HTTPException(404, '接班人账号不可用或无值班权限')
        if target['id'] == user['id']:
            raise HTTPException(409, '不能交接给自己')
        conn.execute(
            "UPDATE duty_shifts SET status='closed', ended_at=now(), handed_over_to=%s, handover_note=%s WHERE id=%s",
            (target['id'], data.note, shift['id']),
        )
        new_shift = conn.execute(
            'INSERT INTO duty_shifts(shift_code,owner_id) VALUES (%s,%s) RETURNING *',
            ('%s->%s' % (shift['shift_code'], target['username']), target['id']),
        ).fetchone()
        open_alerts = conn.execute(
            "SELECT id FROM congestion_alerts WHERE status='open' ORDER BY id"
        ).fetchall()
        audit(conn, user['id'], 'handover', 'duty_shift', shift['id'],
              {'to_user_id': target['id'], 'open_alert_ids': [a['id'] for a in open_alerts],
               'note': data.note})
        conn.commit()
        return {'closed_shift': dict(shift), 'new_shift': dict(new_shift),
                'open_alert_ids': [a['id'] for a in open_alerts]}


# ------------------------------------------------------------------ 警单

@router.get('/alerts')
def list_alerts(user=Depends(current_user)):
    with connect() as conn:
        rows = conn.execute(
            'SELECT a.*, w.code AS wave_code, w.terminal, '
            'op.display_name AS opened_by_name, cl.display_name AS cleared_by_name '
            'FROM congestion_alerts a JOIN flight_waves w ON w.id=a.wave_id '
            'LEFT JOIN users op ON op.id=a.opened_by LEFT JOIN users cl ON cl.id=a.cleared_by '
            'ORDER BY a.id DESC'
        ).fetchall()
        return [dict(row) for row in rows]


@router.get('/alerts/{alert_id}')
def alert_detail(alert_id: int, user=Depends(current_user)):
    with connect() as conn:
        alert = conn.execute(
            'SELECT a.*, w.code AS wave_code, w.terminal FROM congestion_alerts a '
            'JOIN flight_waves w ON w.id=a.wave_id WHERE a.id=%s', (alert_id,),
        ).fetchone()
        if not alert:
            raise HTTPException(404, '警单不存在')
        signoffs = conn.execute(
            'SELECT g.*, u.display_name AS signer_name, u.username AS signer_username, '
            's.shift_code FROM congestion_alert_signoffs g '
            'JOIN users u ON u.id=g.signer_id JOIN duty_shifts s ON s.id=g.duty_shift_id '
            'WHERE g.alert_id=%s ORDER BY g.id', (alert_id,),
        ).fetchall()
        evaluations = conn.execute(
            'SELECT e.*, f.status AS forecast_status FROM congestion_alert_evaluations e '
            'JOIN peak_forecasts f ON f.id=e.forecast_id '
            'WHERE e.wave_id=%s ORDER BY e.id', (alert['wave_id'],),
        ).fetchall()
        return {'alert': dict(alert),
                'signoffs': [dict(row) for row in signoffs],
                'evaluations': [dict(row) for row in evaluations]}


@router.post('/alerts/{alert_id}/signoff', status_code=201)
def signoff(alert_id: int, data: SignoffIn, user=Depends(require('operator', 'supervisor'))):
    with connect() as conn:
        shift = conn.execute(
            "SELECT * FROM duty_shifts WHERE status='active' ORDER BY id DESC LIMIT 1 FOR UPDATE"
        ).fetchone()
        if not shift:
            raise HTTPException(409, '当前没有当班班次，无法签收')
        if shift['owner_id'] != user['id']:
            raise HTTPException(403, '只有当班人可以签收')
        alert = conn.execute('SELECT * FROM congestion_alerts WHERE id=%s FOR UPDATE', (alert_id,)).fetchone()
        if not alert:
            raise HTTPException(404, '警单不存在')
        if alert['status'] != 'open':
            raise HTTPException(409, '该警单已销警，无需签收')
        try:
            row = conn.execute(
                'INSERT INTO congestion_alert_signoffs(alert_id,duty_shift_id,signer_id,note) '
                'VALUES (%s,%s,%s,%s) RETURNING *',
                (alert_id, shift['id'], user['id'], data.note),
            ).fetchone()
        except Exception as exc:
            raise HTTPException(409, '本班次已签收过该警单') from exc
        audit(conn, user['id'], 'signoff_alert', 'congestion_alert', alert_id,
              {'duty_shift_id': shift['id'], 'note': data.note})
        conn.commit()
        return dict(row)


# ---------------------------------------------------------- 判定依据留档

@router.get('/evaluations')
def list_evaluations(user=Depends(current_user)):
    with connect() as conn:
        rows = conn.execute(
            'SELECT e.*, w.code AS wave_code, f.status AS forecast_status '
            'FROM congestion_alert_evaluations e '
            'JOIN flight_waves w ON w.id=e.wave_id '
            'JOIN peak_forecasts f ON f.id=e.forecast_id ORDER BY e.id DESC'
        ).fetchall()
        return [dict(row) for row in rows]


# ---------------------------------------------------------- 预测申请队列

@router.get('/forecast-queue')
def forecast_queue(user=Depends(current_user)):
    """按申请顺序返回未完成的预测申请（排队中/计算中）。"""
    with connect() as conn:
        rows = conn.execute(
            "SELECT f.*, w.code AS wave_code, w.terminal FROM peak_forecasts f "
            "JOIN flight_waves w ON w.id=f.wave_id "
            "WHERE f.status IN ('queued','running') ORDER BY f.id"
        ).fetchall()
        return [dict(row) for row in rows]


@router.post('/forecasts/{forecast_id}/skip')
def skip_forecast(forecast_id: int, data: SkipIn,
                  user=Depends(require('supervisor'))):
    """主管跳过卡住的申请；迟到结果只留档，重试不重复开单。"""
    with connect() as conn:
        forecast = conn.execute('SELECT * FROM peak_forecasts WHERE id=%s FOR UPDATE', (forecast_id,)).fetchone()
        if not forecast:
            raise HTTPException(404, '预测申请不存在')
        if forecast['status'] not in ('queued', 'running'):
            raise HTTPException(409, '只有排队中或计算中的申请可以跳过')
        conn.execute(
            "UPDATE peak_forecasts SET status='skipped', failure_reason=%s, updated_at=now() WHERE id=%s",
            ('主管跳过：' + data.reason, forecast_id),
        )
        audit(conn, user['id'], 'skip_forecast', 'peak_forecast', forecast_id,
              {'reason': data.reason})
        conn.commit()
    # 从 Redis 队列移除，避免 worker 继续占用；正在计算的任务由 worker 自行留档
    try:
        from .worker import r as redis_client
        redis_client.lrem('airport:forecast', 0, str(forecast_id))
    except Exception:
        pass
    return {'id': forecast_id, 'status': 'skipped'}


# ------------------------------------------------------------------ 看板

@router.get('/board')
def board(user=Depends(current_user)):
    with connect() as conn:
        active_shift = watch.get_active_shift(conn)
        if active_shift:
            active_shift = dict(active_shift)
            owner = _user(conn, active_shift['owner_id'])
            active_shift['owner_name'] = owner['display_name'] if owner else None
            active_shift['owner_username'] = owner['username'] if owner else None
        open_alerts = conn.execute(
            'SELECT a.*, w.code AS wave_code, w.terminal FROM congestion_alerts a '
            "JOIN flight_waves w ON w.id=a.wave_id WHERE a.status='open' ORDER BY a.id"
        ).fetchall()
        open_alert_list = []
        for alert in open_alerts:
            signed_row = conn.execute(
                'SELECT 1 FROM congestion_alert_signoffs WHERE alert_id=%s AND duty_shift_id=%s',
                (alert['id'], active_shift['id']),
            ).fetchone() if active_shift else None
            item = dict(alert)
            item['signed_by_current_shift'] = signed_row is not None
            open_alert_list.append(item)
        queue = conn.execute(
            "SELECT f.id, f.wave_id, f.status, f.attempts, f.created_at, w.code AS wave_code, w.terminal "
            "FROM peak_forecasts f JOIN flight_waves w ON w.id=f.wave_id "
            "WHERE f.status IN ('queued','running') ORDER BY f.id"
        ).fetchall()
        recent = conn.execute(
            'SELECT e.id, e.forecast_id, e.wave_id, e.predicted_next_4, e.threshold, e.breach, '
            'e.consecutive_breach, e.consecutive_low, e.action, e.evaluated_at, w.code AS wave_code '
            'FROM congestion_alert_evaluations e JOIN flight_waves w ON w.id=e.wave_id '
            'ORDER BY e.id DESC LIMIT 20'
        ).fetchall()
        return {
            'threshold': watch.THRESHOLD,
            'active_shift': active_shift,
            'is_on_duty': bool(active_shift) and active_shift['owner_id'] == user['id'],
            'open_alerts': open_alert_list,
            'queue': [dict(row) for row in queue],
            'recent_evaluations': [dict(row) for row in recent],
        }
